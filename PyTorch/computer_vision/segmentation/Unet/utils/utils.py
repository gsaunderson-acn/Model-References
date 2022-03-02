# Copyright (c) 2021, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
###############################################################################
# Copyright (C) 2021 Habana Labs, Ltd. an Intel Company
# All Rights Reserved.
#
# Unauthorized copying of this file or any element(s) within it, via any medium
# is strictly prohibited.
# This file contains Habana Labs, Ltd. proprietary and confidential information
# and is subject to the confidentiality and license agreements under which it
# was provided.
#
###############################################################################


import glob
import os
import pickle
from argparse import ArgumentDefaultsHelpFormatter, ArgumentParser
from subprocess import call
from pathlib import Path
from copy import deepcopy

import numpy as np
import torch
from dllogger import JSONStreamBackend, Logger, StdOutBackend, Verbosity
from sklearn.model_selection import KFold
import random

import pytorch_lightning as pl
from pytorch_lightning import  seed_everything
from pytorch_lightning.callbacks import ModelCheckpoint


def mark_step(is_lazy_mode):
    if is_lazy_mode:
        import habana_frameworks.torch.core as htcore
        htcore.mark_step()


def get_device(args):
    if args.gpus:
        return torch.device('cuda')
    elif args.hpus:
        return torch.device('hpu')
    else:
        return torch.device('cpu')


def permute_4d_5d_tensor(tensor, to_filters_last):
    import habana_frameworks.torch.core as htcore
    if htcore.is_enabled_weight_permute_pass() is True:
        return tensor
    if tensor.ndim == 4:
        if to_filters_last:
            tensor = tensor.permute((2, 3, 1, 0))
        else:
            tensor = tensor.permute((3, 2, 0, 1))  # permute RSCK to KCRS
    elif tensor.ndim == 5:
        if to_filters_last:
            tensor = tensor.permute((2, 3, 4, 1, 0))
        else:
            tensor = tensor.permute((4, 3, 0, 1, 2))  # permute RSTCK to KCRST
    return tensor


def permute_params(model, to_filters_last, lazy_mode):
    with torch.no_grad():
        for name, param in model.named_parameters():
            param.data = permute_4d_5d_tensor(param.data, to_filters_last)
    mark_step(lazy_mode)


def change_state_dict_device(state_dict, to_device):
    for name, param in state_dict.items():
        if isinstance(param, torch.Tensor):
            state_dict[name] = param.to(to_device)
    return state_dict


def adjust_tensors_for_save(state_dict, optimizer_states, to_device, to_filters_last, lazy_mode, permute):
    if permute:
        for name, param in state_dict.items():
            if isinstance(param, torch.Tensor):
                param.data = permute_4d_5d_tensor(param.data, to_filters_last)
        mark_step(lazy_mode)

    change_state_dict_device(state_dict, to_device)

    for state in optimizer_states.values():
        for k, v in state.items():
            if isinstance(v, torch.Tensor):
                state[k] = v.to(to_device)


def is_main_process():
    return int(os.getenv("LOCAL_RANK", "0")) == 0


def get_canonical_path_str(name):
    return os.fspath(Path(os.path.expandvars(os.path.expanduser(name))).resolve())


def set_cuda_devices(args):
    assert args.gpus <= torch.cuda.device_count(), f"Requested {args.gpus} gpus, available {torch.cuda.device_count()}."
    device_list = ",".join([str(i) for i in range(args.gpus)])
    os.environ["CUDA_VISIBLE_DEVICES"] = os.environ.get("CUDA_VISIBLE_DEVICES", device_list)


def verify_ckpt_path(args):
    resume_path = os.path.join(args.results, "checkpoints", "last.ckpt")
    ckpt_path = resume_path if args.resume_training and os.path.exists(resume_path) else args.ckpt_path
    return ckpt_path


def get_task_code(args):
    return f"{args.task}_{args.dim}d"


def get_config_file(args):
    task_code = get_task_code(args)
    if args.data != "/data":
        path = os.path.join(args.data, "config.pkl")
    else:
        path = os.path.join(args.data, task_code, "config.pkl")
    return pickle.load(open(path, "rb"))


def get_dllogger(results):
    return Logger(
        backends=[
            JSONStreamBackend(Verbosity.VERBOSE, os.path.join(results, "logs.json")),
            StdOutBackend(Verbosity.VERBOSE, step_format=lambda step: f"Epoch: {step} "),
        ]
    )


def get_tta_flips(dim):
    if dim == 2:
        return [[2], [3], [2, 3]]
    return [[2], [3], [4], [2, 3], [2, 4], [3, 4], [2, 3, 4]]


def make_empty_dir(path):
    call(["rm", "-rf", path])
    os.makedirs(path)


def flip(data, axis):
    return torch.flip(data, dims=axis)


def positive_int(value):
    ivalue = int(value)
    assert ivalue > 0, f"Argparse error. Expected positive integer but got {value}"
    return ivalue


def non_negative_int(value):
    ivalue = int(value)
    assert ivalue >= 0, f"Argparse error. Expected positive integer but got {value}"
    return ivalue


def float_0_1(value):
    ivalue = float(value)
    assert 0 <= ivalue <= 1, f"Argparse error. Expected float to be in range (0, 1), but got {value}"
    return ivalue


def get_unet_params(args):
    config = get_config_file(args)
    patch_size, spacings = config["patch_size"], config["spacings"]
    strides, kernels, sizes = [], [], patch_size[:]
    while True:
        spacing_ratio = [spacing / min(spacings) for spacing in spacings]
        stride = [2 if ratio <= 2 and size >= 8 else 1 for (ratio, size) in zip(spacing_ratio, sizes)]
        kernel = [3 if ratio <= 2 else 1 for ratio in spacing_ratio]
        if all(s == 1 for s in stride):
            break
        sizes = [i / j for i, j in zip(sizes, stride)]
        spacings = [i * j for i, j in zip(spacings, stride)]
        kernels.append(kernel)
        strides.append(stride)
        if len(strides) == 5:
            break
    strides.insert(0, len(spacings) * [1])
    kernels.append(len(spacings) * [3])
    return config["in_channels"], config["n_class"], kernels, strides, patch_size


def log(logname, dice, results="/results"):
    dllogger = Logger(
        backends=[
            JSONStreamBackend(Verbosity.VERBOSE, os.path.join(results, logname)),
            StdOutBackend(Verbosity.VERBOSE, step_format=lambda step: ""),
        ]
    )
    metrics = {}
    metrics.update({"Mean dice": round(dice.mean().item(), 2)})
    metrics.update({f"L{j+1}": round(m.item(), 2) for j, m in enumerate(dice)})
    dllogger.log(step=(), data=metrics)
    dllogger.flush()


def layout_2d(img, lbl):
    batch_size, depth, channels, height, weight = img.shape
    img = torch.reshape(img, (batch_size * depth, channels, height, weight))
    if lbl is not None:
        lbl = torch.reshape(lbl, (batch_size * depth, 1, height, weight))
        return img, lbl
    return img


def get_split(data, idx):
    return list(np.array(data)[idx])


def load_data(path, files_pattern):
    return sorted(glob.glob(os.path.join(path, files_pattern)))


def get_path(args):
    if args.data != "/data":
        return args.data
    data_path = os.path.join(args.data, get_task_code(args))
    if args.exec_mode == "predict" and not args.benchmark:
        data_path = os.path.join(data_path, "test")
    return data_path


def get_test_fnames(args, data_path, meta=None):
    kfold = KFold(n_splits=args.nfolds, shuffle=True, random_state=12345)
    test_imgs = load_data(data_path, "*_x.npy")

    if args.exec_mode == "predict" and "val" in data_path:
        _, val_idx = list(kfold.split(test_imgs))[args.fold]
        test_imgs = sorted(get_split(test_imgs, val_idx))
        if meta is not None:
            meta = sorted(get_split(meta, val_idx))

    return test_imgs, meta


def set_seed(seed):
    if seed is not None:
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
        random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed(seed)
        np.random.seed(seed)
        seed_everything(seed)


def get_main_args(strings=None):
    parser = ArgumentParser(formatter_class=ArgumentDefaultsHelpFormatter)
    arg = parser.add_argument
    arg(
        "--exec_mode",
        type=str,
        choices=["train", "evaluate", "predict"],
        default="train",
        help="Execution mode to run the model",
    )
    arg("--data", type=str, default="/data", help="Path to data directory")
    arg("--results", type=str, default="/results", help="Path to results directory")
    arg("--logname", type=str, default=None, help="Name of dlloger output")
    arg("--task", type=str, help="Task number. MSD uses numbers 01-10")
    arg("--gpus", type=non_negative_int, default=0, help="Number of gpus")
    arg("--hpus", type=non_negative_int, default=0, help="Number of hpus")
    arg("--learning_rate", type=float, default=0.001, help="Learning rate")
    arg("--gradient_clip_val", type=float, default=0, help="Gradient clipping norm value")
    arg("--negative_slope", type=float, default=0.01, help="Negative slope for LeakyReLU")
    arg("--tta", action="store_true", help="Enable test time augmentation")
    arg("--amp", action="store_true", help="Enable automatic mixed precision")
    arg("--benchmark", action="store_true", help="Run model benchmarking")
    arg("--deep_supervision", action="store_true", help="Enable deep supervision")
    arg("--drop_block", action="store_true", help="Enable drop block")
    arg("--attention", action="store_true", help="Enable attention in decoder")
    arg("--residual", action="store_true", help="Enable residual block in encoder")
    arg("--focal", action="store_true", help="Use focal loss instead of cross entropy")
    arg("--sync_batchnorm", action="store_true", help="Enable synchronized batchnorm")
    arg("--save_ckpt", action="store_true", help="Enable saving checkpoint")
    arg("--nfolds", type=positive_int, default=5, help="Number of cross-validation folds")
    arg("--seed", type=non_negative_int, default=1, help="Random seed")
    arg("--skip_first_n_eval", type=non_negative_int, default=0, help="Skip the evaluation for the first n epochs.")
    arg("--ckpt_path", type=str, default=None, help="Path to checkpoint")
    arg("--fold", type=non_negative_int, default=0, help="Fold number")
    arg("--patience", type=positive_int, default=100, help="Early stopping patience")
    arg("--lr_patience", type=positive_int, default=70, help="Patience for ReduceLROnPlateau scheduler")
    arg("--batch_size", type=positive_int, default=2, help="Batch size")
    arg("--val_batch_size", type=positive_int, default=4, help="Validation batch size")
    arg("--steps", nargs="+", type=positive_int, required=False, help="Steps for multistep scheduler")
    arg("--profile", action="store_true", help="Run dlprof profiling")
    arg("--momentum", type=float, default=0.99, help="Momentum factor")
    arg("--weight_decay", type=float, default=0.0001, help="Weight decay (L2 penalty)")
    arg("--save_preds", action="store_true", help="Enable prediction saving")
    arg("--dim", type=int, choices=[2, 3], default=3, help="UNet dimension")
    arg("--resume_training", action="store_true", help="Resume training from the last checkpoint")
    arg("--factor", type=float, default=0.3, help="Scheduler factor")
    arg("--num_workers", type=non_negative_int, default=8, help="Number of subprocesses to use for data loading")
    arg("--min_epochs", type=non_negative_int, default=30, help="Force training for at least these many epochs")
    arg("--max_epochs", type=non_negative_int, default=10000, help="Stop training after this number of epochs")
    arg("--warmup", type=non_negative_int, default=5, help="Warmup iterations before collecting statistics")
    arg("--norm", type=str, choices=["instance", "batch", "group"], default="instance", help="Normalization layer")
    arg("--nvol", type=positive_int, default=1, help="Number of volumes which come into single batch size for 2D model")
    arg('--run_lazy_mode', action='store_true', help='Run model in lazy execution mode')
    arg('--hmp', dest='is_hmp', action='store_true', help='Enable hmp mode')
    arg('--hmp-bf16', default='', help='Path to bf16 ops list in hmp O1 mode')
    arg('--hmp-fp32', default='', help='Path to fp32 ops list in hmp O1 mode')
    arg('--hmp-opt-level', default='O1', help='Choose optimization level for hmp')
    arg('--hmp-verbose', action='store_true', help='Enable verbose mode for hmp')
    arg("--bucket_cap_mb", type=positive_int, default=125, help="Size in MB for the gradient reduction bucket size")
    arg(
        '--channels_last',
        default='True',
        type=lambda x: x.lower() == 'true',
        help='Whether input is channels last format. Any value other than True disables channels-last')
    arg(
        "--data2d_dim",
        choices=[2, 3],
        type=int,
        default=3,
        help="Input data dimension for 2d model",
    )
    arg(
        "--oversampling",
        type=float_0_1,
        default=0.33,
        help="Probability of crop to have some region with positive label",
    )
    arg(
        "--overlap",
        type=float_0_1,
        default=0.5,
        help="Amount of overlap between scans during sliding window inference",
    )
    arg(
        "--affinity",
        type=str,
        default="disabled",
        choices=[
            "socket",
            "single",
            "single_unique",
            "socket_unique_interleaved",
            "socket_unique_continuous",
            "disabled",
        ],
        help="type of CPU affinity",
    )
    arg(
        "--scheduler",
        type=str,
        default="none",
        choices=["none", "multistep", "cosine", "plateau"],
        help="Learning rate scheduler",
    )
    arg(
        "--optimizer",
        type=str,
        default="adamw",
        choices=["sgd", "radam", "adam", "adamw", "fusedadamw"],
        help="Optimizer",
    )
    arg(
        "--blend",
        type=str,
        choices=["gaussian", "constant"],
        default="gaussian",
        help="How to blend output of overlapping windows",
    )
    arg(
        "--train_batches",
        type=non_negative_int,
        default=0,
        help="Limit number of batches for training (used for benchmarking mode only)",
    )
    arg(
        "--test_batches",
        type=non_negative_int,
        default=0,
        help="Limit number of batches for inference (used for benchmarking mode only)",
    )
    arg(
        "--ckpt_every",
        type=int,
        default=None,
        help="Backup checkpoint every n epochs",
    )
    parser.add_argument('--set_aug_seed', dest='set_aug_seed', action='store_true',
                        help='Set seed in data augmentation functions')

    parser.add_argument('--no-augment', dest='augment', action='store_false')
    parser.set_defaults(augment=True)

    if strings is not None:
        arg(
            "strings",
            metavar="STRING",
            nargs="*",
            help="String for searching",
        )
        args = parser.parse_args(strings.split())
    else:
        args = parser.parse_args()

    if args.hpus and args.gpus:
        assert False, 'Cannot use both gpus and hpus'

    if not args.hpus:
        args.run_lazy_mode = False
        if args.optimizer.lower() == 'fusedadamw':
            raise NotImplementedError("FusedAdamW is only supported for hpu devices.")
    if args.is_hmp:
        path = get_canonical_path_str(os.path.dirname(__file__))
        # set default path for bf16 ops
        if not args.hmp_bf16:
            args.hmp_bf16 = os.path.join(path, "../config/ops_bf16_unet.txt")
        if not args.hmp_fp32:
            args.hmp_fp32 = os.path.join(path, "../config/ops_fp32_unet.txt")
    return args


class PeriodicCheckpoint(ModelCheckpoint):
    def __init__(self,
                filepath  = None,
                monitor  = None,
                verbose=  False,
                save_last = None,
                save_top_k = 1,
                save_weights_only= False,
                mode = "auto",
                period = 1,
                prefix = "",
                dirpath = None,
                filename = None,
                every_n = 10,
                first_n = 10,
                pl_module = None):
        super().__init__(dirpath=dirpath,
                            filename=filename,
                            monitor=monitor,
                            verbose=verbose,
                            save_last=save_last,
                            save_top_k=save_top_k,
                            save_weights_only=save_weights_only,
                            mode=mode,
                            every_n_epochs=period)
        self.every_n = every_n
        self.first_n = first_n
        self.pl_module = pl_module

    def restore_tensors_for_ckpt(self, pl_module, state_dict):
        assert (pl_module.args.hpus)

        pl_module.model.load_state_dict(state_dict)
        adjust_tensors_for_save(
            pl_module.model.state_dict(),
            pl_module.optimizers().state,
            to_device="hpu",
            to_filters_last=True,
            lazy_mode=pl_module.args.run_lazy_mode,
            permute=False
        )

    def _save_model(self, trainer: "pl.Trainer", filepath: str) -> None:
        # make paths
        if trainer.should_rank_save_checkpoint:
            self._fs.makedirs(os.path.dirname(filepath), exist_ok=True)

        # delegate the saving to the trainer
        trainer.save_checkpoint(filepath, self.save_weights_only)

    def on_validation_end(self, trainer: 'pl.Trainer', pl_module: 'pl.LightningModule'):
        # Save a copy of state_dict and restore after save is finished
        if pl_module.args.hpus:
            state_dict = deepcopy(change_state_dict_device(pl_module.model.state_dict(), "cpu"))

        super(PeriodicCheckpoint, self).on_validation_end(trainer, pl_module)
        # save backups
        if self.every_n:
            epoch = trainer.current_epoch
            if epoch < self.first_n or epoch % self.every_n == 0:
                filepath = os.path.join(self.dirpath, f"backup_epoch_{epoch}.pt")
                # print("save backup chekpoint: ", filepath)
                # self._save_model(filepath, trainer, pl_module)
                # pl 1.4
                self._save_model(trainer, filepath)
        if pl_module.args.hpus:
            self.restore_tensors_for_ckpt(pl_module, state_dict)

    def save_checkpoint(self, trainer: 'pl.Trainer'):
        pl_module = self.pl_module
        if pl_module.args.hpus:
            state_dict = deepcopy(change_state_dict_device(pl_module.model.state_dict(), "cpu"))
        super(PeriodicCheckpoint, self).save_checkpoint(trainer)
        if pl_module.args.hpus:
            self.restore_tensors_for_ckpt(pl_module, state_dict)
