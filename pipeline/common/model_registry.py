"""Resolve and publish successful model versions without filename guessing."""
from pathlib import Path
import io
import re
import shutil
from .paths import MODELS, ROOT
from .records import read_json, write_json, lock, sha, now

DEFAULTS = {'m1': 'm1/m1.pt', 'm2': 'm2/m2.pt', 'm3': 'm3/m3.pt',
            'm4': 'm4/m4.pt', 'orient': 'orient/orient_page.pt',
            'digit': 'cnn/digit_cnn_v4_20261022_补多选区.pt', 'choice': 'cnn/choice_cnn_v3.pt'}


def model_record_path(value):
    if not isinstance(value, str) or not value:
        raise ValueError('权重记录缺少合法路径')
    path = (ROOT / value).resolve()
    if not path.is_relative_to(MODELS.resolve()) or path == MODELS.resolve():
        raise ValueError('登记或候选权重必须位于项目models目录内：' + value)
    return path


def current(task):
    data = read_json(MODELS / 'current.json', {})
    row = data.get(task)
    path = model_record_path(row['path']) if row else MODELS / DEFAULTS[task]
    if not path.is_file():
        raise FileNotFoundError(f'{task}缺少当前权重：{path}')
    if row and sha(path) != row['sha256']:
        raise ValueError('当前权重与登记哈希不一致：' + str(path))
    return path


def inspect_weight(path, task):
    import torch
    raw = Path(path).read_bytes()
    if task in ('digit', 'choice'):
        model = torch.jit.load(io.BytesIO(raw), map_location='cpu')
        shape = (1, 1, 64, 64) if task == 'digit' else (1, 1, 48, 192)
        model.eval()
        with torch.no_grad():
            output = model(torch.zeros(shape))
        expected = 35 if task == 'digit' else 6
        if tuple(output.shape) != (1, expected) or not torch.isfinite(output).all():
            raise ValueError('CNN输入/输出规格或数值异常')
    else:
        ck = torch.load(io.BytesIO(raw), map_location='cpu', weights_only=False)
        model = ck.get('ema') or ck.get('model')
        if model is None:
            raise ValueError('checkpoint缺少模型')
        expected = {'m1': 3, 'm2': 2}.get(task)
        if expected and len(model.names) != expected:
            raise ValueError('类别数量不匹配')
        scale = getattr(model, 'yaml', {}).get('scale')
        if task in ('m1', 'm2') and scale != {'m1': 'n', 'm2': 's'}[task]:
            raise ValueError(f'{task}架构scale不匹配：{scale}')
    bad = [k for k, v in model.state_dict().items()
           if not torch.isfinite(v).all() or (k.endswith('running_var') and (v < 0).any())]
    if bad:
        raise ValueError('权重数值损坏：' + ', '.join(bad[:10]))
    return {'sha256': sha(path), 'finite': True, 'task': task}


def archive_weight(task, source, run, purpose='迭代完成'):
    evidence = inspect_weight(source, task)
    if not re.fullmatch(r'[\w\u4e00-\u9fff-]{1,40}', purpose):
        raise ValueError('用途只能使用文字、数字、下划线及连字符')
    run = Path(run).resolve()
    with lock(MODELS / '.publish.lock'):
        old = read_json(run / 'candidate.json', {})
        if old:
            if old.get('task') != task or old.get('sha256') != evidence['sha256'] or sha(model_record_path(old['path'])) != old['sha256']:
                raise ValueError('既有留档与本次权重不一致')
            return old
        folder = MODELS / ('cnn' if task in ('digit', 'choice') else task)
        folder.mkdir(parents=True, exist_ok=True)
        prefix = ('digit_cnn' if task == 'digit' else 'choice_cnn' if task == 'choice' else task)
        day = now()[:10].replace('-', '')
        nums = []
        for p in folder.glob(f'{prefix}_{day}_*.pt'):
            match = re.match(re.escape(prefix + '_' + day) + r'_(\d+)_', p.name)
            if match:
                nums.append(int(match.group(1)))
        archive = folder / f'{prefix}_{day}_{max(nums, default=0)+1:02d}_{purpose}.pt'
        with archive.open('xb') as f:
            f.write(Path(source).read_bytes())
        if sha(archive) != evidence['sha256']:
            raise ValueError('留档复制后哈希不一致')
        row = {**evidence, 'path': str(archive.relative_to(ROOT)), 'run': str(run), 'updated': now()}
        write_json(run / 'candidate.json', row)
        return row


def publish(task, source, run, purpose='迭代完成'):
    """仅由获用户授权的独立部署操作调用；训练不调用。"""
    row = archive_weight(task, source, run, purpose)
    with lock(MODELS / '.publish.lock'):
        data = read_json(MODELS / 'current.json', {})
        if data.get(task, {}).get('run') == row['run'] and data[task].get('sha256') == row['sha256']:
            current(task)
            return data[task]
        # CNN 发布别名固定为 cnn/<task>.pt：默认回退仍指 DEFAULTS 中的版本文件，
        # 发布不覆盖任何历史版本（如 choice_cnn_v3.pt）。
        if task in ('digit', 'choice'):
            alias = MODELS / ('cnn/%s.pt' % task)
        else:
            alias = MODELS / DEFAULTS[task]
        pending = alias.with_suffix('.pt.pending')
        shutil.copy2(model_record_path(row['path']), pending)
        pending.replace(alias)
        row = {**row, 'alias': str(alias.relative_to(ROOT))}
        data[task] = row
        write_json(MODELS / 'current.json', data)
        return row
