"""Fail fast on numerical corruption; preserve unsanitized FP32 checkpoints."""
import json
import math
from copy import copy, deepcopy
from datetime import datetime
import torch
import ultralytics
from ultralytics.models.yolo.detect import DetectionTrainer
from ultralytics.utils.torch_utils import unwrap_model
from .snapshot_dataset import build_snapshot_dataset, SnapshotValidator


class GradGuardTrainer(DetectionTrainer):
    def build_dataset(self, img_path, mode='train', batch=None):
        stride = max(int(unwrap_model(self.model).stride.max()), 32)
        return build_snapshot_dataset(self.args, img_path, batch, self.data,
                                      mode=mode, rect=mode == 'val', stride=stride)

    def get_validator(self):
        self.loss_names = 'box_loss', 'cls_loss', 'dfl_loss'
        return SnapshotValidator(self.test_loader, save_dir=self.save_dir,
                                 args=copy(self.args), _callbacks=self.callbacks)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.guard_batch_files = []
        self.guard_batch = None
        self.guard_step = 0
        self.add_callback('on_train_batch_end', self._guard_batch_end)

    def _fail(self, where, details):
        if self.guard_batch is not None and not (self.save_dir / 'failure_batch.pt').exists():
            self.save_dir.mkdir(parents=True, exist_ok=True)
            torch.save({k: v.detach().cpu() if isinstance(v, torch.Tensor) else v
                        for k, v in self.guard_batch.items()
                        if k in ('img', 'cls', 'bboxes', 'batch_idx', 'im_file')},
                       self.save_dir / 'failure_batch.pt')
        record = dict(step=self.guard_step, time=datetime.now().isoformat(), epoch=self.epoch + 1,
                      where=where, files=self.guard_batch_files, details=details)
        self.save_dir.mkdir(parents=True, exist_ok=True)
        with (self.save_dir / 'numerical_failure.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
        raise FloatingPointError(f'Numerical failure at {where}: {details}; see numerical_failure.jsonl')

    def _tensor(self, value, where):
        if isinstance(value, torch.Tensor) and not torch.isfinite(value).all():
            self._fail(where, 'NaN/Inf')

    def _bad_names(self, named):
        # Reduce on device, transfer flags once rather than synchronize per tensor.
        pairs = [(name, value.detach()) for name, value in named if isinstance(value, torch.Tensor) and value.numel()]
        if not pairs:
            return []
        names, tensors = zip(*pairs)
        # Infinity norm preserves NaN/Inf detection without thousands of elementwise kernels.
        norms = torch._foreach_norm(tensors, float('inf'))
        bad_flags = (~torch.isfinite(torch.stack(norms))).cpu().tolist()
        return [name for name, bad in zip(names, bad_flags) if bad]

    def _tree(self, value, where):
        tensors = []
        def collect(item, name):
            if isinstance(item, dict):
                for index, (key, child) in enumerate(item.items()):
                    label = 'parameter_' + str(index) if isinstance(key, torch.Tensor) else str(key)
                    collect(child, name + '.' + label)
            elif isinstance(item, (list, tuple)):
                for key, child in enumerate(item): collect(child, name + '.' + str(key))
            elif isinstance(item, torch.Tensor): tensors.append((name, item))
        collect(value, where)
        groups = {}
        for name, tensor in tensors:
            groups.setdefault(tensor.device, []).append((name, tensor))
        for group in groups.values():
            bad = self._bad_names(group)
            if bad: self._fail(bad[0], 'NaN/Inf')

    def _state(self, model, where):
        if model is None:
            return
        state = model.state_dict()
        # Integer counters need no finite check and can live on a different device.
        bad = self._bad_names((key, value) for key, value in state.items() if value.is_floating_point())
        if bad: self._fail(where + '.' + bad[0], 'NaN/Inf')
        variances = [(key, value) for key, value in state.items() if key.endswith('running_var')]
        if variances:
            if torch.cat([value.flatten() for _, value in variances]).min().item() < 0:
                for key, value in variances:
                    if (value < 0).any(): self._fail(where + '.' + key, 'negative BatchNorm variance')

    def preprocess_batch(self, batch):
        self.guard_step += 1
        self.guard_batch = batch
        self.guard_batch_files = list(batch.get('im_file', []))
        result = super().preprocess_batch(batch)
        self.guard_batch = result
        for key in ('img', 'bboxes', 'cls'):
            self._tensor(result.get(key), 'input.' + key)
        return result

    def optimizer_step(self):
        self._tensor(self.loss, 'batch.loss')
        self._tensor(self.loss_items, 'batch.loss_items')
        # Unscale before testing/clipping; do not rely on GradScaler alone:
        # forward-pass BatchNorm buffers may already have been corrupted.
        self.scaler.unscale_(self.optimizer)
        self._state(self.model, 'model.before_step')
        self._state(self.ema.ema if self.ema else None, 'ema.before_step')
        self._tree(self.optimizer.state, 'optimizer.before_step')
        bad_grads = self._bad_names((name, param.grad) for name, param in self.model.named_parameters() if param.grad is not None)
        if bad_grads:
            if not self.scaler.is_enabled():
                self._fail('gradient.' + bad_grads[0], 'NaN/Inf without AMP')
            # unscale_ recorded found_inf: GradScaler skips optimizer.step and
            # update reduces the scale. Never clip or update EMA on this path.
            scale_before = self.scaler.get_scale()
            self.scaler.step(self.optimizer)
            self.scaler.update()
            self.optimizer.zero_grad(set_to_none=True)
            self._state(self.model, 'model.after_amp_skip')
            self._tree(self.optimizer.state, 'optimizer.after_amp_skip')
            record = dict(time=datetime.now().isoformat(), epoch=self.epoch + 1,
                files=self.guard_batch_files, scale_before=scale_before,
                scale_after=self.scaler.get_scale(), gradients=bad_grads,
                loss=float(self.loss.detach()), action='skip_update_and_ema')
            with (self.save_dir / 'amp_overflow.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
            return
        norm = torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=10.0)
        self._tensor(norm, 'gradient.norm')
        self.scaler.step(self.optimizer)
        self.scaler.update()
        self.optimizer.zero_grad(set_to_none=True)
        self._state(self.model, 'model.after_step')
        self._tree(self.optimizer.state, 'optimizer.after_step')
        if self.ema:
            self.ema.update(self.model)
            self._state(self.ema.ema, 'ema.after_step')

    @staticmethod
    def _guard_batch_end(trainer):
        trainer._tensor(trainer.loss, 'batch_end.loss')
        trainer._tensor(trainer.loss_items, 'batch_end.loss_items')
        trainer._state(trainer.model, 'batch_end.model')

    def validate(self):
        self._state(self.ema.ema if self.ema else self.model, 'before_validation')
        metrics, fitness = super().validate()
        for key, value in metrics.items():
            if not math.isfinite(float(value)):
                self._fail('validation.' + key, str(value))
        if not math.isfinite(float(fitness)):
            self._fail('validation.fitness', str(fitness))
        self._tensor(self.tloss, 'epoch.train_loss')
        return metrics, fitness

    def save_model(self):
        # Bypass the installed trainer's EMA repair and nan_to_num_ entirely.
        self._state(self.model, 'save.model')
        self._state(self.ema.ema, 'save.ema')
        self._tree(self.optimizer.state, 'save.optimizer')
        ema = deepcopy(unwrap_model(self.ema.ema)).float()
        if hasattr(ema, 'criterion'):
            ema.criterion = None
        checkpoint = dict(epoch=self.epoch, best_fitness=self.best_fitness,
            model=None, ema=ema, updates=self.ema.updates,
            optimizer=deepcopy(self.optimizer.state_dict()), scaler=self.scaler.state_dict(),
            train_args=vars(self.args), train_metrics={**self.metrics, 'fitness': self.fitness},
            train_results=self.read_results_csv(), date=datetime.now().isoformat(),
            version=ultralytics.__version__, numerical_guard='amp_skip_fail_fast_fp32_v2')
        self.wdir.mkdir(parents=True, exist_ok=True)
        # Serialize once, then atomically replace checkpoints; retain previous on failure.
        import io
        buffer = io.BytesIO()
        torch.save(checkpoint, buffer)
        targets = [self.last]
        if self.best_fitness == self.fitness:
            targets.append(self.best)
        if self.save_period > 0 and self.epoch % self.save_period == 0:
            targets.append(self.wdir / f'epoch{self.epoch}.pt')
        for target in targets:
            staging = target.with_suffix('.pt.pending')
            staging.write_bytes(buffer.getvalue())
            staging.replace(target)
        return True
