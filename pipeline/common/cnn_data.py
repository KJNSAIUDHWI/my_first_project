# -*- coding: utf-8 -*-
"""已审核的 CNN 数据、固定父页分区和训练名单。

日志是唯一事实源：digit/choice 每张样本一个 data/<库>/log/<样本>.log。
训练名单写 selection.json，不复制图片；训练直接读 data 里的原图。
"""
from pathlib import Path
import math

from .paths import DATA, ROOT
from .records import read_csv, read_json, write_json
from . import fixed_dataset, row_log
from .fixed_dataset import read_plan
from .digit_classes import MAX_DIGIT


def _read_parent_plans():
    plans = []
    for name in ('m2', 'm1'):
        fixed_dataset._PLAN_CACHE.pop(name, None)
        plans.append(read_plan(name) or {})
    return plans


def _parent_identity(stem, plans, split_fallback=''):
    parent = stem.split('.选择题区')[0].split('.填空题区')[0]
    for plan in plans:
        if stem in plan or parent in plan:
            row = plan.get(stem, plan.get(parent))
            return row['split'], row['group'] or parent
    if split_fallback in ('train', 'val'):
        return split_fallback, stem
    raise ValueError('缺少父页分区/身份：' + stem)


def special_kind(note):
    """从特殊类型判断 changed / illegible / unknown。"""
    n = (note or '').strip().lower()
    if 'changed' in n or '改过' in n or '改答案' in n:
        return 'changed'
    if 'illegible' in n or '乱涂' in n or '看不清' in n or '无法识别' in n or '无法读' in n:
        return 'illegible'
    return 'unknown'


def _stem(file):
    return file[:-4] if file.lower().endswith('.png') else file


def digit_image(base, row):
    p = Path(base) / 'images' / row['split'] / row['file']
    return p if p.exists() else Path(base) / 'images' / row['file']


def _digit_admitted(base, row, allow_inferred=False):
    st = row_log.digit_state(row_log.read(base, _stem(row['file'])))
    if st['label'] is None:
        raise ValueError('数字日志没有标签：%s' % row['file'])
    inferred = st['basis'] == '推断'
    ok = st['reviewed'] or (inferred and allow_inferred)
    return ok, inferred, st


def digit_reviewed(base, row, allow_inferred=False):
    return _digit_admitted(base, row, allow_inferred)[0]


def data_bases(task):
    """旧库原位保留；只接入带完整批次记录的新增库。"""
    names = ('digit', 'digit_legacy', 'digit_extra') if task == 'digit' else ('choice',)
    bases = [(DATA / name, False) for name in names]
    for base in sorted((DATA / task / 'increments').glob('*')):
        if not base.is_dir():
            continue
        if not (base / 'batch.json').is_file():
            raise ValueError('增量批次未完成：' + str(base))
        meta = read_json(base / 'batch.json')
        if meta.get('task') != task or not meta.get('complete'):
            raise ValueError('增量批次记录无效：' + str(base))
        if meta.get('enabled') is False:
            continue
        bases.append((base, True))
    return bases


def choice_subject_rows(rows, subject):
    """英语实验仅筛选；同一原始页的其他区域不能留在训练侧。"""
    selected = [r for r in rows if ('.' + subject + '.') in r['stem']]
    parent = lambda r: r['stem'].split('.选择题区')[0]
    val_parents = {parent(r) for r in selected if r['split'] == 'val'}
    return [r for r in selected if r['split'] != 'train' or parent(r) not in val_parents]


def collect(task, allow_inferred=False, persist_cache=True, subject=None):
    if subject and (task != 'choice' or subject != '英语'):
        raise ValueError('学科限定目前仅支持choice英语实验')
    plans = _read_parent_plans()
    out, inferred_rows = [], []
    if task == 'digit':
        for base, incremental in data_bases('digit'):
            lib = str(base.relative_to(DATA)).replace('\\', '__').replace('/', '__')
            labels = base / 'labels.csv'
            if not labels.exists():
                continue
            for r in read_csv(labels):
                if incremental:
                    split, group = _parent_identity(r['stem'], plans)
                    if split != r['split'] or group != r['group']:
                        raise ValueError('增量父页身份变化：' + r['stem'])
                    if split != 'train':
                        continue  # 新val/test仅预留，保持旧CNN验证名单不变。
                ok, inferred, st = _digit_admitted(base, r, allow_inferred)
                label = int(st['label'])
                if not 0 <= label <= MAX_DIGIT:
                    raise ValueError('数字标签超出0~60：' + r['file'])
                p = digit_image(base, r)
                if not p.exists():
                    raise FileNotFoundError('数字图缺失：' + str(p))
                if not ok:
                    if incremental:
                        continue
                    if inferred:
                        inferred_rows.append(lib + '__' + r['file'])
                        continue
                    raise ValueError('数字尚未人工确认：' + str(p))
                if st['special']:
                    continue
                split, group = _parent_identity(r['stem'], plans, st['split'])
                if split not in ('train', 'val'):
                    continue
                out.append({'source': str(p.relative_to(ROOT)), 'file': lib + '__' + r['file'],
                            'stem': r['stem'], 'label': label, 'split': split, 'group': group})
    elif task == 'choice':
        for base, incremental in data_bases('choice'):
            manifest = {}
            for r in read_csv(base / 'manifest.csv'):
                manifest.setdefault(r['stem'], []).append(r)
            for stem, rows in manifest.items():
                if subject and ('.' + subject + '.') not in stem:
                    continue
                for r in rows:
                    if incremental:
                        split, group = _parent_identity(stem, plans)
                        if split != r['split'] or group != r['group']:
                            raise ValueError('增量父页身份变化：' + stem)
                        if split != 'train':
                            continue
                    file = '%s_%03d.png' % (stem, int(r['row']))
                    st = row_log.choice_state(row_log.read(base, _stem(file)))
                    if not st['reviewed']:
                        continue
                    weight = 1.0
                    if st['special']:
                        action = st['decision']
                        kind = special_kind(st['note'])
                        if not action:
                            # 没写处理的特殊样本：不训练，等页面上的决定
                            continue
                        if action in ('排除', '暂缓'):
                            continue
                        if action != '纳入':
                            raise ValueError('未知特殊处理：' + file)
                        if kind == 'illegible':
                            raise ValueError('乱涂样本不能纳入训练：' + file)
                        if kind == 'unknown':
                            raise ValueError('未知特殊类型不能纳入训练：' + file)
                        weight = float(st['weight'] or 0)
                        if not math.isfinite(weight) or weight <= 0:
                            raise ValueError('纳入样本权重必须为正数：' + file)
                        if kind == 'changed' and weight != 2.0:
                            raise ValueError('学生改过答案的样本权重必须为 2.0：' + file)
                        if st['status'] != '已确认':
                            # 待用户确认的纳入决定：先不训练，不因此卡住整批数据
                            continue
                    answer = st['answer']
                    if any(c not in 'ABCDEF' for c in answer):
                        raise ValueError('非法答案：' + file)
                    p = base / 'images' / file
                    if not p.is_file():
                        raise FileNotFoundError('选择题图缺失：' + str(p))
                    split, group = _parent_identity(stem, plans)
                    if split not in ('train', 'val'):
                        continue
                    out.append({'source': str(p.relative_to(ROOT)), 'file': file, 'stem': stem,
                                'answer': answer, 'weight': weight, 'split': split, 'group': group})
    else:
        raise ValueError('未知 CNN 任务：' + str(task))

    if subject:
        out = choice_subject_rows(out, subject)
    if not any(r['split'] == 'train' for r in out) or not any(r['split'] == 'val' for r in out):
        raise ValueError('训练和验证均需非空')
    groups = {}
    for r in out:
        if r['group'] in groups and groups[r['group']] != r['split']:
            raise ValueError('同一卷组跨训练/验证：' + r['group'])
        groups[r['group']] = r['split']
    return out, inferred_rows


def freeze(task, output, allow_inferred=False, subject=None):
    """只写训练名单，不复制图片。"""
    from PIL import Image
    output = Path(output)
    if output.exists():
        raise FileExistsError(str(output))
    rows, inferred = collect(task, allow_inferred, subject=subject)
    for r in rows:
        p = ROOT / r['source']
        if not p.is_file():
            raise FileNotFoundError('训练图缺失：' + str(p))
        with Image.open(p) as image:
            image.verify()
    output.mkdir(parents=True)
    write_json(output / 'selection.json', {'task': task, 'subject': subject, 'allow_inferred': allow_inferred,
                                           'inferred_excluded': inferred, 'items': rows})
    return rows


def load_frozen(output, task):
    import cv2
    import numpy as np
    output = Path(output)
    data = read_json(output / 'selection.json')
    if data['task'] != task:
        raise ValueError('快照任务不匹配')
    items = []
    for row in data['items']:
        src = Path(row['source'])
        p = src if src.is_absolute() else ROOT / src
        if not p.is_file():
            legacy = output / 'images' / row['file']
            p = legacy if legacy.is_file() else p
        raw = p.read_bytes()
        g = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if g is None:
            raise ValueError('无法读取训练图：' + str(p))
        items.append({**row, 'g': g, 'img': g, 'y': row.get('label'), 'fname': row['file']})
    return items
