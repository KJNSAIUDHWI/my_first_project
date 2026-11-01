"""扫描现存权重及来源；只写索引，不加载模型、不切换部署。"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def build_index(root, deployed):
    root = Path(root).resolve()
    paths = [p for folder in ('models', 'train') for p in (root / folder).rglob('*')
             if p.is_file() and p.suffix.lower() in ('.pt', '.pth', '.onnx', '.safetensors')]
    paths += list(root.glob('*.pt'))
    registry = root / 'models/current.json'
    registered = json.loads(registry.read_text('utf-8-sig')) if registry.exists() else {}
    entries = []
    for path in sorted(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('索引拒绝项目外权重：' + str(path))
        relative = path.relative_to(root).as_posix()
        role = ('pretrained' if relative.startswith('models/pretrained/') else
                'training_checkpoint' if relative.startswith('train/') else 'archived_weight')
        tasks = [task for task, value in deployed.items() if relative == value['path']]
        digest = sha(path)
        aliases = [task for task, record in registered.items()
                   if record.get('alias') and (root / record['alias']).resolve() == path.resolve()
                   and task in deployed and deployed[task]['sha256'] == digest and not tasks]
        for task in tasks:
            if digest != deployed[task]['sha256']:
                raise ValueError('当前模型SHA变化：' + task)
        entries.append(dict(path=relative, bytes=path.stat().st_size, sha256=digest,
                            role='deployed' if tasks else 'deployment_alias' if aliases else role,
                            deployed_tasks=tasks, alias_tasks=aliases,
                            source_records=[], identical_paths=[]))
        if relative.startswith('train/'):
            run = root / 'train' / path.relative_to(root / 'train').parts[0]
            entries[-1]['run'] = run.relative_to(root).as_posix()
            entries[-1]['run_metadata'] = [p.relative_to(root).as_posix() for p in sorted(run.iterdir())
                                           if p.is_file() and p.suffix in ('.json', '.yaml', '.csv')]
    by_path = {row['path']: row for row in entries}
    for task, row in deployed.items():
        if row['path'] not in by_path:
            raise ValueError('缺少当前权重：' + task)
    for record in sorted((root / 'train').glob('*/candidate.json')):
        data = json.loads(record.read_text('utf-8-sig'))
        value = data.get('path')
        if not isinstance(value, str):
            continue
        path = (root / value).resolve()
        if not path.is_relative_to(root):
            continue
        entry = by_path.get(path.relative_to(root).as_posix())
        if entry:
            entry['source_records'].append(dict(path=record.relative_to(root).as_posix(),
                task=data.get('task'), recorded_sha_matches=data.get('sha256') == entry['sha256'],
                recorded_finite=data.get('finite')))
    groups = {}
    for row in entries:
        groups.setdefault(row['sha256'], []).append(row['path'])
    for row in entries:
        row['identical_paths'] = [p for p in groups[row['sha256']] if p != row['path']]
    return dict(schema_version=1, generated_at=datetime.now().astimezone().isoformat(),
                note='文件索引不代表数值或回退验收；部署仅由current.json与DEFAULTS决定。',
                deployed=deployed, file_count=len(entries), unique_sha_count=len(groups),
                entries=entries)


def markdown(index):
    labels = dict(deployed='当前部署', pretrained='基础权重',
                  deployment_alias='部署别名',
                  training_checkpoint='训练检查点', archived_weight='留档权重')
    lines = ['# 权重索引', '', '由`tools/权重索引.py --write`生成。这里只记录文件与来源，不修改部署。', '',
             f"现存{index['file_count']}个文件、{index['unique_sha_count']}种SHA。相同SHA代表字节一致，保留别名可能重复占用。",
             '', '## 当前部署', '', '| 任务 | 文件 | SHA256 |', '|---|---|---|']
    for task, row in index['deployed'].items():
        lines.append(f"| {task} | [{row['path']}](../{row['path']}) | {row['sha256']} |")
    lines += ['', '## 全部文件', '', '| 文件 | 用途 | MiB | 来源记录数 |', '|---|---|---:|---:|']
    for row in index['entries']:
        lines.append(f"| [{row['path']}](../{row['path']}) | {labels[row['role']]} | "
                     f"{row['bytes']/1024**2:.2f} | {len(row['source_records'])} |")
    lines += ['', '详细SHA、同内容路径和候选来源见[机器索引](weights_index.json)。', '',
              '留档权重或训练检查点不自动视为可回退：切换前仍须核对架构、数值与同名单评估，并获用户授权。',
              '候选记录中的finite是历史记录，本工具没有加载模型重新验算。',
              '第二轮已删除路径的对应表见[去重映射](清理/20261031_第二轮彻底清理_1303/weight_mapping.json)。']
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true', help='生成models/weights_index.json和md')
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / 'pipeline'))
    from common.model_registry import current
    deployed = {task: dict(path=current(task).relative_to(ROOT).as_posix(), sha256=sha(current(task)))
                for task in ('m1', 'm2', 'm3', 'digit', 'choice', 'orient')}
    index = build_index(ROOT, deployed)
    if args.write:
        # Existing indexes are backed up outside the project before replacement.
        backup = Path('D:/temp') / (datetime.now().strftime('%Y-%m-%d-%H-%M') + '_codex_权重索引备份')
        for name in ('weights_index.json', 'weights_index.md'):
            path = ROOT / 'models' / name
            if path.exists():
                backup.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backup / (datetime.now().strftime('%H%M%S%f') + '_' + name))
        (ROOT / 'models/weights_index.json').write_text(json.dumps(index, ensure_ascii=False, indent=2), 'utf-8')
        (ROOT / 'models/weights_index.md').write_text(markdown(index), 'utf-8')
    print(json.dumps(dict(written=args.write, files=index['file_count'], unique=index['unique_sha_count'],
                          roles=dict(Counter(row['role'] for row in index['entries']))), ensure_ascii=False))


if __name__ == '__main__':
    main()
