"""读取并校验固定分区；历史共享dataset构建接口已停用。"""
import csv
import os

from .paths import m_dir


# splits.csv 是固定分区，逐张图调用 parent_identity 时不能每次重解析整个文件
_PLAN_CACHE = {}


def read_plan(m):
    path = m_dir(m) / 'splits.csv'
    if not path.exists():
        return None
    stamp = path.stat().st_mtime_ns
    hit = _PLAN_CACHE.get(m)
    if hit and hit[0] == stamp:
        return hit[1]
    with path.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    by, groups = {}, {}
    for row in rows:
        name = row['new_name']
        if not name or os.path.basename(name) != name or '/' in name or '\\' in name:
            raise ValueError('非法图片名: ' + name)
        if name in by:
            raise ValueError('重复分区记录: ' + name)
        if row['split'] not in ('train', 'val', 'test', 'exclude', 'empty_test'):
            raise ValueError('未知split: ' + str(row))
        row['stage'] = int(row['stage'])
        if row['stage'] not in range(5):
            raise ValueError('非法stage: ' + str(row))
        group = row['group']
        if group and row['split'] in ('train', 'val', 'test'):
            old = groups.setdefault(group, row['split'])
            if old != row['split']:
                raise ValueError('卷组跨分区: ' + group)
        by[name] = row
    _PLAN_CACHE[m] = (stamp, by)
    return by


def build_fixed(m, cls_map, names, stage):
    raise RuntimeError('历史构建接口已停用，请使用 tools/迭代.py snapshot；固定身份读取仍可用。')
