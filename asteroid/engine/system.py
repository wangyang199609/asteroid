import torch
import pytorch_lightning as pl
from argparse import Namespace
from typing import Callable, Optional
from torch.optim.optimizer import Optimizer

from ..utils import flatten_dict


class System(pl.LightningModule):
    """Base class for deep learning systems.
    Contains a model, an optimizer, a loss function, training and validation
    dataloaders and learning rate scheduler.

    Args:
        model (torch.nn.Module): Instance of model.
        optimizer (torch.optim.Optimizer): Instance or list of optimizers.
        loss_func (callable): Loss function with signature
            (est_targets, targets).
        train_loader (torch.utils.data.DataLoader): Training dataloader.
        val_loader (torch.utils.data.DataLoader): Validation dataloader.
        scheduler (torch.optim.lr_scheduler._LRScheduler): Instance, or list
            of learning rate schedulers.
        config: Anything to be saved with the checkpoints during training.
            The config dictionary to re-instantiate the run for example.
    .. note:: By default, `training_step` (used by `pytorch-lightning` in the
        training loop) and `validation_step` (used for the validation loop)
        share `common_step`. If you want different behavior for the training
        loop and the validation loop, overwrite both `training_step` and
        `validation_step` instead.
    """

    def __init__(
        self,
        model,
        optimizer,
        loss_func,
        train_loader,
        val_loader=None,
        scheduler=None,
        config=None,
    ):
        super().__init__()
        self.model = model
        self.optimizer = optimizer
        self.loss_func = loss_func
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.scheduler = scheduler
        config = {} if config is None else config
        self.config = config
        # hparams will be logged to Tensorboard as text variables.
        # torch doesn't support None in the summary writer for now, convert
        # None to strings temporarily.
        # See https://github.com/pytorch/pytorch/issues/33140
        self.hparams = Namespace(**self.config_to_hparams(config))

    def forward(self, *args, **kwargs):
        """Applies forward pass of the model.

        Returns:
            :class:`torch.Tensor`
        """
        return self.model(*args, **kwargs)

    def common_step(self, batch, batch_nb, train=True):
        """Common forward step between training and validation.

        The function of this method is to unpack the data given by the loader,
        forward the batch through the model and compute the loss.
        Pytorch-lightning handles all the rest.

        Args:
            batch: the object returned by the loader (a list of torch.Tensor
                in most cases) but can be something else.
            batch_nb (int): The number of the batch in the epoch.
            train (bool): Whether in training mode. Needed only if the training
                and validation steps are fundamentally different, otherwise,
                pytorch-lightning handles the usual differences.

        Returns:
            :class:`torch.Tensor` : The loss value on this batch.

        .. note:: This is typically the method to overwrite when subclassing
            `System`. If the training and validation steps are somehow
            different (except for loss.backward() and optimzer.step()),
            the argument `train` can be used to switch behavior.
            Otherwise, `training_step` and `validation_step` can be overwriten.
        """
        inputs, targets = batch
        est_targets = self(inputs)
        loss = self.loss_func(est_targets, targets)
        return loss

    def training_step(self, batch, batch_nb):
        """Pass data through the model and compute the loss.

        Backprop is **not** performed (meaning PL will do it for you).

        Args:
            batch: the object returned by the loader (a list of torch.Tensor
                in most cases) but can be something else.
            batch_nb (int): The number of the batch in the epoch.

        Returns:
            dict:

            ``'loss'``: loss

            ``'log'``: dict with tensorboard logs

        """
        loss = self.common_step(batch, batch_nb, train=True)
        tensorboard_logs = {"train_loss": loss}
        return {"loss": loss, "log": tensorboard_logs}

    def optimizer_step(self, *args, **kwargs) -> None:
        if self.scheduler is not None:
            if not isinstance(self.scheduler, (list, tuple)):
                self.scheduler = [self.scheduler]  # support multiple schedulers
            for sched in self.scheduler:
                if isinstance(sched, dict) and sched["interval"] == "batch":
                    sched["scheduler"].step()  # call step on each batch scheduler
        super().optimizer_step(*args, **kwargs)

    def validation_step(self, batch, batch_nb):
        """Need to overwrite PL validation_step to do validation.

        Args:
            batch: the object returned by the loader (a list of torch.Tensor
                in most cases) but can be something else.
            batch_nb (int): The number of the batch in the epoch.

        Returns:
            dict:

            ``'val_loss'``: loss
        """
        loss = self.common_step(batch, batch_nb, train=False)
        return {"val_loss": loss}

    def validation_epoch_end(self, outputs):
        """How to aggregate outputs of `validation_step` for logging.

        Args:
           outputs (list[dict]): List of validation losses, each with a
           ``'val_loss'`` key

        Returns:
            dict: Average loss

            ``'val_loss'``: Average loss on `outputs`

            ``'log'``: Tensorboard logs

            ``'progress_bar'``: Tensorboard logs
        """
        avg_loss = torch.stack([x["val_loss"] for x in outputs]).mean()
        tensorboard_logs = {"val_loss": avg_loss}
        return {"val_loss": avg_loss, "log": tensorboard_logs, "progress_bar": tensorboard_logs}

    def configure_optimizers(self):
        """ Required by pytorch-lightning. """

        if self.scheduler is not None:
            if not isinstance(self.scheduler, (list, tuple)):
                self.scheduler = [self.scheduler]  # support multiple schedulers
            epoch_schedulers = []
            for sched in self.scheduler:
                if not isinstance(sched, dict):
                    epoch_schedulers.append(sched)
                else:
                    assert sched["interval"] in [
                        "batch",
                        "epoch",
                    ], "Scheduler interval should be either batch or epoch"
                    if sched["interval"] == "epoch":
                        epoch_schedulers.append(sched)
            return [self.optimizer], epoch_schedulers
        return self.optimizer

    def train_dataloader(self):
        return self.train_loader

    def val_dataloader(self):
        return self.val_loader

    def on_save_checkpoint(self, checkpoint):
        """ Overwrite if you want to save more things in the checkpoint."""
        checkpoint["training_config"] = self.config
        return checkpoint

    def on_batch_start(self, batch):
        """ Overwrite if needed. Called by pytorch-lightning"""
        pass

    def on_batch_end(self):
        """ Overwrite if needed. Called by pytorch-lightning"""
        pass

    def on_epoch_start(self):
        """ Overwrite if needed. Called by pytorch-lightning"""
        pass

    def on_epoch_end(self):
        """ Overwrite if needed. Called by pytorch-lightning"""
        pass

    @staticmethod
    def config_to_hparams(dic):
        """Sanitizes the config dict to be handled correctly by torch
        SummaryWriter. It flatten the config dict, converts `None` to
         ``'None'`` and any list and tuple into torch.Tensors.

        Args:
            dic (dict): Dictionary to be transformed.

        Returns:
            dict: Transformed dictionary.
        """
        dic = flatten_dict(dic)
        for k, v in dic.items():
            if v is None:
                dic[k] = str(v)
            elif isinstance(v, (list, tuple)):
                dic[k] = torch.Tensor(v)
        return dic
