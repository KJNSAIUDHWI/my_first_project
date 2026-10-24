"""CNN fine-tune initialization, metrics and finite-value diagnostics."""
import io
import csv
from pathlib import Path
import torch
from .records import append, write_json


def initialize(net, initial, scratch):
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
