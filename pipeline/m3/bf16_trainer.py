"""BF16训练：保留标准AMP检查，再检查实际M3的FP32几何损失与CPU参照。"""
import copy
import json
import random
import numpy as np
import torch
from common.numerical_trainer import GradGuardTrainer
from .range_loss import RangeLoss


class BF16RangeTrainer(GradGuardTrainer):
    def _setup_train(self):
        # Ultralytics通用检查用另一只官方模型及FP16像素容差；先原样执行，不跳过。
        requested = torch.get_autocast_dtype('cuda')
        torch.set_autocast_dtype('cuda', torch.float16)
        try:
            super()._setup_train()
        finally:
            torch.set_autocast_dtype('cuda', requested)
        if not self.amp or not torch.cuda.is_bf16_supported():
            raise RuntimeError('标准AMP检查或BF16硬件支持未通过，拒绝启动BF16训练')
        if not isinstance(self.model.criterion, RangeLoss):
            raise RuntimeError('BF16训练必须使用范围约束及FP32几何损失')
        self._check_actual_bf16()
        torch.set_autocast_dtype('cuda', torch.bfloat16)
        torch.cuda.empty_cache()

    def validate(self):
        # 本机验证器在FP32权重上使用autocast；不能让训练BF16悄悄改变验证坐标精度。
        requested = torch.get_autocast_dtype('cuda')
        torch.set_autocast_dtype('cuda', torch.float16)
        evidence = dict(epoch=self.epoch+1, requested_validation_autocast='torch.float16',
                        restore_training_autocast=str(requested))
        def check_first_layer(module, inputs, output):
            evidence.update(actual_output_dtype=str(output.dtype), autocast_enabled=torch.is_autocast_enabled('cuda'))
            if output.dtype != torch.float16:
                raise RuntimeError('验证未使用约定的FP16，拒绝记录不可比的验证指标')
            handle.remove()
        evaluated = self.ema.ema if self.ema else self.model
        handle = evaluated.model[0].register_forward_hook(check_first_layer)
        try:
            result = super().validate()
            (self.save_dir/'validation_precision.json').write_text(json.dumps(evidence,indent=2),'utf-8')
            return result
        finally:
            handle.remove()
            torch.set_autocast_dtype('cuda', requested)

    def _check_actual_bf16(self):
        dataset = self.train_loader.dataset
        indices = sorted(set([0, len(dataset)//3, 2*len(dataset)//3, len(dataset)-1]))
        # 检查不消耗训练迭代器，也不改变后续抽样/增强的随机序列。
        python_rng, numpy_rng = random.getstate(), np.random.get_state()
        try:
            with torch.random.fork_rng(devices=[self.device.index]):
                batch = dataset.collate_fn([dataset[i] for i in indices])
        finally:
            random.setstate(python_rng)
            np.random.set_state(numpy_rng)
        batch = {k: v.cpu() if isinstance(v, torch.Tensor) else v for k, v in batch.items()}
        batch['img'] = batch['img'].float() / 255
        if not all(torch.isfinite(v).all() for v in batch.values() if isinstance(v, torch.Tensor)):
            raise ValueError('BF16精度检查输入非有限')
        evidence = dict(standard_amp_check='passed using unmodified FP16 official-model check',
                        train_indices=indices, files=list(batch['im_file']), comparisons=[])
        reference = None
        passed = True
        for device, dtype, repeats in [('cpu', torch.float32, 1), ('cuda', torch.bfloat16, 3)]:
            model = copy.deepcopy(self.model).to(device).float().train()
            model.requires_grad_(True)
            model.model[-1].dfl.requires_grad_(False)
            model.criterion = RangeLoss(model)
            inputs = {k: v.to(device) if isinstance(v, torch.Tensor) else v for k,v in batch.items()}
            for repeat in range(repeats):
                model.zero_grad(set_to_none=True)
                with torch.autocast(device, dtype=dtype, enabled=device=='cuda'):
                    loss, _ = model.loss(inputs)
                values = loss.detach().float().cpu()
                finite = bool(torch.isfinite(values).all())
                grads = False
                if finite:
                    loss.sum().backward()
                    grads = all(torch.isfinite(p.grad).all() for p in model.parameters() if p.grad is not None)
                state = all(torch.isfinite(v).all() for v in model.state_dict().values())
                if device=='cpu':reference=values
                # 前面批次回放已验证BF16积分用FP32；禁止接受纯BF16几何造成的大偏差。
                close = bool(finite and torch.all((values-reference).abs() <= .03 + .10*reference.abs()))
                passed = passed and finite and bool(grads) and bool(state) and close
                evidence['comparisons'].append(dict(device=device, dtype=str(dtype), repeat=repeat+1,
                    loss=values.tolist() if finite else None, finite=finite,
                    gradients_finite=bool(grads), state_finite=bool(state), within_cpu_tolerance=close))
            del model, inputs, loss
        evidence['passed'] = passed
        evidence['limitation'] = 'Initialization self-check only; long-run numerical guards and final CPU evaluation remain required.'
        (self.save_dir/'bf16_validation.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), 'utf-8')
        if not passed:
            raise RuntimeError('实际M3的BF16/CPU数值对照未通过，拒绝训练；见bf16_validation.json')
