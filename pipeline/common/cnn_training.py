"""CNN fine-tune initialization, metrics and finite-value diagnostics."""
import io
import csv
from pathlib import Path
import torch
from .records import append, write_json


def initialize(net, initial, scratch, expand_digit=False):
    if scratch:
        if initial:
            raise ValueError('随机重训不能同时指定旧权重')
        return
    if not initial:
        raise ValueError('微调必须指定--initial；随机重训显式传--scratch')
    raw = Path(initial).read_bytes()
    if torch.serialization._is_zipfile(io.BytesIO(raw)):
        import zipfile
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            scripted = any('/code/' in n for n in z.namelist())
    else:
        scripted = False
    if scripted:
        state = torch.jit.load(io.BytesIO(raw),map_location='cpu').state_dict()
    else:
        state = torch.load(io.BytesIO(raw),map_location='cpu',weights_only=True)
        state = state.get('model_state',state)
    if expand_digit:
        from .digit_classes import DIGIT_NC, LEGACY_DIGIT_NC
        target = net.state_dict()
        old_nc = state['fc2.weight'].shape[0]
        if old_nc == LEGACY_DIGIT_NC and target['fc2.weight'].shape[0] == DIGIT_NC:
            # 只扩题号输出层：旧类别逐值保留，新类别沿用本轮随机初始化。
            for key in ('fc2.weight', 'fc2.bias'):
                expanded = target[key].clone()
                expanded[:old_nc] = state[key]
                state[key] = expanded
    net.load_state_dict(state, strict=True)


def guard(run, net, loss=None, files=(), epoch=0, step=0, x=None, y=None, gradients=False):
    bad = []
    if loss is not None and not torch.isfinite(loss).all():
        bad.append('loss')
    for k,v in net.state_dict().items():
        if not torch.isfinite(v).all() or (k.endswith('running_var') and (v<0).any()):
            bad.append(k)
    if gradients:
        bad += ['gradient.'+k for k,p in net.named_parameters() if p.grad is not None and not torch.isfinite(p.grad).all()]
    if x is not None and not torch.isfinite(x).all():
        bad.append('input.img')
    if bad:
        append(Path(run)/'numerical_failure.jsonl', {'epoch':epoch,'step':step,'where':bad[0],'details':bad,'files':list(files)})
        torch.save({'img':x.detach().cpu() if x is not None else None,
                    'labels':y.detach().cpu() if y is not None else None,'files':list(files)},Path(run)/'failure_batch.pt')
        raise FloatingPointError('CNN数值异常：'+', '.join(bad))


def metrics(run, row):
    path = Path(run)/'results.csv'
    fresh = not path.exists()
    with path.open('a',encoding='utf-8',newline='') as f:
        w = csv.DictWriter(f,fieldnames=list(row))
        if fresh: w.writeheader()
        w.writerow(row)
