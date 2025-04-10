# coding=utf-8
import concurrent.futures
import os
import re
import warnings
from collections import abc as container_abcs
from collections import defaultdict
from itertools import chain
from pathlib import Path
from typing import Dict, Generator, Iterator, List, Mapping, Optional, OrderedDict, Tuple, Union

import torch
import torch.distributed as dist
import torch.nn as nn
from packaging.version import Version
from peft import PeftModel, PeftType
from peft.utils.other import EMBEDDING_LAYER_NAMES, check_file_exists_on_hf_hub
from peft.utils.save_and_load import get_embedding_layer_name, has_valid_embedding_base_layer
from torch.optim import Optimizer
from torch.utils._pytree import tree_flatten, tree_map, tree_unflatten

from colossalai.accelerator import get_accelerator
from colossalai.interface.model import PeftUnwrapMixin
from colossalai.tensor.d_tensor import (
    is_customized_distributed_tensor,
    is_distributed_tensor,
    to_global,
    to_global_for_customized_distributed_tensor,
)
from colossalai.utils import get_current_device
from colossalai.utils.safetensors import _flatten_optim_state_dict, load_flat

SAFE_WEIGHTS_NAME = "model.safetensors"
WEIGHTS_NAME = "pytorch_model.bin"
STATES_NAME = "pytorch_optim.bin"
SAFE_STATE_NAME = "optimizer.safetensors"
SAFE_WEIGHTS_INDEX_NAME = "model.safetensors.index.json"
WEIGHTS_INDEX_NAME = "pytorch_model.bin.index.json"
STATES_INDEX_NAME = "pytorch_optim.bin.index.json"
SAFE_STATES_INDEX_NAME = "optimizer.safetensors.index.json"
GROUP_FILE_NAME = "pytorch_optim_group.bin"

# ======================================
# General helper functions
# ======================================


def calculate_tensor_size(tensor: torch.Tensor) -> float:
    """
    Calculate the size of a parameter in MB. Used to compute whether a group of params exceed the shard size.
    If so, a new shard should be created.

    Args:
        tensor (torch.Tensor): the tensor to calculate size for.

    Returns:
        float: size of the tensor in MB.
    """
    return tensor.numel() * tensor.element_size() / 1024 / 1024


def is_safetensors_available() -> bool:
    """
    Check whether safetensors is available.

    Returns:
        bool: whether safetensors is available.
    """
    try:
        return True
    except ImportError:
        return False


def is_dtensor_checkpoint(checkpoint_file_path: str) -> bool:
    """
    Check whether the checkpoint file is a dtensor checkpoint.

    Args:
        checkpoint_file_path (str): path to the checkpoint file.

    Returns:
        bool: whether the checkpoint file is a dtensor checkpoint.
    """
    if checkpoint_file_path.endswith(".*.safetensors") or checkpoint_file_path.endswith(".*.bin"):
        return True
    else:
        return False


def is_safetensor_checkpoint(checkpoint_file_path: str) -> bool:
    """
    Check whether the checkpoint file is a safetensor checkpoint.

    Args:
        checkpoint_file_path (str): path to the checkpoint file.

    Returns:
        bool: whether the checkpoint file is a safetensor checkpoint.
    """
    if checkpoint_file_path.endswith(".safetensors"):
        return True
    else:
        return False


def search_tp_partition_dim(current_shape: torch.Size, original_shape: torch.Size, tp_size: int) -> Optional[int]:
    """
    Given the current shape of parameter and the shape of parameter before sharding,
    return the dimension along which the parameter is sharded when using tensor parallel.
    If tensor parallel is not used, return None.

    Args:
        current_shape (torch.Size): The current shape of parameter after sharding.
        original_shape (torch.Size): The shape of parameter before sharding.
        tp_size (int): The size of tp group.

    Returns:
        Optional[int]: The dimension along which parameter is partitioned.
    """
    partition_dim = None
    for dim, length in enumerate(original_shape):
        if length > current_shape[dim]:
            partition_dim = dim
            break
    if partition_dim is not None:
        assert (
            original_shape[partition_dim] == tp_size * current_shape[partition_dim]
        ), f"The parameter isn't evenly distributed among tensor parallel group: \
                shape before sharding {original_shape}, shape after sharding {current_shape}"

    return partition_dim


def search_padding_dim(global_shape: torch.Size, original_shape: torch.Size) -> Optional[int]:
    padding_dim = None
    for dim, length in enumerate(global_shape):
        if length > original_shape[dim]:
            padding_dim = dim
            break
    return padding_dim


# ======================================
# Helper classes and functions for saving shard file
# ======================================


class StateDictSharder:
    def __init__(self, size_per_shard: int) -> None:
        self.max_shard_size = size_per_shard
        self.current_block = OrderedDict()
        self.current_block_size = 0

    def append_param(self, name: str, tensor: torch.Tensor) -> Tuple[Optional[OrderedDict], int]:
        tensor_size = calculate_tensor_size(tensor)
        ret_block = None
        ret_block_size = 0

        # before we return the current block and create a new block,
        # we need to ensure that the current block is not empty
        if self.current_block_size + tensor_size > self.max_shard_size and self.current_block_size > 0:
            ret_block = self.current_block
            ret_block_size = self.current_block_size
            self.current_block = OrderedDict()
            self.current_block_size = 0

        self.current_block[name] = tensor
        self.current_block_size += tensor_size
        return ret_block, ret_block_size

    def append_optim_state(self, param_id: int, state: OrderedDict) -> Tuple[Optional[OrderedDict], int]:
        # A state might contain more than one tensors.
        # e.g. each Adam state includes: 'step', 'exp_avg', 'exp_avg_sq'
        state_size = 0
        isDTensor = False
        for state_tensor in state.values():
            # When state_tensor is not of Tensor class,
            # e.g., a SGD optimizer with momentum set to 0 can have None as state
            # The calculation of tensor size should be skipped to avoid error.
            if not isinstance(state_tensor, torch.Tensor):
                continue

            # If the states are stored as DTensors, mark isDTensor as true.
            if is_distributed_tensor(state_tensor):
                isDTensor = True
            state_size += calculate_tensor_size(state_tensor)

        ret_block = None
        ret_block_size = 0

        # directly return if state is stored as distributed tensor
        if isDTensor:
            return ret_block, ret_block_size

        # before we return the current block and create a new block,
        # we need to ensure that the current block is not empty
        if self.current_block_size + state_size > self.max_shard_size and self.current_block_size > 0:
            ret_block = self.current_block
            ret_block_size = self.current_block_size
            self.current_block = OrderedDict()
            self.current_block_size = 0

        self.current_block[param_id] = state
        self.current_block_size += state_size
        return ret_block, ret_block_size


def gather_distributed_param(param: torch.Tensor, keep_vars: bool = False) -> torch.Tensor:
    """
    Gather the complete parameter for saving if passed in param is distributed under tp setting.

    Args:
        param (torch.Tensor): A model parameter, might be d_tensor.
        keep_vars (bool, optional): Whether to return the parameter in calculation graph. Defaults to False.

    Returns:
        torch.Tensor: the complete parameter
    """
    param_ = param if keep_vars else param.detach()
    if is_distributed_tensor(param_):
        return to_global(param_)
    elif is_customized_distributed_tensor(param_):
        return to_global_for_customized_distributed_tensor(param_)
    else:
        return param_


def save_state_dict_shards(
    sharded_state_dict: Iterator[Tuple[OrderedDict, int]],
    checkpoint: str,
    index_file: "CheckpointIndexFile",
    base_filename: str,
    is_master: bool,
    use_safetensors: bool = False,
    use_pp_format: bool = False,
) -> int:
    """
    Save sharded state dict only on master rank, this method can be used by both model and optimizer states.
    Args:
        sharded_state_dict (Iterator[Tuple[OrderedDict, int]]): a generator of shards, each shard contains state dict and shard size.
        checkpoint (str): The path of checkpoint directory as string.
        index_file (CheckpointIndexFile): The index file object to be updated.
        base_filename (str): Decides the prefix of filenames of shards.
        is_master (bool): Whether current rank is main process.
        use_safetensors (bool, optional): Whether to use safetensors to save checkpoint. Defaults to False.
        use_pp_format: (bool, optional): Whether to save the files in pipeline format including stage information. Defaults to False.

    Returns:
        int: the total size of shards
    """

    total_size = 0
    shard_filenames = []
    for idx, shard_pair in enumerate(sharded_state_dict):
        shard, current_size = shard_pair
        # Just loop over the sharder and gather to other ranks if not master
        if not is_master:
            del shard
            continue
        shard_file = get_shard_filename(base_filename, idx)
        total_size = total_size + current_size
        for key in shard.keys():
            index_file.append_weight_map(key, shard_file)
        checkpoint_file_path = os.path.join(checkpoint, shard_file)

        # Only save on master rank.
        save_state_dict(shard, checkpoint_file_path, use_safetensors=use_safetensors)
        shard_filenames.append(shard_file)
        del shard

    # Clean folder, deleted unneeded files.
    clean_folder(checkpoint, base_filename, shard_filenames, is_master=is_master, use_pp_format=use_pp_format)

    return total_size


def async_save_state_dict_shards(
    sharded_state_dict: Iterator[Tuple[OrderedDict, int]],
    checkpoint: str,
    index_file: "CheckpointIndexFile",
    base_filename: str,
    is_master: bool,
    use_pp_format: bool = False,
    state_preprocess: bool = False,
) -> Tuple[int, list]:
    """
    Save sharded state dict only on master rank, this method can be used by both model and optimizer states.
    Args:
        sharded_state_dict (Iterator[Tuple[OrderedDict, int]]): a generator of shards, each shard contains state dict and shard size.
        checkpoint (str): The path of checkpoint directory as string.
        index_file (CheckpointIndexFile): The index file object to be updated.
        base_filename (str): Decides the prefix of filenames of shards.
        is_master (bool): Whether current rank is main process.
        use_safetensors (bool, optional): Whether to use safetensors to save checkpoint. Defaults to False.
        use_pp_format: (bool, optional): Whether to save the files in pipeline format including stage information. Defaults to False.

    Returns:
        int: the total size of shards
    """
    from colossalai.utils.safetensors import save

    total_size = 0
    shard_filenames = []
    writers = []
    for idx, shard_pair in enumerate(sharded_state_dict):
        shard, current_size = shard_pair
        # Just loop over the sharder and gather to other ranks if not master
        if not is_master:
            del shard
            continue
        shard_file = get_shard_filename(base_filename, idx)
        total_size = total_size + current_size
        for key in shard.keys():
            index_file.append_weight_map(key, shard_file)
        checkpoint_file_path = os.path.join(checkpoint, shard_file)

        if state_preprocess:
            state_dict, metadata = _flatten_optim_state_dict(state_dict=shard, seperator=".")
        else:
            state_dict = shard
            metadata = None

        # Only save on master rank.
        writer = save(checkpoint_file_path, state_dict=state_dict, metadata=metadata)
        writers.append(writer)
        shard_filenames.append(shard_file)
        del shard

    # Clean folder, deleted unneeded files.
    clean_folder(checkpoint, base_filename, shard_filenames, is_master=is_master, use_pp_format=use_pp_format)

    return total_size, writers


def async_move_save_state_dict_shards(
    sharded_state_dict: Iterator[Tuple[OrderedDict, int]],
    checkpoint: str,
    index_file: "CheckpointIndexFile",
    base_filename: str,
    is_master: bool,
    pinned_state_dict: Optional[Dict[str, torch.Tensor]],
    use_pp_format: bool = False,
    state_preprocess: bool = False,
) -> Tuple[int, Dict[str, torch.Tensor], list]:
    """
    Save sharded state dict only on master rank, this method can be used by both model and optimizer states.
    Args:
        sharded_state_dict (Iterator[Tuple[OrderedDict, int]]): a generator of shards, each shard contains state dict and shard size.
        checkpoint (str): The path of checkpoint directory as string.
        index_file (CheckpointIndexFile): The index file object to be updated.
        base_filename (str): Decides the prefix of filenames of shards.
        is_master (bool): Whether current rank is main process.
        use_safetensors (bool, optional): Whether to use safetensors to save checkpoint. Defaults to False.
        use_pp_format: (bool, optional): Whether to save the files in pipeline format including stage information. Defaults to False.

    Returns:
        int: the total size of shards
    """
    from colossalai.utils.safetensors import move_and_save

    total_size = 0
    shard_filenames = []
    if pinned_state_dict is None:
        returned_state_dict = {}
    else:
        returned_state_dict = pinned_state_dict
    writers = []
    for idx, shard_pair in enumerate(sharded_state_dict):
        shard, current_size = shard_pair
        # Just loop over the sharder and gather to other ranks if not master
        if not is_master:
            del shard
            continue
        shard_file = get_shard_filename(base_filename, idx)
        total_size = total_size + current_size
        for key in shard.keys():
            index_file.append_weight_map(key, shard_file)
        checkpoint_file_path = os.path.join(checkpoint, shard_file)

        if state_preprocess:
            state_dict, metadata = _flatten_optim_state_dict(state_dict=shard)
        else:
            state_dict = shard
            metadata = None

        if pinned_state_dict is not None:
            sub_pinned_state_dict = {k: pinned_state_dict[k] for k in state_dict.keys()}
        else:
            sub_pinned_state_dict = create_pinned_state_dict(state_dict)
            returned_state_dict.update(sub_pinned_state_dict)

        # Only save on master rank.
        writer = move_and_save(checkpoint_file_path, state_dict, sub_pinned_state_dict, metadata)
        writers.append(writer)
        shard_filenames.append(shard_file)
        del shard

    # Clean folder, deleted unneeded files.
    clean_folder(checkpoint, base_filename, shard_filenames, is_master=is_master, use_pp_format=use_pp_format)

    return total_size, returned_state_dict, writers


def shard_model_checkpoint(
    state_dict: torch.Tensor,
    max_shard_size: int = 1024,
    pinned_state_dicts: Optional[Dict[int, Dict[str, torch.Tensor]]] = None,
) -> Iterator[Tuple[OrderedDict, int]]:
    """
    Splits a model state dictionary in sub-checkpoints so that the final size of each sub-checkpoint does not exceed a
    given size.
    """
    state_dict_sharder = StateDictSharder(max_shard_size)

    for key, weight in state_dict.items():
        if not is_distributed_tensor(weight):
            if pinned_state_dicts is not None:
                if key not in pinned_state_dicts:
                    pinned_state_dicts[key] = torch.empty_like(weight, pin_memory=True, device="cpu")
                pinned_state_dicts[key].copy_(weight)
                weight = pinned_state_dicts[key]
            block, block_size = state_dict_sharder.append_param(key, weight)

        if block != None:
            yield block, block_size

    # Return the last block in sharder.
    yield state_dict_sharder.current_block, state_dict_sharder.current_block_size


def shard_optimizer_checkpoint(
    state_dict: dict, max_shard_size: int = 1024, pinned_state_dicts: Optional[Dict[str, torch.Tensor]] = None
) -> Iterator[Tuple[OrderedDict, int]]:
    """
    Splits an optimizer state dictionary in sub-checkpoints so that the final size of each sub-checkpoint does not exceed a
    given size.
    """

    # Only split state_dict['state']; state_dict['param_group'] is not considered in this function.
    states = state_dict["state"]
    state_dict_sharder = StateDictSharder(max_shard_size)

    for param_id, state in states.items():
        if pinned_state_dicts is not None:
            if param_id not in pinned_state_dicts:
                pinned_state_dicts[param_id] = {}
                for k, v in state.items():
                    if k not in pinned_state_dicts[param_id]:
                        pinned_state_dicts[param_id][k] = torch.empty_like(v, pin_memory=True, device="cpu")
                    pinned_state_dicts[param_id][k].copy_(v)
                    state[k] = pinned_state_dicts[param_id][k]

        block, block_size = state_dict_sharder.append_optim_state(param_id, state)
        if block != None:
            yield block, block_size

    # Return the last block in sharder.
    yield state_dict_sharder.current_block, state_dict_sharder.current_block_size


# ======================================
# Helper functions for saving state dict
# ======================================


def save_state_dict(
    state_dict: dict,
    checkpoint_file_path: str,
    use_safetensors: bool,
) -> None:
    """
    Save state dict to checkpoint.

    Args:
        state_dict (dict): state dict.
        checkpoint_file_path (str): path to the checkpoint file.
        use_safetensors (bool): whether to use safetensors to save the checkpoint.
    """
    # Move all tensors in the state_dict to CPU before saving to avoid serialization issues
    state_dict_cpu = tree_map(lambda x: x.data.cpu() if torch.is_tensor(x) else x, state_dict)

    if use_safetensors:
        assert is_safetensors_available(), "safetensors is not available."
        assert checkpoint_file_path.endswith(
            ".safetensors"
        ), "safetensors only supports .safetensors suffix for checkpoint file."
        from safetensors.torch import save_file as safe_save_file

        safe_save_file(state_dict_cpu, checkpoint_file_path, metadata={"format": "pt"})
    else:
        torch.save(state_dict_cpu, checkpoint_file_path)


def save_param_groups(state_dict: dict, group_file_path: str) -> None:
    """
    Save information of param_groups to given file path.

    Args:
        state_dict (dict): state dict.
        group_file_path (str): path to the group file.
    """
    param_groups = state_dict["param_groups"]
    torch.save(param_groups, group_file_path)


def clean_folder(
    checkpoint_path: str,
    weights_name: str,
    shard_filenames: List[str],
    is_master: bool = True,
    use_pp_format: bool = False,
):
    """
    Clean the unneeded files in checkpoint directory after shards of state_dict have been saved.

    Args:
        checkpoint_path (str): Path to the checkpoint directory.
        weights_name (str): Decides the prefix of filenames of weight shards.
        shard_filenames (List[str]): The list of saved shard filenames which should not be removed.
        is_master (bool, optional): Whether current rank is main process. Defaults to True.
        use_pp_format: (bool, optional): Whether to save the files in pipeline format including stage information. Defaults to False.

    """
    if is_master:
        for filename in os.listdir(checkpoint_path):
            full_filename = os.path.join(checkpoint_path, filename)
            weights_no_suffix = weights_name.replace(".bin", "").replace(".safetensors", "")
            filename_no_suffix = filename.replace(".bin", "").replace(".safetensors", "")
            if not use_pp_format:
                reg = re.compile(r"(.*?)-\d{5}")
            else:
                # When this checkpoint is created by pipeline parallel process, the pattern is a little different.
                reg = re.compile(r"(.*?)-stage-\d{5}-shard-\d{5}")
            if (
                filename.startswith(weights_no_suffix)
                and filename not in shard_filenames
                and reg.fullmatch(filename_no_suffix) is not None
            ):
                os.remove(full_filename)


def save_config_file(model: nn.Module, checkpoint_path: str, is_master: bool = True):
    """
    Save config.json/generation_config.json if model is a Huggingface pretrained model.
    This method can only be called when a model is saved in a sharded way.

    Args:
        model (nn.Module): The model whose config should be saved if it's a huggingface model.
        checkpoint_path (str): Path to the checkpoint directory.
        is_master (bool): Whether current rank is main process.
    """
    try:
        from transformers.modeling_utils import PreTrainedModel, get_parameter_dtype
        from transformers.modeling_utils import unwrap_model as unwrap_huggingface_model
    except ImportError:
        return
    if isinstance(model, PeftUnwrapMixin):
        model = model.base_model
    if not isinstance(model, PreTrainedModel):
        return

    model = unwrap_huggingface_model(model)

    # save the string version of dtype to the config, e.g. convert torch.float32 => "float32"
    dtype = get_parameter_dtype(model)
    model.config.torch_dtype = str(dtype).split(".")[1]

    # Attach architecture to the config
    model.config.architectures = [model.__class__.__name__]

    # Save the config
    if is_master:
        model.config.save_pretrained(checkpoint_path)
        if model.can_generate():
            model.generation_config.save_pretrained(checkpoint_path)


def save_dtensor(name: str, tensor: torch.Tensor, index_file: "CheckpointIndexFile", use_safetensors: bool) -> None:
    """
    Save distributed tensor to checkpoint. This checkpoint will be a dictionary which contains
    only one tensor.

    Args:
        tensor (Tensor): tensor to be saved.
        index_file (CheckpointIndexFile): path to the checkpoint file.
        size_per_shard (int): size per shard in MB.
    """
    root_path = index_file.root_path
    output_root_path = root_path.joinpath("dtensor")

    # create directory
    output_root_path.mkdir(exist_ok=True)

    # save tensor to this directory
    # TODO(YuliangLiu): get index of the tensor shard
    # e.g. index =
    index = 0

    # save tensor to file
    ckpt_file_name = generate_dtensor_file_name(name, index, use_safetensors)
    ckpt_file_path = output_root_path.joinpath(ckpt_file_name)

    # dtensor ckpt file always contains only one tensor
    state_dict = {name: tensor}
    save_state_dict(state_dict, str(ckpt_file_path), use_safetensors)

    # update the weight map
    # * means all shards
    ckpt_file_name_in_weight_map = "dtensor/" + generate_dtensor_file_name(name, "*", use_safetensors)
    index_file.append_weight_map(name, ckpt_file_name_in_weight_map)


def get_checkpoint_file_suffix(use_safetensors: bool) -> str:
    """
    Get checkpoint file suffix.

    Args:
        use_safetensors (bool): whether to use safetensors to save the checkpoint.

    Returns:
        str: checkpoint file suffix.
    """
    if use_safetensors:
        return ".safetensors"
    else:
        return ".bin"


def generate_checkpoint_shard_file_name(
    index: int, total_number: int, use_safetensors: bool, prefix: str = None
) -> str:
    """
    Generate checkpoint shard file name.

    Args:
        index (int): index of the shard.
        total_number (int): total number of shards.
        use_safetensors (bool): whether to use safetensors to save the checkpoint.
        prefix (str): prefix of the shard file name. Default: None.

    Returns:
        str: checkpoint shard file name.
    """
    suffix = get_checkpoint_file_suffix(use_safetensors)

    if prefix is None:
        return f"{index:05d}-of-{total_number:05d}.{suffix}"
    else:
        return f"{prefix}-{index:05d}-of-{total_number:05d}.{suffix}"


def generate_dtensor_file_name(param_name: str, index: int, use_safetensors: bool) -> str:
    """
    Generate dtensor file name.

    Args:
        param_name (str): name of the distributed parameter.
        index (int): index of the shard.
        use_safetensors (bool): whether to use safetensors to save the checkpoint.

    Returns:
        str: dtensor file name.
    """
    suffix = get_checkpoint_file_suffix(use_safetensors)
    return f"{param_name}.{index}.{suffix}"


# ========================================
# Helper functions for loading state dict
# ========================================


def load_shard_state_dict(checkpoint_file: Path, use_safetensors: bool = False):
    """
    load shard state dict into model
    """
    if use_safetensors and not checkpoint_file.suffix == ".safetensors":
        raise Exception("load the model using `safetensors`, but no file endwith .safetensors")
    if use_safetensors:
        from safetensors.torch import load_file as safe_load_file

        return safe_load_file(checkpoint_file)
    else:
        return torch.load(checkpoint_file, map_location=torch.device("cpu"))


def load_state_dict_into_model(
    model: nn.Module, state_dict: torch.Tensor, missing_keys: List, strict: bool = False, load_sub_module: bool = True
):
    r"""Copies parameters and buffers from :attr:`state_dict` into
    this module and its descendants.

    Args:
        state_dict (dict): a dict containing parameters and
            persistent buffers.
    """
    if isinstance(model, PeftUnwrapMixin):
        state_dict = model.patch_state_dict(state_dict)
        model = model.base_model
    if not isinstance(state_dict, Mapping):
        raise TypeError("Expected state_dict to be dict-like, got {}.".format(type(state_dict)))

    unexpected_keys: List[str] = []
    sub_missing_keys: List[str] = []
    error_msgs: List[str] = []

    # copy state_dict so _load_from_state_dict can modify it
    metadata = getattr(state_dict, "_metadata", None)
    state_dict = OrderedDict(state_dict)
    if metadata is not None:
        state_dict._metadata = metadata

    def load(module: nn.Module, state_dict, prefix="", load_sub_module: bool = True):
        local_metadata = {} if metadata is None else metadata.get(prefix[:-1], {})
        args = (state_dict, prefix, local_metadata, True, sub_missing_keys, unexpected_keys, error_msgs)
        # Parameters of module and children will start with prefix. We can exit early if there are none in this
        # state_dict
        if strict or len([key for key in state_dict if key.startswith(prefix)]) > 0:
            module._load_from_state_dict(*args)
        if load_sub_module:
            for name, child in module._modules.items():
                if child is not None:
                    load(child, state_dict, prefix + name + ".")

    load(model, state_dict, "", load_sub_module)
    del load

    missing_keys = missing_keys.append(sub_missing_keys)

    if strict:
        if len(unexpected_keys) > 0:
            error_msgs = [
                "Unexpected key(s) in state_dict: {}. ".format(", ".join('"{}"'.format(k) for k in unexpected_keys))
            ]
            raise RuntimeError(
                "Error(s) in loading state_dict for {}:\n\t{}".format(model.__class__.__name__, "\n\t".join(error_msgs))
            )


def load_param_groups_into_optimizer(optimizer: Optimizer, param_group_path: str) -> dict:
    """
    Load information of param_groups into an initialized optimizer.
    """

    # Load list of param_groups from given file path.
    # The params in saved_groups are in the form of integer indices.
    saved_groups = torch.load(param_group_path, map_location=torch.device("cpu"))
    if not isinstance(saved_groups, List):
        raise ValueError(f"The param_groups saved at {param_group_path} is not of List type")

    # The params in param_groups are in the form of pytorch tensors.
    # For more details, please view source code of Optimizer class in pytorch.
    param_groups = optimizer.param_groups

    # Check the compatibility of saved_groups and param_groups.
    if len(param_groups) != len(saved_groups):
        raise ValueError("loaded state dict has a different number of original parameter groups")
    param_lens = (len(g["params"]) for g in param_groups)
    saved_lens = (len(g["params"]) for g in saved_groups)
    if any(p_len != s_len for p_len, s_len in zip(param_lens, saved_lens)):
        raise ValueError(
            "loaded state dict contains a parameter group " "that doesn't match the size of optimizer's group"
        )

    # Creating mapping from id to parameters.
    id_map = {
        old_id: p
        for old_id, p in zip(
            chain.from_iterable((g["params"] for g in saved_groups)),
            chain.from_iterable((g["params"] for g in param_groups)),
        )
    }

    # Update parameter groups, setting their 'params' value.
    def update_group(group, new_group):
        new_group["params"] = group["params"]
        return new_group

    updated_groups = [update_group(g, ng) for g, ng in zip(param_groups, saved_groups)]

    optimizer.__dict__.update({"param_groups": updated_groups})
    return id_map


def load_states_into_optimizer(optimizer: Optimizer, state_dict: dict, id_map: dict, strict: bool = False):
    r"""Copies states from `state_dict` into an Optimizer object.

    Args:
        optimizer(Optimizer): An initialized Optimizer object to be loaded
        state_dict(dict): A mapping from tensor index (an integer)
            to its states to be loaded (a mapping from state name to a tensor).
        id_map(dict): A mapping from tensor index (an integer)
            to its corresponding parameter (a tensor) whose states will be updated.
        strict(bool, optional): If set to True, only load the parameters with its id in id_map. Defaults to False.
    """

    # Ensure that the keys of state_dict are integers.
    state_dict = {int(k): v for k, v in state_dict.items()}

    def cast(param, value, key=None):
        r"""Make a deep copy of value, casting all tensors to device of param."""
        if isinstance(value, torch.Tensor):
            # Floating-point types are a bit special here. They are the only ones
            # that are assumed to always match the type of params.
            # Make sure state['step'] is not casted https://github.com/pytorch/pytorch/issues/74424
            if key != "step":
                if param.is_floating_point():
                    value = value.to(param.dtype)
                value = value.to(param.device, non_blocking=True)
            return value
        elif isinstance(value, dict):
            return {k: cast(param, v, key=k) for k, v in value.items()}
        elif isinstance(value, container_abcs.Iterable):
            return type(value)(cast(param, v) for v in value)
        else:
            return value

    # Copy state assigned to params (and cast tensors to appropriate types).
    # State that is not assigned to params is copied as is (needed for
    # backward compatibility).
    new_states = defaultdict(dict)
    for k, v in state_dict.items():
        if k in id_map:
            param = id_map[k]
            new_states[param] = cast(param, v)
        elif not strict:
            new_states[k] = v

    get_accelerator().synchronize()
    optimizer.state.update(new_states)


def sharded_optimizer_loading_epilogue(optimizer: Optimizer):
    r"""Do the cleaning up work after state_dict has been loaded into optimizer

    Args:
        optimizer(Optimizer): An optimizer object whose state has just been loaded.
    """

    # Do the cleaning up as in src code of Pytorch.
    if Version(torch.__version__) >= Version("2.0.0"):
        optimizer._patch_step_function()  # To support multiprocessing pickle/unpickle
    else:
        optimizer._hook_for_profile()  # To support multiprocessing pickle/unpickle.
    optimizer.defaults.setdefault("differentiable", False)


def has_index_file(checkpoint_path: str) -> Tuple[bool, Optional[Path]]:
    """
    Check whether the checkpoint has an index file.

    Args:
        checkpoint_path (str): path to the checkpoint.

    Returns:
        Tuple[bool, Optional[Path]]: a tuple of (has_index_file, index_file_path)
    """
    checkpoint_path = Path(checkpoint_path)
    if checkpoint_path.is_file():
        # check if it is .index.json
        reg = re.compile("(.*?).index((\..*)?).json")
        if reg.fullmatch(checkpoint_path.name) is not None:
            return True, checkpoint_path
        else:
            return False, None
    elif checkpoint_path.is_dir():
        # check if there is only one a file ending with .index.json in this directory
        index_files = list(checkpoint_path.glob("*.index.*json"))

        # if we found a .index.json file, make sure there is only one
        if len(index_files) > 0:
            assert (
                len(index_files) == 1
            ), f"Expected to find one .index.json file in {checkpoint_path}, but found {len(index_files)}"

        if len(index_files) == 1:
            return True, index_files[0]
        else:
            return False, None
    else:
        raise RuntimeError(f"Invalid checkpoint path {checkpoint_path}. Expected a file or a directory.")


def load_state_dict(checkpoint_file_path: Path):
    """
    Load state dict from checkpoint.

    Args:
        checkpoint_file_path (Path): path to the checkpoint file.

    Returns:
        dict: state dict.
    """

    assert not is_dtensor_checkpoint(
        checkpoint_file_path
    ), f"Cannot load state dict from dtensor checkpoint {checkpoint_file_path}, you should convert the distributed tensors to gathered tensors with our CLI offline."

    if is_safetensor_checkpoint(checkpoint_file_path):
        assert (
            is_safetensors_available()
        ), f"Cannot load state dict from safetensor checkpoint {checkpoint_file_path}, because safetensors is not available. Please install safetensors first with pip install safetensors."
        # load with safetensors
        from safetensors import safe_open

        state_dict = {}
        with safe_open(checkpoint_file_path, framework="pt", device="cpu") as f:
            for k in f.keys():
                state_dict[k] = f.get_tensor(k)
        return state_dict

    else:
        # load with torch
        return torch.load(checkpoint_file_path, map_location=torch.device("cpu"))


def add_prefix(weights_name: str, prefix: Optional[str] = None) -> str:
    if prefix is not None and len(prefix) > 0:
        splits = weights_name.split(".")
        splits = splits[:-1] + [prefix] + splits[-1:]
        weights_name = ".".join(splits)

    return weights_name


def get_model_base_filenames(prefix: str = None, use_safetensors: bool = False):
    """
    generate base model weight filenames
    """
    weights_name = SAFE_WEIGHTS_NAME if use_safetensors else WEIGHTS_NAME
    weights_name = add_prefix(weights_name, prefix)

    save_index_file = SAFE_WEIGHTS_INDEX_NAME if use_safetensors else WEIGHTS_INDEX_NAME
    save_index_file = add_prefix(save_index_file, prefix)

    return weights_name, save_index_file


def get_optimizer_base_filenames(prefix: str = None, use_safetensors: bool = False):
    """
    generate base optimizer state filenames
    """
    states_name = SAFE_STATE_NAME if use_safetensors else STATES_NAME
    states_name = add_prefix(states_name, prefix)

    save_index_file = SAFE_STATES_INDEX_NAME if use_safetensors else STATES_INDEX_NAME
    save_index_file = add_prefix(save_index_file, prefix)

    param_group_file = GROUP_FILE_NAME
    param_group_file = add_prefix(param_group_file, prefix)

    return states_name, save_index_file, param_group_file


def get_shard_filename(weights_name: str, idx: int):
    """
    get shard file name
    """
    shard_file = weights_name.replace(".bin", f"-{idx+1:05d}.bin")
    shard_file = shard_file.replace(".safetensors", f"-{idx+1:05d}.safetensors")
    return shard_file


def _pin_tensor(tensor: torch.Tensor, empty: bool = True) -> torch.Tensor:
    if empty:
        return torch.empty_like(tensor, pin_memory=True, device="cpu")
    return tensor.pin_memory()


def create_pinned_state_dict(
    state_dict: Union[Dict[str, torch.Tensor], Dict[int, Dict[str, torch.Tensor]]],
    empty: bool = True,
    num_threads: int = 1,
) -> Dict[str, torch.Tensor]:
    if num_threads == 1:
        return tree_map(lambda x: _pin_tensor(x, empty=empty) if isinstance(x, torch.Tensor) else x, state_dict)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            elems, spec = tree_flatten(state_dict)
            future_to_idx = {}
            for i, elem in enumerate(elems):
                if isinstance(elem, torch.Tensor):
                    future_to_idx[executor.submit(_pin_tensor, elem, empty)] = i
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                elems[idx] = future.result()
            return tree_unflatten(elems, spec)


def load_optim_or_model_shard(path: str, is_optim: bool, use_safetensors: bool) -> dict:
    if is_optim:
        if path.endswith(".safetensors"):
            state_dict = load_flat(path)
        else:
            state_dict = load_shard_state_dict(Path(path), use_safetensors=False)
    else:
        state_dict = load_shard_state_dict(Path(path), use_safetensors)
    return state_dict


def load_state_dict_shards(
    checkpoint_files: List[str],
    is_optim: bool,
    use_safetensors: bool,
    low_cpu_mem_mode: bool = True,
    prefetch: int = 3,
) -> Generator[dict, None, None]:
    if low_cpu_mem_mode:
        for shard_file in checkpoint_files:
            state_dict = load_optim_or_model_shard(shard_file, is_optim, use_safetensors)
            yield state_dict
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=prefetch) as executor:
            futures = []
            for shard_file in checkpoint_files:
                future = executor.submit(load_optim_or_model_shard, shard_file, is_optim, use_safetensors)
                futures.append(future)
            for future in concurrent.futures.as_completed(futures):
                yield future.result()


# adapted from `peft/utils/save_and_load.py`
def get_lora_state_dict(
    model: PeftModel, state_dict: dict, adapter_name="default", save_embedding_layers="auto"
) -> dict:
    config = model.peft_config[adapter_name]
    if config.peft_type != PeftType.LORA:
        raise ValueError(f"Adapter {adapter_name} is not a LORA adapter.")
    # to_return = lora_state_dict(model, bias=model.peft_config.bias)
    # adapted from `https://github.com/microsoft/LoRA/blob/main/loralib/utils.py`
    # to be used directly with the state dict which is necessary when using DeepSpeed or FSDP
    bias = config.bias
    if bias == "none":
        to_return = {k: state_dict[k] for k in state_dict if "lora_" in k}
    elif bias == "all":
        to_return = {k: state_dict[k] for k in state_dict if "lora_" in k or "bias" in k}
    elif bias == "lora_only":
        to_return = {}
        for k in state_dict:
            if "lora_" in k:
                to_return[k] = state_dict[k]
                bias_name = k.split("lora_")[0] + "bias"
                if bias_name in state_dict:
                    to_return[bias_name] = state_dict[bias_name]
    else:
        raise NotImplementedError
    to_return = {k: v for k, v in to_return.items() if (("lora_" in k and adapter_name in k) or ("bias" in k))}
    if config.use_dora:
        # Here we take care of a refactor of DoRA which changed lora_magnitude_vector from a ParameterDict to a
        # ModuleDict with a DoraLayer instance. The old parameter is now the "weight" attribute of that layer. Since
        # we want the state_dict format not to change, we remove the "weight" part.
        new_dora_suffix = f"lora_magnitude_vector.{adapter_name}.weight"

        def renamed_dora_weights(k):
            if k.endswith(new_dora_suffix):
                k = k[:-7]  # remove ".weight"
            return k

        to_return = {renamed_dora_weights(k): v for k, v in to_return.items()}

    # DEAL WITH EMBEDDINGS
    # check the common embedding layers in `target_modules` to reset `save_embedding_layers` if necessary
    is_embedding_in_target_modules = False
    if (
        save_embedding_layers == "auto"
        and hasattr(config, "target_modules")
        and any(k in config.target_modules for k in EMBEDDING_LAYER_NAMES)
    ):
        warnings.warn("Setting `save_embedding_layers` to `True` as embedding layers found in `target_modules`.")
        save_embedding_layers = is_embedding_in_target_modules = True
    elif save_embedding_layers == "auto":
        vocab_size = getattr(getattr(model, "config", None), "vocab_size", None)
        model_id = getattr(config, "base_model_name_or_path", None)

        # For some models e.g. diffusers the text config file is stored in a subfolder
        # we need to make sure we can download that config.
        has_base_config = False

        # ensure that this check is not performed in HF offline mode, see #1452
        if model_id is not None:
            local_config_exists = os.path.exists(os.path.join(model_id, "config.json"))
            exists = local_config_exists or check_file_exists_on_hf_hub(model_id, "config.json")
            if exists is None:
                # check failed, could not determine if it exists or not
                warnings.warn(
                    f"Could not find a config file in {model_id} - will assume that the vocabulary was not modified."
                )
                has_base_config = False
            else:
                has_base_config = exists

        # check if the vocab size of the base model is different from the vocab size of the finetuned model
        if (
            vocab_size
            and model_id
            and has_base_config
            and (vocab_size != model.config.__class__.from_pretrained(model_id).vocab_size)
        ):
            warnings.warn(
                "Setting `save_embedding_layers` to `True` as the embedding layer has been resized during finetuning."
            )
            save_embedding_layers = True
        else:
            save_embedding_layers = False

    if save_embedding_layers and hasattr(model, "get_input_embeddings"):
        for layer in [model.get_input_embeddings(), model.get_output_embeddings()]:
            if not is_embedding_in_target_modules or has_valid_embedding_base_layer(layer):
                # support from version >= 0.6.2
                embedding_module_name = get_embedding_layer_name(model, layer, is_embedding_in_target_modules)
                if embedding_module_name:
                    to_return.update({k: v for k, v in state_dict.items() if embedding_module_name in k})
    elif save_embedding_layers:
        warnings.warn("Could not identify embedding layer(s) because the model is not a 🤗 transformers model.")

    return to_return


def gather_state_dict_fast(
    state_dict: Dict[str, torch.Tensor],
    group: dist.ProcessGroup,
    device: Optional[Union[torch.device, str]] = None,
    dst: int = 0,
) -> Optional[Dict[str, torch.Tensor]]:
    if device is None:
        device = get_current_device()
    rank = dist.get_rank(group)
    world_size = dist.get_world_size(group)
    metadata = [(k, v.shape, v.dtype) for k, v in state_dict.items()]
    all_meta_data = [None] * world_size
    if rank == dst:
        returned_state_dict = state_dict.copy()
        dist.gather_object(metadata, all_meta_data, dst=dist.get_global_rank(group, rank), group=group)
        for i, target_metadata in enumerate(all_meta_data):
            if i == dst:
                continue
            ops = []
            for k, shape, dtype in target_metadata:
                buffer = torch.empty(shape, dtype=dtype, device=get_current_device())
                returned_state_dict[k] = buffer
                ops.append(dist.P2POp(dist.irecv, buffer, dist.get_global_rank(group, i), group))
            reqs = dist.batch_isend_irecv(ops)
            for req, (k, *_) in zip(reqs, target_metadata):
                req.wait()
                returned_state_dict[k] = returned_state_dict[k].to(device)
        return returned_state_dict
    else:
        dist.gather_object(metadata, dst=dist.get_global_rank(group, dst), group=group)
        ops = []
        for k, *_ in metadata:
            ops.append(dist.P2POp(dist.isend, state_dict[k], dist.get_global_rank(group, dst), group))
        reqs = dist.batch_isend_irecv(ops)
        for req in reqs:
            req.wait()
