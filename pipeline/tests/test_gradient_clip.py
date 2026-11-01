"""验证普通梯度与PyTorch一致，有限极端梯度不溢出，非有限梯度仍拒绝。"""
import argparse
import sys
from pathlib import Path
import torch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common.numerical_trainer import stable_clip_grad_norm_

ap=argparse.ArgumentParser();ap.add_argument('--device',default='cpu');args=ap.parse_args()
def parameter(values):
    p=torch.nn.Parameter(torch.zeros(len(values),device=args.device));p.grad=torch.tensor(values,device=args.device);return p
for values in [[3.,4.],[30.,40.],[0.,0.]]:
    actual,reference=parameter(values),parameter(values)
    old=torch.nn.utils.clip_grad_norm_([reference],10.)
    norm=stable_clip_grad_norm_([actual],10.)
    assert torch.allclose(norm,old.double(),atol=1e-6,rtol=1e-6)
    assert torch.allclose(actual.grad,reference.grad,atol=1e-6,rtol=1e-6)
actual=parameter([3e20,4e20]);reference=parameter([3e20,4e20])
old=torch.nn.utils.clip_grad_norm_([reference],10.)
assert not torch.isfinite(old)  # 复现FP32聚合溢出，单个梯度却都有限。
norm=stable_clip_grad_norm_([actual],10.)
assert torch.isfinite(norm) and torch.allclose(actual.grad,torch.tensor([6.,8.],device=args.device),atol=2e-6)
actual=parameter([1e38,-1e38]);norm=stable_clip_grad_norm_([actual],10.)
assert torch.isfinite(norm) and abs(float(torch.linalg.vector_norm(actual.grad.double()))-10)<1e-5
first,second=parameter([3e20]),parameter([4e20])
norm=stable_clip_grad_norm_([first,second],10.)
assert torch.allclose(first.grad,torch.tensor([6.],device=args.device),atol=2e-6)
assert torch.allclose(second.grad,torch.tensor([8.],device=args.device),atol=2e-6)
for invalid in [float('nan'),float('inf')]:
    p=parameter([invalid,1.])
    try:stable_clip_grad_norm_([p],10.)
    except FloatingPointError:pass
    else:raise AssertionError('non-finite gradient must still be rejected')
assert stable_clip_grad_norm_([])==0
print('PASS',args.device,'ordinary equivalence, finite overflow case, extreme safe scaling, NaN/Inf rejection')
