import ctypes
import random
from collections import defaultdict
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from functools import partial
from types import MethodType
from typing import Any, Callable, Dict, Iterator, List, Optional, OrderedDict, Tuple, Union

import numpy as np
import torch
import torch.distributed as dist
from peft import PeftModel
from torch import Tensor, inf
from torch.distributed import ProcessGroup, get_world_size
from torch.nn import Module, SyncBatchNorm
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.optim import Optimizer
from torch.optim.lr_scheduler import _LRScheduler as LRScheduler
from torch.utils._pytree import tree_map
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler

from colossalai.accelerator import get_accelerator
from colossalai.amp.naive_amp.mixed_precision_optimizer import MixedPrecisionOptimizer
from colossalai.checkpoint_io import CheckpointIO, HybridParallelCheckpointIO
from colossalai.cluster import ProcessGroupMesh
from colossalai.interface import AMPModelMixin, ModelWrapper, OptimizerWrapper
from colossalai.interface.model import PeftUnwrapMixin
from colossalai.interface.optimizer import DistributedOptim
from colossalai.logging import get_dist_logger
from colossalai.nn.optimizer import DistGaloreAwamW, cast_to_distributed
from colossalai.pipeline.schedule import InterleavedSchedule, OneForwardOneBackwardSchedule, ZeroBubbleVPipeScheduler
from colossalai.pipeline.stage_manager import PipelineStageManager
from colossalai.quantization import BnbQuantizationConfig, quantize_model
from colossalai.quantization.fp8_hook import FP8Hook
from colossalai.shardformer import GradientCheckpointConfig, ShardConfig, ShardFormer
from colossalai.shardformer.layer.utils import SeqParallelUtils, is_share_sp_tp
from colossalai.shardformer.policies.base_policy import Policy
from colossalai.tensor.colo_parameter import ColoParameter
from colossalai.tensor.d_tensor.api import is_distributed_tensor
from colossalai.tensor.param_op_hook import ColoParamOpHookManager
from colossalai.zero.low_level import LowLevelZeroOptimizer
from colossalai.zero.low_level.zero_hook import ZeroOpHook, wait_all_gather_handle

from .pp_plugin_base import PipelinePluginBase

SUPPORT_SP_MODE = ["split_gather", "ring", "all_to_all", "ring_attn"]

PRECISION_TORCH_TYPE = {"fp16": torch.float16, "fp32": torch.float32, "bf16": torch.bfloat16}


def _convert_floating_point(x, dtype: torch.dtype = torch.float16):
    if isinstance(x, torch.Tensor) and torch.is_floating_point(x):
        return x.to(dtype)
    return x


class HybridParallelModule(ModelWrapper, AMPModelMixin):
    def __init__(
        self,
        module: Module,
        precision: str,
        shard_config: ShardConfig,
        dp_group: ProcessGroup,
        tp_group: ProcessGroup,
        sp_group: ProcessGroup,
        use_ddp: bool,
        ddp_config: dict,
        custom_policy: Policy,
        overlap_allgather: bool = False,
        use_fp8: bool = False,
    ) -> None:
        self.stage_manager = shard_config.pipeline_stage_manager
        self.shard_config = shard_config
        self.dp_group = dp_group
        self.tp_group = tp_group
        self.sp_group = sp_group
        self.use_ddp = use_ddp
        self.require_grad_sync = True
        self.overlap_allgather = overlap_allgather
        self.use_fp8 = use_fp8

        shardformer = ShardFormer(shard_config)
        if custom_policy is not None:
            assert isinstance(custom_policy, object)
        module, self.shared_params = shardformer.optimize(module, policy=custom_policy)

        # setting process groups for shared parameters
        self.shared_param_process_groups = []
        for shared_param in self.shared_params:
            if len(shared_param) > 0:
                self.shared_param_process_groups.append(
                    self.stage_manager.init_process_group_by_stages(list(shared_param.keys()))
                )

        # setting mixed_precision
        self.mixed_precision = None
        if precision == "fp16":
            self.mixed_precision = torch.float16
        elif precision == "bf16":
            self.mixed_precision = torch.bfloat16
        if self.mixed_precision is not None:
            module = module.to(self.mixed_precision)
        module = module.to(get_accelerator().get_current_device())

        # setting input type cast when using mixed precision
        self.convert_fn = None
        if self.mixed_precision is not None:
            self.convert_fn = partial(_convert_floating_point, dtype=self.mixed_precision)

        # setting ddp configs
        if use_ddp:
            # convert model to sync bn
            module = SyncBatchNorm.convert_sync_batchnorm(module, dp_group)
            # wrap the model with PyTorch DDP
            module = DDP(module, process_group=dp_group, **ddp_config)

        super().__init__(module)
        self.op_hooks = []
        if use_fp8:
            self.op_hooks.append(FP8Hook())
        if overlap_allgather:
            self.op_hooks.append(ZeroOpHook())
        if use_fp8 or overlap_allgather:
            for p in module.parameters():
                if p.requires_grad and type(p) is not ColoParameter:
                    p.__class__ = ColoParameter
                    p.__init__(p, requires_grad=True)

    def sync_shared_params(self):
        for shared_param, group in zip(self.shared_params, self.shared_param_process_groups):
            if self.stage_manager.stage in shared_param:
                param = shared_param[self.stage_manager.stage]
                dist.all_reduce(param.grad, group=group)
            dist.barrier()

    @contextmanager
    def no_sync(self):
        r"""
        A context manager to disable automatic gradient synchronization (all-reduce) and allow manual synchronization
        when 'no_sync' is active. Alternatively, synchronization will occur in the first forward-backward pass
        when exiting the context.
        """

        # Store the current value of 'require_grad_sync' to restore it later.
        old_require_grad_sync = self.require_grad_sync
        # Disable automatic gradient synchronization.
        self.require_grad_sync = False
        try:
            if self.use_ddp:
                # If using data parallel processing (use_ddp), disable synchronization too.
                with self.module.no_sync():
                    yield
            else:
                yield
        finally:
            # Restore the original value of 'require_grad_sync'.
            self.require_grad_sync = old_require_grad_sync

    def sync_dp_grads(self):
        r"""
        Synchronize gradients across data parallelism (DP) if the DP group size is greater than 1.
        This function performs an all-reduce operation to combine gradients from different devices in the DP group.

        Args:
            None

        Returns:
            None
        """

        # Check if the DP group size is 1, meaning no synchronization is needed.
        if self.dp_group.size() == 1:
            return

        # Iterate through the model's parameters and perform gradient synchronization.
        for p in self.module.parameters():
            if p.grad is not None:
                # Perform all-reduce to combine gradients from different devices.
                dist.all_reduce(p.grad, group=self.dp_group)
                # Normalize the gradient by dividing it by the DP group size.
                p.grad.div_(self.dp_group.size())

    def sync_sp_grads(self, grads: Optional[List[torch.Tensor]] = None):
        r"""
        Synchronize gradients that are partially derived within sequence parallelism
        if sequence parallelism is enabled. Gradients can be provided explicitly or extracted
        from the module.

        Args:
            grads (Optional[List[torch.Tensor]]): A list of gradient tensors to synchronize. If not
                provided, gradients will be extracted from the model.

        Returns:
            None
        """

        if self.shard_config.enable_sequence_parallelism:
            if self.shard_config.sequence_parallelism_mode in ["all_to_all", "ring_attn"]:
                return

            if self.shard_config.sequence_parallelism_mode in ["split_gather", "ring"]:
                # If sequence parallelism is enabled and mode is split_gather or ring, gradients are synchronized
                # across the tensor parallelism group.
                group = self.tp_group
            else:
                raise ValueError(f"Unknown sequence parallelism mode: {self.shard_config.sequence_parallelism_mode}")

            if grads is not None:
                # Synchronize provided gradient tensors across the tensor parallelism group.
                SeqParallelUtils.allreduce_partial_data_grad(process_group=group, grads=grads)
            else:
                # Synchronize gradients from the model across the tensor parallelism group.
                SeqParallelUtils.allreduce_partial_data_grad(process_group=group, model=self.module)

    def forward(self, *args, **kwargs):
        if self.convert_fn is not None:
            args = tree_map(self.convert_fn, args)
            kwargs = tree_map(self.convert_fn, kwargs)
        with self._hook_context():
            return super().forward(*args, **kwargs)

    def unwrap(self, unwrap_peft: bool = True):
        model = self.module
        if isinstance(model, DDP):
            model = model.module
        if unwrap_peft and isinstance(model, PeftModel):
            model = PeftUnwrapMixin(model)
        return model

    def _force_wait_all_gather(self):
        for p in self.module.parameters():
            wait_all_gather_handle(p)

    def _hook_context(self):
        return ColoParamOpHookManager.use_hooks(*self.op_hooks) if len(self.op_hooks) > 0 else nullcontext()


def get_param_info(optim: Optimizer):
    # Get a backup of necessary information of parameters for future use, which includes:
    # 1. A complete param_group, with params in the form of param_id
    # 2. A mapping from param address (obtained using id(param)) to integer param_id
    # 3. A mapping from integer param_id to param address.
    # 4. A mapping from param_address (obtained using id(param)) to the original shape of parameter before sharding.
    # When Zero is used, the params here are fp16/bf16 model params rather than fp32 master params in optimizer.

    if optim is None:
        return {}
    param_info = {"param_groups": [], "param2id": {}, "id2param": {}, "param2shape": {}}
    start_index = 0
    for group in optim.param_groups:
        packed_group = {k: v for k, v in group.items() if k != "params"}
        packed_group["params"] = []

        for param_id, param in enumerate(group["params"], start_index):
            original_shape = param.shape if isinstance(param, torch.Tensor) else None
            packed_group["params"].append(param_id)
            param_info["param2id"][id(param)] = param_id
            param_info["id2param"][param_id] = id(param)
            param_info["param2shape"][id(param)] = original_shape

        param_info["param_groups"].append(packed_group)
        start_index += len(group["params"])

    return param_info


def reinitialize_optimizer(optim: Optimizer, model: Module):
    model_params = set(model.parameters())
    new_param_groups = []
    for group in optim.param_groups:
        params = [p for p in group["params"] if p in model_params]
        new_param_groups.append({**group, "params": params})
    optim.__setstate__({"param_groups": new_param_groups})


class HybridParallelNaiveOptimizer(OptimizerWrapper):
    def __init__(
        self,
        optim: Optimizer,
        model: HybridParallelModule,
        use_pipeline: bool,
        param_info: OrderedDict,
        max_norm: float = 0,
        tp_process_group: Optional[ProcessGroup] = None,  # if using tp
        pp_process_group: Optional[ProcessGroup] = None,  # if using pp
    ):
        self.param_info = param_info
        if use_pipeline:
            reinitialize_optimizer(optim, model)
        self.model = model
        self.stage_manager = model.stage_manager
        self.shared_params = model.shared_params
        self.max_norm = max_norm
        self.tp_pg = tp_process_group
        self.pp_pg = pp_process_group
        self.tp_size = get_world_size(self.tp_pg) if self.tp_pg is not None else 1
        self.pp_size = get_world_size(self.pp_pg) if self.pp_pg is not None else 1
        self._current_grad_norm: Optional[float] = None
        super().__init__(optim)

    def backward(self, loss: Tensor, inputs=None, retain_graph=False, **kwargs):
        r"""
        Backpropagate gradients through the model and optionally synchronize sequence parallelism gradients.

        This method performs backward pass for gradient computation. If sequence parallelism is enabled
        and gradient synchronization is required, it will synchronize gradients that are partially derived
        within sequence parallelism across tp parallelism groups.

        Args:
            loss (Tensor): The loss tensor to compute gradients with respect to.
            *args: Additional positional arguments to be passed to the superclass backward method.
            **kwargs: Additional keyword arguments to be passed to the superclass backward method.

        Returns:
            None
        """

        # Call the superclass backward method to compute gradients.
        with self.model._hook_context():
            super().backward(loss, inputs=inputs, retain_graph=retain_graph, **kwargs)

        if self.model.require_grad_sync:
            # If gradient synchronization is required, sync sequence parallelism gradients.
            self.model.sync_sp_grads()
        else:
            # If gradient synchronization is is not required, return.
            return

    def backward_by_grad(self, tensor: Tensor, grad: Tensor, inputs: Tensor = None, retain_graph: bool = False):
        """
        Backpropagate gradients through the model using a precomputed gradient and optionally synchronize sequence parallelism gradients.

        This method performs a backward pass for gradient computation using a precomputed gradient tensor.
        If sequence parallelism is enabled and gradient synchronization is required, it will synchronize
        gradients that are partially derived within sequence parallelism across tp parallelism groups.

        Args:
            tensor (Tensor): The input tensor for which gradients are computed.
            grad (Tensor): The precomputed gradient tensor to compute gradients with respect to the input tensor.

        Returns:
            None
        """

        # Call the superclass backward method to compute gradients.
        super().backward_by_grad(tensor, grad, inputs=inputs, retain_graph=retain_graph)

        if self.model.require_grad_sync:
            # If gradient synchronization is required, sync sequence parallelism gradients.
            self.model.sync_sp_grads()
        else:
            # If gradient synchronization is is not required, return.
            return

    def step(self, *args, **kwargs):
        r"""
        Perform an optimization step.

        Args:
            *args: Variable-length positional arguments to be passed to the optimizer's step function.
            **kwargs: Keyword arguments to be passed to the optimizer's step function.
        """

        if self.max_norm > 0:
            # Compute the total gradient norm.
            param_gradient_pairs = [
                (p, p.grad) for group in self.optim.param_groups for p in group["params"] if p.grad is not None
            ]
            total_norm = self._compute_grad_norm(param_gradient_pairs)
            self._current_grad_norm = total_norm

            # Clip the gradients to prevent exploding gradients.
            self._clip_grad_norm(total_norm)

        # Perform the optimization step using the underlying optimizer.
        self.optim.step(*args, **kwargs)

    def _compute_grad_norm(self, param_gradient_pairs: List[Tuple[Tensor]], norm_type: int = 2) -> int:
        r"""
        Compute and return the gradient norm for gradient clipping.

        Args:
            param_gradient_pairs (List[Tuple[Tensor]]): List of (parameter, gradient) pairs; gradients are used for norm calculation.
            norm_type (int, optional): Type of the norm used (e.g., 2 for L2 norm). Defaults to 2.

        Returns:
            float: The total norm of the given gradients.
        """

        if len(param_gradient_pairs) == 0:
            return 0.0

        norm_type = float(norm_type)

        # gradients used for norm calculation.
        gradients = [grad for param, grad in param_gradient_pairs]

        if norm_type == inf:
            total_norm = max(grad.data.abs().max() for grad in gradients)
            total_norm_cuda = torch.tensor(
                [float(total_norm)], device=get_accelerator().get_current_device(), dtype=torch.float32
            )
            if self.tp_size > 1:
                dist.all_reduce(tensor=total_norm_cuda, op=dist.ReduceOp.MAX, group=self.tp_pg)
            if self.pp_size > 1:
                dist.all_reduce(tensor=total_norm_cuda, op=dist.ReduceOp.MAX, group=self.pp_pg)
            total_norm = total_norm_cuda.item()
        else:
            # gradients used for norm calculation.
            gradients = [grad for param, grad in param_gradient_pairs]
            # grad_to_param_mapping is used to check which gradients are not distributed across devices of the 'tp_group'.
            grad_to_param_mapping = {id(grad): param for param, grad in param_gradient_pairs}

            total_norm_exponentiated = 0.0
            for grad in gradients:
                grad_norm_exponentiated = grad.data.double().norm(norm_type) ** norm_type

                # If 'tp_size' is greater than 1 and the parameter for the gradient is not a distributed tensor,
                # it indicates that the parameter is not distributed across devices of the 'tp_group'.
                # Consequently, there is no need to perform an 'all_reduce' operation for 'grad_norm'.
                # However, we still perform the 'all_reduce' operation for the sake of good coding practices.
                # To ensure mathematical equivalence, we divide the 'grad_norm' by 'tp_size.'
                if self.tp_size > 1:
                    param_for_grad = grad_to_param_mapping[id(grad)]
                    if not is_distributed_tensor(param_for_grad):
                        grad_norm_exponentiated /= self.tp_size

                # If 'pp_size' is greater than 1 and the gradient belongs to shared parameters,
                # it means that this parameter is used in two different pipeline stages.
                # To avoid redundant norm calculations, we divide the exponent of this norm by
                # the number of shared stages.
                if self.pp_size > 1:
                    for shared_param in self.shared_params:
                        if self.stage_manager.stage in shared_param:
                            stage_shared_param = shared_param[self.stage_manager.stage]
                            if grad is stage_shared_param.grad:
                                grad_norm_exponentiated /= len(shared_param)

                total_norm_exponentiated += grad_norm_exponentiated

            total_norm_exponentiated_cuda = torch.tensor(
                [float(total_norm_exponentiated)], device=get_accelerator().get_current_device(), dtype=torch.float32
            )
            if self.tp_size > 1:
                # compute norm in tp process group
                dist.all_reduce(tensor=total_norm_exponentiated_cuda, op=dist.ReduceOp.SUM, group=self.tp_pg)
            if self.pp_size > 1:
                # compute norm in pp process group
                dist.all_reduce(tensor=total_norm_exponentiated_cuda, op=dist.ReduceOp.SUM, group=self.pp_pg)

            # compute the total_norm
            total_norm = total_norm_exponentiated_cuda.item() ** (1.0 / norm_type)

        return total_norm

    def _clip_grad_norm(self, total_norm: float) -> None:
        r"""
        Clips the gradients of the model's parameters to prevent exploding gradients.

        Args:
            total_norm (float): The computed total gradient norm.

        Returns:
            None
        """
        clip_coef = torch.tensor(self.max_norm / (total_norm + 1e-6))
        clip_coef_clamped = torch.clamp(clip_coef, max=1.0)

        for group in self.optim.param_groups:
            for p in group["params"]:
                if p.grad is None:
                    continue
                p.grad.data.mul_(clip_coef_clamped)

    def update_master_params(self, model: Module):
        pass

    def get_working_to_master_map(self):
        return None

    def get_master_to_working_map(self):
        return None

    def get_grad_norm(self, norm_type=2, **kwargs):
        return self._current_grad_norm


class HybridParallelAMPOptimizer(MixedPrecisionOptimizer):
    def __init__(
        self,
        optim: Optimizer,
        model: HybridParallelModule,
        use_pipeline: bool,
        param_info: OrderedDict,
        precision: str = "fp16",
        initial_scale: float = 2**16,
        min_scale: float = 1,
        growth_factor: float = 2,
        backoff_factor: float = 0.5,
        growth_interval: int = 1000,
        hysteresis: int = 2,
        max_scale: float = 2**32,
        max_norm: float = 0,
        tp_process_group: Optional[ProcessGroup] = None,  # if using tp
        pp_process_group: Optional[ProcessGroup] = None,  # if using pp
    ):
        self.model = model
        self.param_info = param_info
        self.stage_manager = model.stage_manager
        self.shared_params = model.shared_params
        self.tp_pg = tp_process_group
        self.pp_pg = pp_process_group
        self.tp_size = get_world_size(self.tp_pg) if self.tp_pg is not None else 1
        self.pp_size = get_world_size(self.pp_pg) if self.pp_pg is not None else 1
        if use_pipeline:
            reinitialize_optimizer(optim, model)
        super().__init__(
            optim,
            precision=precision,
            initial_scale=initial_scale,
            min_scale=min_scale,
            growth_factor=growth_factor,
            backoff_factor=backoff_factor,
            growth_interval=growth_interval,
            hysteresis=hysteresis,
            max_scale=max_scale,
            max_norm=max_norm,
        )

    def backward(self, loss: Tensor, inputs=None, retain_graph=False, **kwargs):
        r"""
        Backpropagate gradients through the model and optionally synchronize sequence parallelism gradients.

        This method performs backward pass for gradient computation. If sequence parallelism is enabled
        and gradient synchronization is required, it will synchronize gradients that are partially derived
        within sequence parallelism across tp parallelism groups.

        Args:
            loss (Tensor): The loss tensor to compute gradients with respect to.
            *args: Additional positional arguments to be passed to the superclass backward method.
            **kwargs: Additional keyword arguments to be passed to the superclass backward method.

        Returns:
            None
        """
        # Call the superclass backward method to compute gradients.
        with self.model._hook_context():
            super().backward(loss, inputs=inputs, retain_graph=retain_graph, **kwargs)

        if self.model.require_grad_sync:
            # If gradient synchronization is required, sync sequence parallelism gradients.
            self.model.sync_sp_grads()
        else:
            # If gradient synchronization is is not required, return.
            return

    def backward_by_grad(self, tensor: Tensor, grad: Tensor, inputs: Tensor = None, retain_graph: bool = False):
        """
        Backpropagate gradients through the model using a precomputed gradient and optionally synchronize sequence parallelism gradients.

        This method performs a backward pass for gradient computation using a precomputed gradient tensor.
        If sequence parallelism is enabled and gradient synchronization is required, it will synchronize
        gradients that are partially derived within sequence parallelism across tp parallelism groups.

        Args:
            tensor (Tensor): The input tensor for which gradients are computed.
            grad (Tensor): The precomputed gradient tensor to compute gradients with respect to the input tensor.

        Returns:
            None
        """
        # Call the superclass backward method to compute gradients.
        super().backward_by_grad(tensor, grad, inputs=inputs, retain_graph=retain_graph)

        if self.model.require_grad_sync:
            # If gradient synchronization is required, sync sequence parallelism gradients.
            self.model.sync_sp_grads()
        else:
            # If gradient synchronization is is not required, return.
            return

    def _compute_grad_norm(self, param_gradient_pairs: List[Tuple[Tensor]], norm_type: int = 2) -> int:
        r"""
        Compute and return the gradient norm for gradient clipping.

        Args:
            param_gradient_pairs (List[Tuple[Tensor]]): List of (parameter, gradient) pairs; gradients are used for norm calculation.
            norm_type (int, optional): Type of the norm used (e.g., 2 for L2 norm). Defaults to 2.

        Returns:
            float: The total norm of the given gradients.
        """
        if len(param_gradient_pairs) == 0:
            return 0.0

        norm_type = float(norm_type)

        if norm_type == inf:
            # The parent class calculates the norm of 'dp' gradients,
            # so we need to calculate the norm of 'tp' and 'pp' gradients.
            total_norm = super()._compute_grad_norm(param_gradient_pairs, norm_type)

            total_norm_cuda = torch.tensor(
                [float(total_norm)], device=get_accelerator().get_current_device(), dtype=torch.float32
            )

            if self.tp_size > 1:
                dist.all_reduce(tensor=total_norm_cuda, op=dist.ReduceOp.MAX, group=self.tp_pg)
            if self.pp_size > 1:
                dist.all_reduce(tensor=total_norm_cuda, op=dist.ReduceOp.MAX, group=self.pp_pg)

            total_norm = total_norm_cuda.item()

        else:
            # gradients used for norm calculation.
            gradients = [grad for param, grad in param_gradient_pairs]
            # grad_to_param_mapping is used to check which gradients are not distributed in tensor parallelism.
            grad_to_param_mapping = {id(grad): param for param, grad in param_gradient_pairs}

            total_norm_exponentiated = 0.0
            for grad in gradients:
                grad_norm_exponentiated = grad.data.double().norm(norm_type) ** norm_type

                # If 'tp_size' is greater than 1 and the parameter for the gradient is not a distributed tensor,
                # it indicates that the parameter is not distributed across devices of the 'tp_group'.
                # Consequently, there is no need to perform an 'all_reduce' operation for 'grad_norm'.
                # However, we still perform the 'all_reduce' operation for the sake of good coding practices.
                # To ensure mathematical equivalence, we divide the 'grad_norm' by 'tp_size.'
                if self.tp_size > 1:
                    param_for_grad = grad_to_param_mapping[id(grad)]
                    if not is_distributed_tensor(param_for_grad):
                        grad_norm_exponentiated /= self.tp_size

                # If 'pp_size' is greater than 1 and the gradient belongs to shared parameters,
                # it means that this parameter is used in two different pipeline stages.
                # To avoid redundant norm calculations, we divide the exponent of this norm by
                # the number of shared stages.
                if self.pp_size > 1:
                    for shared_param in self.shared_params:
                        if self.stage_manager.stage in shared_param:
                            stage_working_shared_param = shared_param[self.stage_manager.stage]
                            stage_master_shared_param = self.working_to_master_map[stage_working_shared_param]
                            if grad is stage_master_shared_param.grad:
                                grad_norm_exponentiated /= len(shared_param)

                total_norm_exponentiated += grad_norm_exponentiated

            total_norm_exponentiated_cuda = torch.tensor(
                [float(total_norm_exponentiated)], device=get_accelerator().get_current_device(), dtype=torch.float32
            )
            if self.tp_size > 1:
                # compute norm in tp process group
                dist.all_reduce(tensor=total_norm_exponentiated_cuda, op=dist.ReduceOp.SUM, group=self.tp_pg)
            if self.pp_size > 1:
                # compute norm in pp process group
                dist.all_reduce(tensor=total_norm_exponentiated_cuda, op=dist.ReduceOp.SUM, group=self.pp_pg)

            # compute the total_norm
            total_norm = total_norm_exponentiated_cuda.item() ** (1.0 / norm_type)

        return total_norm


class HybridParallelZeroOptimizer(LowLevelZeroOptimizer):
    def __init__(
        self,
        optimizer: Optimizer,
        model: HybridParallelModule,
        use_pipeline: bool,
        param_info: OrderedDict,
        pg_to_param_list: Dict[ProcessGroup, List[torch.nn.Parameter]] = None,
        initial_scale: int = 2**16,  # grad scaler config
        min_scale: int = 1,
        growth_factor: float = 2.0,
        backoff_factor: float = 0.5,
        growth_interval: int = 2000,
        hysteresis: int = 2,
        max_scale: int = 2**24,
        clip_grad_norm: float = 0.0,  # grad clipping
        verbose: bool = False,
        reduce_bucket_size: int = 1024 * 1024,  # communication
        communication_dtype: Optional[torch.dtype] = None,
        overlap_communication: bool = True,
        partition_grad: bool = False,  # stage 2 flag
        cpu_offload: bool = False,  # cpu offload
        dp_process_group: Optional[ProcessGroup] = None,  # the dp pg for comm
        tp_process_group: Optional[ProcessGroup] = None,  # if using tp
        pp_process_group: Optional[ProcessGroup] = None,  # if using pp
        forced_dtype: Optional[torch.dtype] = None,
        overlap_allgather: bool = False,
        fp8_communication: bool = False,
    ):
        self.model = model
        self.param_info = param_info
        self.stage_manager = model.stage_manager
        self.shared_params = model.shared_params
        self.tp_pg = tp_process_group
        self.pp_pg = pp_process_group
        if use_pipeline:
            reinitialize_optimizer(optimizer, model)
        super().__init__(
            optimizer=optimizer,
            initial_scale=initial_scale,
            min_scale=min_scale,
            pg_to_param_list=pg_to_param_list,
            growth_factor=growth_factor,
            backoff_factor=backoff_factor,
            growth_interval=growth_interval,
            hysteresis=hysteresis,
            max_scale=max_scale,
            clip_grad_norm=clip_grad_norm,
            verbose=verbose,
            reduce_bucket_size=reduce_bucket_size,
            communication_dtype=communication_dtype,
            overlap_communication=overlap_communication,
            partition_grad=partition_grad,
            cpu_offload=cpu_offload,
            dp_process_group=dp_process_group,
            forced_dtype=forced_dtype,
            overlap_allgather=overlap_allgather,
            fp8_communication=fp8_communication,
            backward_context=model._hook_context,
        )

    def sync_dp_grads(self):
        r"""
        Synchronize gradients in the data parallelism dimension.

        This method wraps the existing `_sync_grad` method in order to explicitly synchronize gradients
        in the data parallelism dimension. It is necessary due to the introduction of new parallel dimensions,
        namely tp (tensor parallelism) and pp (pipeline parallelism). This ensures better code organization
        and readability.

        Args:
            None

        Returns:
            None
        """
        # Call the superclass `_sync_grad` method to synchronize gradients.
        super()._sync_grad()

    def _sync_sp_grads(self):
        r"""
        Synchronize gradients that are partially derived within sequence parallelism.

        This method is responsible for synchronizing partially derived gradients across tp parallelism groups.
        It identifies gradients that ara partially derived or not and synchronizes them.
        If synchronization is required and gradients are found to be synchronized,
        it performs the synchronization.

        Args:
            None

        Returns:
            None
        """

        def _get_all_working_grads() -> List[Tensor]:
            """Retrieve all working gradients from different parameter groups."""
            all_working_grads = []
            for group_id in range(self.num_param_groups):
                working_grads = self.get_working_grads_by_group_id(group_id)
                all_working_grads.extend(working_grads)
            return all_working_grads

        def _get_grads_to_sync(all_working_grads) -> Union[List[Tensor], None]:
            """Identify gradients to be synchronized in the sequence parallelism."""
            grads_to_sync = []
            for grad in all_working_grads:
                param_id_for_grad = self.get_param_id_for_grad(grad)
                param_for_grad = ctypes.cast(param_id_for_grad, ctypes.py_object).value
                if SeqParallelUtils.is_sp_partial_derived_param(param_for_grad):
                    grads_to_sync.append(grad)

            if len(grads_to_sync) > 0:
                return grads_to_sync
            else:
                return None

        # Get all working gradients and gradients to be synchronized.
        all_working_grads = _get_all_working_grads()
        grads_to_sync = _get_grads_to_sync(all_working_grads)
        if self.require_grad_sync and grads_to_sync is not None:
            # Synchronize sequence parallelism gradients if required.
            SeqParallelUtils.allreduce_partial_data_grad(process_group=self.tp_pg, grads=grads_to_sync)
        else:
            return

    def backward(self, loss, inputs=None, retain_graph=False):
        """
        Backpropagate gradients through the model and optionally synchronize sequence parallelism gradients.

        This method performs the backward pass for gradient computation based on a given loss tensor.
        If sequence parallelism is enabled and gradient synchronization is required, it will synchronize
        gradients that are partially derived within sequence parallelism across TP parallelism groups.

        Args:
            loss: The loss tensor to compute gradients with respect to.
            retain_graph (bool): Whether to retain the computation graph.

        Returns:
            None
        """
        # Call the superclass backward method to compute gradients.
        super().backward(loss, inputs=inputs, retain_graph=retain_graph)

        if self.require_grad_sync and self.model.shard_config.enable_sequence_parallelism:
            # If gradient synchronization is required, sync sequence parallelism gradients.
            self._sync_sp_grads()
        else:
            # If gradient synchronization is is not required, return.
            return

    def backward_by_grad(self, tensor, grad, inputs: Tensor = None, retain_graph: bool = False):
        """
        Backpropagate gradients through the model using a precomputed gradient and optionally synchronize sequence parallelism gradients.

        This method performs a backward pass for gradient computation based on a precomputed gradient tensor.
        If sequence parallelism is enabled and gradient synchronization is required, it will synchronize
        gradients that are partially derived within sequence parallelism across TP parallelism groups.

        Args:
            tensor: The input tensor for which gradients are computed.
            grad: The precomputed gradient tensor to compute gradients with respect to the input tensor.

        Returns:
            None
        """
        # Call the superclass backward_by_grad method to compute gradients.
        super().backward_by_grad(tensor, grad, inputs=inputs, retain_graph=retain_graph)

        if self.require_grad_sync and self.model.shard_config.enable_sequence_parallelism:
            # If gradient synchronization is required, sync sequence parallelism gradients.
            self._sync_sp_grads()
        else:
            # If gradient synchronization is is not required, return.
            return

    def _compute_grad_norm(self, dp_pg, gradients: List[Tensor], norm_type: int = 2) -> float:
        r"""
        Compute and return the gradient norm for gradient clipping.

        Args:
            gradients (List[Tensor]): A list of tensors containing gradients.
            norm_type (int, optional): Type of the p-norm to be computed. Defaults to 2.

        Returns:
            float: The computed gradient norm.
        """

        # Check if the list of gradients is empty
        if len(gradients) == 0:
            return 0.0

        dp_size = get_world_size(dp_pg) if dp_pg is not None else 1
        tp_size = get_world_size(self.tp_pg) if self.tp_pg is not None else 1
        pp_size = get_world_size(self.pp_pg) if self.pp_pg is not None else 1
        norm_type = float(norm_type)

        if norm_type == inf:
            # The parent class calculates the norm of 'dp' gradients,
            # so we only need to calculate the norm 'tp' of 'pp' gradients.
            total_norm = super()._compute_grad_norm(gradients, norm_type)

            total_norm_cuda = torch.tensor(
                [float(total_norm)], device=get_accelerator().get_current_device(), dtype=torch.float32
            )

            if tp_size > 1:
                dist.all_reduce(tensor=total_norm_cuda, op=dist.ReduceOp.MAX, group=self.tp_pg)
            if pp_size > 1:
                dist.all_reduce(tensor=total_norm_cuda, op=dist.ReduceOp.MAX, group=self.pp_pg)

            total_norm = total_norm_cuda.item()
        else:
            total_norm_exponentiated = 0.0
            for grad in gradients:
                grad_norm_exponentiated = grad.data.double().norm(norm_type) ** norm_type

                # If 'tp_size' is greater than 1 and the parameter for the gradient is not a distributed tensor,
                # it indicates that the parameter is not distributed across devices of the 'tp_group'.
                # Consequently, there is no need to perform an 'all_reduce' operation for 'grad_norm'.
                # However, we still perform the 'all_reduce' operation for the sake of good coding practices.
                # To ensure mathematical equivalence, we divide the 'grad_norm' by 'tp_size.'
                if tp_size > 1:
                    param_id_for_grad = self.get_param_id_for_grad(grad)
                    param_for_grad = ctypes.cast(param_id_for_grad, ctypes.py_object).value

                    if not is_distributed_tensor(param_for_grad):
                        grad_norm_exponentiated /= tp_size

                # If 'pp_size' is greater than 1 and the gradient belongs to shared parameters,
                # it means that this parameter is used in two different pipeline stages.
                # To avoid redundant norm calculations, we divide the exponent of this norm by
                # the number of shared stages.
                if pp_size > 1:
                    for shared_param in self.shared_params:
                        if self.stage_manager.stage in shared_param:
                            stage_shared_param = shared_param[self.stage_manager.stage]
                            working_grad = self.get_working_grad_by_param_id(id(stage_shared_param))
                            if grad is working_grad:
                                grad_norm_exponentiated /= len(shared_param)

                total_norm_exponentiated += grad_norm_exponentiated

            total_norm_exponentiated_cuda = torch.tensor(
                [float(total_norm_exponentiated)], device=get_accelerator().get_current_device(), dtype=torch.float32
            )
            if dp_size > 1:
                # compute norm in dp process group
                dist.all_reduce(tensor=total_norm_exponentiated_cuda, op=dist.ReduceOp.SUM, group=dp_pg)
            if tp_size > 1:
                # compute norm in tp process group
                dist.all_reduce(tensor=total_norm_exponentiated_cuda, op=dist.ReduceOp.SUM, group=self.tp_pg)
            if pp_size > 1:
                # compute norm in pp process group
                dist.all_reduce(tensor=total_norm_exponentiated_cuda, op=dist.ReduceOp.SUM, group=self.pp_pg)

            # Compute the 'total_norm' from 'total_norm_exponentiated'
            total_norm = total_norm_exponentiated_cuda.item() ** (1.0 / norm_type)

        return total_norm


class HybridParallelPlugin(PipelinePluginBase):
    """
    Plugin for Hybrid Parallel Training.
    Tensor parallel, pipeline parallel and data parallel(DDP/ZeRO) can be picked and combined in this plugin.
    The size of tp and pp should be passed in by user, then the size of dp is automatically calculated from dp_size = world_size / (tp_size * pp_size).

    ```python
    from colossalai.booster import Booster
    from colossalai.booster.plugin import HybridParallelPlugin

    model, train_dataset, optimizer, criterion = ...
    plugin =  HybridParallelPlugin(tp_size=2, pp_size=2)

    train_dataloader = plugin.prepare_dataloader(train_dataset, batch_size=8)
    booster = Booster(plugin=plugin)
    model, optimizer, criterion, train_dataloader, _ = booster.boost(model, optimizer, criterion, train_dataloader)
    ```

    Args:
        tp_size (int): The size of tensor parallelism. Tensor parallelism will not be used when tp_size is set to 1.
        pp_size (int): The number of pipeline stages in pipeline parallelism. Pipeline parallelism will not be used when pp_size is set to 1.
        sp_size (int): The size of sequence parallelism.
        precision (str, optional): Specifies the precision of parameters during training.
                                    Auto-mixied precision will be used when this argument is set to 'fp16' or 'bf16', otherwise model is trained with 'fp32'.
                                    Defaults to 'fp16'.
        zero_stage (int, optional): The stage of ZeRO for data parallelism. Can only be choosed from [0, 1, 2].
                                        When set to 0, ZeRO will not be used. Defaults to 0.
        enable_all_optimization (bool, optional): Whether to switch on all the optimizations supported by Shardformer.
                                                    Currently all the optimization methods include fused normalization, flash attention and JIT.
                                                    Defaults to False.
        enable_fused_normalization (bool, optional): Whether to switch on fused normalization in Shardformer. Defaults to False.
        enable_flash_attention (bool, optional): Whether to switch on flash attention in Shardformer. Defaults to False.
        enable_jit_fused (bool, optional): Whether to switch on JIT in Shardformer. Default to False.
        enable_sequence_parallelism (bool): Whether to turn on sequence parallelism in Shardformer. Defaults to False.
        sequence_parallelism_mode (str): The Sequence parallelism mode. Can only be choosed from ["split_gather", "ring", "all_to_all"]. Defaults to "split_gather".
        parallel_output (bool): Whether to keep the output parallel when enabling tensor parallelism. Default to True.
        num_microbatches (int, optional): Number of microbatches when using pipeline parallelism. Defaults to None.
        microbatch_size (int, optional): Microbatch size when using pipeline parallelism.
            Either ``num_microbatches`` or ``microbatch_size`` should be provided if using pipeline.
            If ``num_microbatches`` is provided, this will be ignored. Defaults to None.
        initial_scale (float, optional): The initial loss scale of AMP. Defaults to 2**16.
        min_scale (float, optional): The minimum loss scale of AMP. Defaults to 1.
        growth_factor (float, optional): The multiplication factor for increasing loss scale when using AMP. Defaults to 2.
        backoff_factor (float, optional): The multiplication factor for decreasing loss scale when using AMP. Defaults to 0.5.
        growth_interval (int, optional): The number of steps to increase loss scale when no overflow occurs when using AMP. Defaults to 1000.
        hysteresis (int, optional):  The number of overflows before decreasing loss scale when using AMP. Defaults to 2.
        max_scale (float, optional): The maximum loss scale of AMP. Defaults to 2**32.
        max_norm (float, optional): Maximum norm for gradient clipping. Defaults to 0.
        broadcast_buffers (bool, optional): Whether to broadcast buffers in the beginning of training when using DDP. Defaults to True.
        ddp_bucket_cap_mb (int, optional): The bucket size in MB when using DDP. Defaults to 25.
        find_unused_parameters (bool, optional): Whether to find unused parameters when using DDP. Defaults to False.
        check_reduction (bool, optional): Whether to check reduction when using DDP. Defaults to False.
        gradient_as_bucket_view (bool, optional): Whether to use gradient as bucket view when using DDP. Defaults to False.
        static_graph (bool, optional): Whether to use static graph when using DDP. Defaults to False.
        zero_bucket_size_in_m (int, optional): Gradient reduce bucket size in million elements when using ZeRO. Defaults to 12.
        cpu_offload (bool, optional): Whether to open cpu_offload when using ZeRO. Defaults to False.
        communication_dtype (torch.dtype, optional): Communication dtype when using ZeRO. If not specified, the dtype of param will be used. Defaults to None.
        overlap_communication (bool, optional): Whether to overlap communication and computation when using ZeRO. Defaults to True.
        custom_policy (Policy, optional): Custom policy for Shardformer. Defaults to None.
        pp_style (str, optional): The style for pipeline parallelism. Defaults to '1f1b'.
        num_model_chunks (int, optional): The number of model chunks for interleaved pipeline parallelism. Defaults to 1.
        gradient_checkpoint_config (GradientCheckpointConfig, optional): Configuration for gradient checkpointing. Defaults to None.
        enable_metadata_cache (bool, optional): Whether to enable metadata cache for pipeline parallelism. Defaults to True.
        make_vocab_size_divisible_by (int, optional): it's used when padding the vocabulary size, to make it choose an faster kenel. Default to 64.
        fp8_communication (bool, optional): Whether to enable fp8 communication. Defaults to False.
        use_fp8 (bool, optional): Whether to enable fp8 mixed precision training. Defaults to False.
        overlap_p2p (bool, optional): Whether to overlap the p2p communication in pipeline parallelism
        inner_ring_size (int, optional): The inner ring size of 2D Ring Attention when sp mode is "ring_attn".
            It's advisable to not tune this (especially in single-node settings) and let it be heuristically set based on topology by default.

    """

    def __init__(
        self,
        tp_size: int,
        pp_size: int,
        sp_size: int = None,
        precision: str = "fp16",
        zero_stage: int = 0,
        enable_all_optimization: bool = False,
        enable_fused_normalization: bool = False,
        enable_flash_attention: bool = False,
        enable_jit_fused: bool = False,
        enable_sequence_parallelism: bool = False,
        sequence_parallelism_mode: str = None,
        parallel_output: bool = True,
        num_microbatches: Optional[int] = None,
        microbatch_size: Optional[int] = None,
        initial_scale: float = 2**16,
        min_scale: float = 1,
        growth_factor: float = 2,
        backoff_factor: float = 0.5,
        growth_interval: int = 1000,
        hysteresis: int = 2,
        max_scale: float = 2**32,
        max_norm: float = 0,
        broadcast_buffers: bool = True,
        ddp_bucket_cap_mb: int = 25,
        find_unused_parameters: bool = False,
        check_reduction: bool = False,
        gradient_as_bucket_view: bool = False,
        static_graph: bool = False,
        zero_bucket_size_in_m: int = 12,
        cpu_offload: bool = False,
        communication_dtype: Optional[torch.dtype] = None,
        overlap_communication: bool = True,
        custom_policy: Policy = None,
        pp_style: str = "1f1b",
        num_model_chunks: int = 1,
        scheduler_nodes: List = None,
        num_layers_per_stage: Optional[List[int]] = None,
        gradient_checkpoint_config: Optional[GradientCheckpointConfig] = None,
        enable_metadata_cache: bool = True,
        make_vocab_size_divisible_by: int = 64,
        dp_outside: bool = True,
        overlap_p2p: bool = True,
        overlap_allgather: bool = False,
        fp8_communication: bool = False,
        use_fp8: bool = False,
        inner_ring_size: int = None,
    ) -> None:
        super().__init__()
        self.logger = get_dist_logger()

        assert (
            dist.get_world_size() % (tp_size * pp_size) == 0
        ), f"World size {dist.get_world_size()} is not divisible by tp_size {tp_size} * pp_size {pp_size}"

        assert (
            not pp_style == "zbv" or scheduler_nodes is not None
        ), f"scheduler_nodes must not be None when using zero bubble pipeline."
        if enable_sequence_parallelism:
            self.sequence_parallelism_mode = (
                sequence_parallelism_mode if sequence_parallelism_mode is not None else "all_to_all"
            )
            assert (
                self.sequence_parallelism_mode in SUPPORT_SP_MODE
            ), f"Sequence parallelism mode {self.sequence_parallelism_mode} is not in the supported list {SUPPORT_SP_MODE}"
            if self.sequence_parallelism_mode in ["split_gather", "ring"]:
                assert (
                    tp_size > 1
                ), f"Sequence parallelism mode {self.sequence_parallelism_mode} must be enabled when using tensor parallelism"
                if sp_size != 1:
                    self.logger.warning(
                        f"The sp_size will be the same as tp_size in sequence parallelism mode {self.sequence_parallelism_mode}, will ignore the given sequence parallelism size.",
                        ranks=[0],
                    )
                self.sp_size = 1
                self.dp_size = dist.get_world_size() // (tp_size * pp_size)
            elif self.sequence_parallelism_mode in ["all_to_all", "ring_attn"]:
                self.sp_size = 1 if sp_size is None else sp_size
                self.dp_size = dist.get_world_size() // (self.sp_size * pp_size * tp_size)
                if self.sequence_parallelism_mode == "ring_attn":
                    enable_flash_attention = True
        else:
            self.dp_size = dist.get_world_size() // (tp_size * pp_size)
            assert (
                sp_size == 1 or sp_size is None
            ), f"You should not set sp_size when sequence parallelism is not enabled."
            self.sp_size = 1

        self.tp_size = tp_size
        self.pp_size = pp_size
        self.precision = precision
        self.zero_stage = zero_stage
        self.cpu_offload = cpu_offload
        self.enable_all_optimization = enable_all_optimization
        self.enable_fused_normalization = enable_fused_normalization
        self.enable_flash_attention = enable_flash_attention
        self.enable_jit_fused = enable_jit_fused
        self.enable_sequence_parallelism = enable_sequence_parallelism
        self.use_fp8 = use_fp8
        if dp_outside:
            self.dp_axis, self.pp_axis, self.tp_axis, self.sp_axis = 0, 1, 2, 3
            self.pg_mesh = ProcessGroupMesh(self.dp_size, self.pp_size, self.tp_size, self.sp_size)
            if sequence_parallelism_mode == "ring_attn":
                # Swap tp and sp since 2D Ring has better inter-node latency
                self.pg_mesh = ProcessGroupMesh(self.dp_size, self.pp_size, self.sp_size, self.tp_size)
                self.sp_axis = 2
                self.tp_axis = 3
            else:
                self.pg_mesh = ProcessGroupMesh(self.dp_size, self.pp_size, self.tp_size, self.sp_size)
        else:
            self.pp_axis, self.dp_axis, self.tp_axis, self.sp_axis = 0, 1, 2, 3
            if sequence_parallelism_mode == "ring_attn":
                self.pg_mesh = ProcessGroupMesh(self.pp_size, self.dp_size, self.sp_size, self.tp_size)
                self.sp_axis = 2
                self.tp_axis = 3
            else:
                self.pg_mesh = ProcessGroupMesh(self.pp_size, self.dp_size, self.tp_size, self.sp_size)

        self.stage_manager = None
        self.scheduler = None
        self.custom_policy = custom_policy
        assert zero_stage in (0, 1, 2)
        if self.pp_size > 1:
            assert pp_style in ["1f1b", "interleaved", "zbv"], "Unsupported pipeline parallelism style"
            assert (
                pp_style in ["interleaved", "zbv"] or num_model_chunks == 1
            ), "num_model_chunks must be 1 when using 1f1b"
            assert (
                pp_style in ["1f1b", "interleaved"] or num_model_chunks == 2
            ), "num_model_chunks must be 2 when using zero bubble pipeline"
            assert (
                num_microbatches is not None or microbatch_size is not None
            ), "num_microbatches or microbatch_size must be specified when using pipeline parallelism"
            assert (
                self.zero_stage <= 1
            ), "To avoid prohibitive gradient synchronization costs, zero stage must be 0 or 1 when using pipeline parallelism"
            if pp_style == "zbv":
                self.logger.warning(
                    """the enable_gradient_checkpointing function must set the use_reentrant to False, such as  model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant':False})"""
                )
            self.stage_manager = PipelineStageManager(
                self.pg_mesh,
                pipeline_axis=self.pp_axis,
                enable_interleave=(pp_style == "interleaved" or pp_style == "zbv"),
                use_zbv=(pp_style == "zbv"),
                num_model_chunks=num_model_chunks,
                num_layers_per_stage=num_layers_per_stage,
            )

            if pp_style == "interleaved":
                assert num_model_chunks > 1, "number of model chunks must be > 1 when using interleaved"
                self.scheduler = InterleavedSchedule(
                    stage_manager=self.stage_manager,
                    num_model_chunks=num_model_chunks,
                    num_microbatch=num_microbatches,
                    microbatch_size=microbatch_size,
                    enable_metadata_cache=enable_metadata_cache,
                    overlap_p2p=overlap_p2p,
                    fp8_communication=fp8_communication,
                )
            elif pp_style == "1f1b":
                self.scheduler = OneForwardOneBackwardSchedule(
                    stage_manager=self.stage_manager,
                    num_microbatches=num_microbatches,
                    microbatch_size=microbatch_size,
                    enable_metadata_cache=enable_metadata_cache,
                    fp8_communication=fp8_communication,
                )
            elif pp_style == "zbv":
                self.scheduler = ZeroBubbleVPipeScheduler(
                    stage_manager=self.stage_manager,
                    schedule=scheduler_nodes,
                    num_model_chunks=num_model_chunks,
                    num_microbatch=num_microbatches,
                    microbatch_size=microbatch_size,
                )
            else:
                raise NotImplementedError()
        if sequence_parallelism_mode == "ring_attn":
            if not parallel_output:
                self.logger.warning(
                    "parallel_output must be True for Zigzag Ring Attention, as we've not supported Zigzag all-gather yet.",
                    ranks=[0],
                )
                parallel_output = True

        self.tp_group = self.pg_mesh.get_group_along_axis(self.tp_axis)
        self.dp_group = self.pg_mesh.get_group_along_axis(self.dp_axis)
        self.pp_group = self.pg_mesh.get_group_along_axis(self.pp_axis)
        if self.enable_sequence_parallelism and self.sequence_parallelism_mode in ["split_gather", "ring"]:
            self.sp_group = self.pg_mesh.get_group_along_axis(self.tp_axis)
        else:
            self.sp_group = self.pg_mesh.get_group_along_axis(self.sp_axis)

        # sync gradients across DP * SP ranks
        # sync gradients across DP * SP ranks
        # Apply Hybrid ZeRO across DP * SP ranks
        if self.enable_sequence_parallelism and not is_share_sp_tp(self.sequence_parallelism_mode):
            self.mixed_dp_group = self.pg_mesh.create_group_along_axis([self.dp_axis, self.sp_axis])
            self.dp_size = get_world_size(self.mixed_dp_group)
        else:
            self.mixed_dp_group = self.dp_group

        self.shard_config = ShardConfig(
            tensor_parallel_process_group=self.tp_group,
            sequence_parallel_process_group=self.sp_group,
            pipeline_stage_manager=self.stage_manager,
            enable_tensor_parallelism=self.tp_size > 1,
            enable_all_optimization=self.enable_all_optimization,
            enable_fused_normalization=self.enable_fused_normalization,
            enable_flash_attention=self.enable_flash_attention,
            enable_jit_fused=self.enable_jit_fused,
            enable_sequence_parallelism=enable_sequence_parallelism,
            sequence_parallelism_mode=sequence_parallelism_mode,
            parallel_output=parallel_output,
            make_vocab_size_divisible_by=make_vocab_size_divisible_by,
            gradient_checkpoint_config=gradient_checkpoint_config,
            fp8_communication=fp8_communication,
            inner_ring_size=inner_ring_size,
            pg_mesh=self.pg_mesh,
            sp_axis=self.sp_axis,
        )

        self.amp_config = dict(
            initial_scale=initial_scale,
            growth_factor=growth_factor,
            backoff_factor=backoff_factor,
            growth_interval=growth_interval,
            hysteresis=hysteresis,
            min_scale=min_scale,
            max_scale=max_scale,
        )

        self.ddp_config = dict(
            broadcast_buffers=broadcast_buffers,
            bucket_cap_mb=ddp_bucket_cap_mb,
            find_unused_parameters=find_unused_parameters,
            check_reduction=check_reduction,
            gradient_as_bucket_view=gradient_as_bucket_view,
            static_graph=static_graph,
        )

        self.zero_config = dict(
            reduce_bucket_size=zero_bucket_size_in_m * 1024 * 1024,
            communication_dtype=communication_dtype,
            overlap_communication=overlap_communication,
            cpu_offload=cpu_offload,
            partition_grad=(self.zero_stage == 2),
            forced_dtype=PRECISION_TORCH_TYPE[precision],
            overlap_allgather=overlap_allgather,
            fp8_communication=fp8_communication,
        )

        self.max_norm = max_norm

    def __del__(self):
        """Destroy the process groups in ProcessGroupMesh"""
        self.pg_mesh.destroy_mesh_process_groups()

    @property
    def enable_pipeline_parallelism(self) -> bool:
        return self.pp_size > 1

    def supported_devices(self) -> List[str]:
        return ["cuda", "npu"]

    def supported_precisions(self) -> List[str]:
        return ["fp16", "bf16", "fp32"]

    def control_device(self) -> bool:
        return True

    def control_precision(self) -> bool:
        return True

    def support_no_sync(self) -> bool:
        return True

    def support_lora(self) -> bool:
        return True

    def control_checkpoint_io(self) -> bool:
        return True

    def configure(
        self,
        model: Module,
        optimizer: Optional[Optimizer] = None,
        criterion: Optional[Callable] = None,
        dataloader: Optional[DataLoader] = None,
        lr_scheduler: Optional[LRScheduler] = None,
    ) -> Tuple[Module, OptimizerWrapper, Callable, DataLoader, LRScheduler]:
        param_info = get_param_info(optimizer)

        # TODO: Support Galore + ZeRO
        zero_stage = self.zero_stage
        zero_config = deepcopy(self.zero_config)

        # Replace with distributed implementation if exists
        optimizer = cast_to_distributed(optimizer)
        if isinstance(optimizer, DistGaloreAwamW) and zero_stage > 0 and self.dp_size > 0:
            self.logger.warning(
                "Galore is only supported for Tensor Parallel and vanilla Data Parallel yet. Disabling ZeRO.",
                ranks=[0],
            )
            zero_config["partition_grad"] = False
            zero_stage = 0

        if not isinstance(model, ModelWrapper):
            # Shouldn't use pp (frequent grad accumulation) with torch ddp
            use_ddp = (self.dp_size > 1 and self.pp_size == 1 and self.zero_stage == 0) or (
                self.dp_size == 1 and self.pp_size == 1
            )
            model = HybridParallelModule(
                model,
                precision=self.precision,
                shard_config=self.shard_config,
                dp_group=self.mixed_dp_group,
                tp_group=self.tp_group,
                sp_group=self.sp_group,
                use_ddp=use_ddp,
                ddp_config=self.ddp_config,
                custom_policy=self.custom_policy,
                overlap_allgather=(self.zero_stage > 0 and self.zero_config["overlap_allgather"]),
                use_fp8=self.use_fp8,
            )
        if optimizer is not None and not isinstance(optimizer, OptimizerWrapper):
            if zero_stage == 0:
                is_zero = False
                if self.precision in ["fp16", "bf16"]:
                    optimizer = HybridParallelAMPOptimizer(
                        optimizer,
                        model,
                        use_pipeline=self.enable_pipeline_parallelism,
                        param_info=param_info,
                        precision=self.precision,
                        max_norm=self.max_norm,
                        pp_process_group=self.pp_group,
                        tp_process_group=self.tp_group,
                        **self.amp_config,
                    )
                else:
                    optimizer = HybridParallelNaiveOptimizer(
                        optimizer,
                        model,
                        use_pipeline=self.enable_pipeline_parallelism,
                        param_info=param_info,
                        max_norm=self.max_norm,
                        pp_process_group=self.pp_group,
                        tp_process_group=self.tp_group,
                    )
            else:
                is_zero = self.dp_size > 1
                if self.dp_size == 1:
                    self.logger.warning(
                        "Use Zero Optimizer when data parallel size is 1 may introduce unnecessary overhead. "
                        "If you do not intend to use cpu_offload, please consider set zero_stage=0.",
                        ranks=[0],
                    )

                assert self.precision != "fp32", "Please set precision to 'fp16' or 'bf16' when using ZeRO."
                optimizer = HybridParallelZeroOptimizer(
                    optimizer,
                    model,
                    use_pipeline=self.enable_pipeline_parallelism,
                    param_info=param_info,
                    dp_process_group=self.mixed_dp_group,
                    tp_process_group=self.tp_group,
                    pp_process_group=self.pp_group,
                    verbose=True,
                    clip_grad_norm=self.max_norm,
                    **zero_config,
                    **self.amp_config,
                )
            # inject update_master_params
            model.update_master_params = MethodType(optimizer.update_master_params, model)

            # Setup optimizers that require global states
            optim = optimizer.optim
            if isinstance(optim, DistributedOptim):
                shard_to_param = optimizer.get_master_to_working_map() if is_zero else {}
                padding_map = optimizer.get_param_padding_map() if is_zero else defaultdict(int)
                optim.setup_distributed(self.tp_group, self.dp_group, shard_to_param, padding_map, is_zero)

        return model, optimizer, criterion, dataloader, lr_scheduler

    def execute_pipeline(
        self,
        data_iter: Iterator,
        model: HybridParallelModule,
        criterion: Callable[[Any, Any], torch.Tensor],
        optimizer: Optional[
            Union[HybridParallelNaiveOptimizer, HybridParallelAMPOptimizer, HybridParallelZeroOptimizer]
        ] = None,
        return_loss: bool = True,
        return_outputs: bool = False,
    ) -> dict:
        assert self.enable_pipeline_parallelism, "pipeline parallelism is not enabled"

        if return_outputs:
            self.logger.warning("return_outputs may lead to significant extra memory consumption.", ranks=[0])

        # Create a context for gradient synchronization based on the optimizer type.
        # If it's a HybridParallelZeroOptimizer, use optimizer.no_sync(); otherwise, use model.no_sync().
        # This is to avoid redundant gradient reduction in pipeline parallelism (multiple microbatch values should be reduced once),
        # so we disable it, performing manual reduction instead.
        ctx = optimizer.no_sync() if isinstance(optimizer, HybridParallelZeroOptimizer) else model.no_sync()

        with ctx, model._hook_context():
            outputs = self.scheduler.forward_backward_step(
                model, data_iter, criterion, optimizer, return_loss, return_outputs
            )

        # run with gradients accumulation
        if model.require_grad_sync == False or (
            isinstance(optimizer, HybridParallelZeroOptimizer) and optimizer.require_grad_sync == False
        ):
            return outputs

        # Synchronize the grads of shared parameters of the model.
        model.sync_shared_params()
        # Synchronize sequence parallelism gradients of the model.
        model.sync_sp_grads()

        # Check if the optimizer is a HybridParallelZeroOptimizer and synchronize data parallelism gradients if so.
        # Otherwise, synchronize data parallelism gradients of the model.
        # This is because these are two different forms of data parallelism.
        if isinstance(optimizer, HybridParallelZeroOptimizer):
            optimizer.sync_dp_grads()
        else:
            model.sync_dp_grads()

        return outputs

    def prepare_dataloader(
        self,
        dataset,
        batch_size,
        shuffle=False,
        seed=1024,
        drop_last=False,
        pin_memory=False,
        num_workers=0,
        distributed_sampler_cls=None,
        **kwargs,
    ):
        r"""
        Prepare a dataloader for distributed training. The dataloader will be wrapped by
        `torch.utils.data.DataLoader` and `torch.utils.data.DistributedSampler`.


        Args:
            dataset (`torch.utils.data.Dataset`): The dataset to be loaded.
            shuffle (bool, optional): Whether to shuffle the dataset. Defaults to False.
            seed (int, optional): Random worker seed for sampling, defaults to 1024.
            add_sampler: Whether to add ``DistributedDataParallelSampler`` to the dataset. Defaults to True.
            drop_last (bool, optional): Set to True to drop the last incomplete batch, if the dataset size
                is not divisible by the batch size. If False and the size of dataset is not divisible by
                the batch size, then the last batch will be smaller, defaults to False.
            pin_memory (bool, optional): Whether to pin memory address in CPU memory. Defaults to False.
            num_workers (int, optional): Number of worker threads for this dataloader. Defaults to 0.
            kwargs (dict): optional parameters for ``torch.utils.data.DataLoader``, more details could be found in
                    `DataLoader <https://pytorch.org/docs/stable/_modules/torch/utils/data/dataloader.html#DataLoader>`_.

        Returns:`
            :class:`torch.utils.data.DataLoader`: A DataLoader used for training or testing.
        """
        _kwargs = kwargs.copy()
        distributed_sampler_cls = distributed_sampler_cls or DistributedSampler
        sampler = distributed_sampler_cls(
            dataset,
            num_replicas=self.dp_group.size(),
            rank=dist.get_group_rank(self.dp_group, global_rank=dist.get_rank()),
            shuffle=shuffle,
        )

        # Deterministic dataloader
        def seed_worker(worker_id):
            worker_seed = seed
            np.random.seed(worker_seed)
            torch.manual_seed(worker_seed)
            random.seed(worker_seed)

        return DataLoader(
            dataset,
            batch_size=batch_size,
            sampler=sampler,
            worker_init_fn=seed_worker,
            drop_last=drop_last,
            pin_memory=pin_memory,
            num_workers=num_workers,
            **_kwargs,
        )

    def get_checkpoint_io(self) -> CheckpointIO:
        return HybridParallelCheckpointIO(
            self.mixed_dp_group, self.pp_group, self.tp_group, self.sp_group, self.zero_stage
        )

    def no_sync(self, model: Module, optimizer: OptimizerWrapper) -> Iterator[None]:
        assert (
            self.zero_stage != 2
        ), "ZERO2 is not compatible with no_sync function, please run gradient accumulation with gradient synchronization allowed."
        return optimizer.no_sync() if isinstance(optimizer, HybridParallelZeroOptimizer) else model.no_sync()

    def enable_lora(
        self,
        model: Module,
        pretrained_dir: Optional[str] = None,
        lora_config: Optional[Dict] = None,
        bnb_quantization_config: Optional[BnbQuantizationConfig] = None,
    ) -> Module:
        from peft import PeftModel, get_peft_model

        assert not isinstance(model, HybridParallelModule), "Lora should be enabled before boosting the model."
        assert self.tp_size == 1
        self.lora_enabled = True
        self.logger.warning("You have enabled LoRa training. Please check the hyperparameters such as lr", ranks=[0])

        if bnb_quantization_config is not None:
            model = quantize_model(model, bnb_quantization_config)

        if pretrained_dir is None:
            peft_model = get_peft_model(model, lora_config)
        else:
            peft_model = PeftModel.from_pretrained(model, pretrained_dir, is_trainable=True)
        return peft_model
