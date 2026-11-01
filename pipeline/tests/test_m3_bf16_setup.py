"""BF16初始化必须执行标准AMP检查；失败不能留下错误的全局精度。"""
import sys
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import torch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.numerical_trainer import GradGuardTrainer
from m3.bf16_trainer import BF16RangeTrainer

old=torch.get_autocast_dtype('cuda')
calls=[]
def reject_standard_check(self):
    calls.append(torch.get_autocast_dtype('cuda'))
    raise RuntimeError('standard AMP check rejected fixture')
try:
    torch.set_autocast_dtype('cuda',torch.bfloat16)
    with patch.object(GradGuardTrainer,'_setup_train',reject_standard_check):
        try:BF16RangeTrainer.__new__(BF16RangeTrainer)._setup_train()
        except RuntimeError as error:assert 'standard AMP check rejected' in str(error)
        else:raise AssertionError('must not bypass standard setup failure')
    assert calls==[torch.float16]
    assert torch.get_autocast_dtype('cuda')==torch.bfloat16
    def reject_validation(self):
        assert torch.get_autocast_dtype('cuda')==torch.float16
        raise RuntimeError('validation rejected fixture')
    obj=BF16RangeTrainer.__new__(BF16RangeTrainer);obj.epoch=0
    layer=torch.nn.Identity();obj.ema=SimpleNamespace(ema=SimpleNamespace(model=[layer]))
    with patch.object(GradGuardTrainer,'validate',reject_validation):
        try:obj.validate()
        except RuntimeError as error:assert 'validation rejected' in str(error)
        else:raise AssertionError('validation failure must propagate')
    assert torch.get_autocast_dtype('cuda')==torch.bfloat16 and not layer._forward_hooks
finally:
    torch.set_autocast_dtype('cuda',old)
print('PASS: standard AMP setup invoked, rejection propagated, precision restored')
