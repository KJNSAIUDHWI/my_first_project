"""范围分配的最小回归：不训练、不读写正式数据。"""
import sys
from pathlib import Path
from types import SimpleNamespace
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from m3.range_loss import RangeAssigner, RangeLoss, install_range_loss
from ultralytics.utils.loss import v8DetectionLoss


assigner = RangeAssigner(topk=1, num_classes=1, stride=[8, 16, 32])
assigner.reg_max = 32
assigner.anchor_strides = torch.tensor([[8.], [16.], [32.]])
centers = torch.tensor([[256., 256.]] * 3)
gt = torch.tensor([[[0., 100., 512., 400.]]])
mask = torch.ones(1, 1, 1)
assert assigner.select_candidates_in_gts(centers, gt, mask).tolist() == [[[False, True, True]]]
result = assigner(torch.tensor([[[.99], [.8], [.7]]]), gt.expand(1, 3, 4), centers,
                  torch.zeros(1, 1, 1), gt, mask)
assert result[3].tolist() == [[False, True, False]]  # 细层分最高，仍必须补选能表示完整框的粗层。
assigner.anchor_strides = torch.ones(3, 1) * 8
try:
    assigner.select_candidates_in_gts(centers, gt, mask)
except ValueError as e:
    assert '无可表达锚点' in str(e)
else:
    raise AssertionError('不能静默放弃所有预测层都无法表示的真值')
assert not assigner.select_candidates_in_gts(centers, torch.zeros_like(gt), torch.zeros_like(mask)).any()


class Dummy(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.dummy = torch.nn.Parameter(torch.zeros(1))
        self.args = SimpleNamespace(box=7.5, cls=.5, dfl=1.5)
        self.model = [SimpleNamespace(stride=torch.tensor([8., 16., 32.]), nc=2, reg_max=32)]


model = Dummy()
standard = v8DetectionLoss(model)
install_range_loss(SimpleNamespace(model=model))
assert isinstance(model.criterion, RangeLoss)
torch.manual_seed(42)
raw = {'feats': [torch.zeros(1, 1, s, s) for s in [4, 2, 1]],
       'boxes': torch.randn(1, 128, 21, requires_grad=True),
       'scores': torch.randn(1, 2, 21, requires_grad=True)}
batch = {'batch_idx': torch.zeros(1), 'cls': torch.zeros(1, 1),
         'bboxes': torch.tensor([[.5, .5, .4, .4]])}
reference = standard(raw, batch)[0]
actual = model.criterion(raw, batch)[0]
assert torch.allclose(reference, actual, atol=1e-6, rtol=0), (reference, actual)
actual.sum().backward()
assert all(torch.isfinite(raw[k].grad).all() for k in ['boxes', 'scores'])
empty = {'batch_idx': torch.zeros(0), 'cls': torch.zeros(0, 1), 'bboxes': torch.zeros(0, 4)}
assert torch.isfinite(model.criterion(raw, empty)[0]).all()
bf16 = {k: [v.to(torch.bfloat16) for v in values] if k == 'feats'
        else values.detach().to(torch.bfloat16).requires_grad_(True) for k, values in raw.items()}
quantized_reference = dict(bf16, boxes=bf16['boxes'].float(), scores=bf16['scores'].float())
expected = standard(quantized_reference, batch)[0]
with torch.autocast('cpu', dtype=torch.bfloat16):
    precise = model.criterion(bf16, batch)[0]
assert precise.dtype == torch.float32 and torch.allclose(precise, expected, atol=1e-6, rtol=0)
precise.sum().backward()
assert torch.isfinite(bf16['boxes'].grad).all() and torch.isfinite(bf16['scores'].grad).all()
print('PASS: insufficient fine-layer range rejected before ranking; coarser layer selected; impossible GT fails; padding/empty safe; representable-case loss unchanged; finite gradients; callback installs criterion')
print('PASS: BF16 predictions use FP32 geometry/loss without breaking gradients')
