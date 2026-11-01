"""核查最大层可表达全宽框及主干迁移；不代表每层都可表达。"""
import sys
from pathlib import Path

sys.path.append('D:/Dev/Python/3.13.6/Lib/site-packages')
import torch
from ultralytics import YOLO
from ultralytics.nn.modules.block import DFL
from ultralytics.utils.tal import dist2bbox

root=Path(__file__).resolve().parents[2]
official=YOLO(str(root/'models/pretrained/yolo11s.pt'))
model=YOLO(str(root/'pipeline/m3/yolo11s_regmax32.yaml')).load(str(root/'models/pretrained/yolo11s.pt'))
head=model.model.model[-1]
assert head.reg_max==32 and head.nc==2 and max(head.stride).item()==32
assert 2*(official.model.model[-1].reg_max-1)*32 < 1024
assert 2*(head.reg_max-1)*32 > 1024
assert [2*(head.reg_max-1)*int(s) for s in head.stride] == [496, 992, 1984]
for name,value in official.model.model[:10].state_dict().items():
    assert torch.equal(value,model.model.model[:10].state_dict()[name]),name
# A full-width box at stride32 needs 16 bins on each side of a centered anchor.
logits=torch.full((1,4*32,1),-100.)
logits.view(1,4,32,1)[:,:,16,:]=100.
distance=DFL(32)(logits)
box=dist2bbox(distance,torch.tensor([[[16.],[16.]]]),xywh=False,dim=1)*32
assert torch.allclose(box.flatten(),torch.tensor([0.,0.,1024.,1024.])),box
assert model.model.yaml['reg_max']==32 and model.model.yaml['scale']=='s'
print('M3 reg_max32: stride32 can represent full-width1024; stride8/16 caps496/992 need assignment checks; official backbone unchanged')
