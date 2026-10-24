"""Freeze reviewed m2 labels and images with explicit, audited training deferrals."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import shutil
import sys
from collections import Counter
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'pipeline'))
from common import events
from common.fixed_dataset import read_plan


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_state(stem):
    log = ROOT / 'data/m2/log' / (stem + '.jsonl')
    rows = [json.loads(line) for line in log.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    return events.project(rows), sha(log)


def convert_box(box, stem, index, image_size=None):
    if len(box) != 5:
        raise ValueError('Wrong box field count: ' + stem)
    c, x, y, w, h = map(float, box)
    if not all(math.isfinite(v) for v in (c, x, y, w, h)):
        raise ValueError('Nonfinite label: ' + stem)
    if c not in (2, 3) or w <= 0 or h <= 0:
        raise ValueError('Invalid class or area: ' + stem)
    bounds = (x-w/2, y-h/2, x+w/2, y+h/2)
    tx, ty = (max(0.01, 2/image_size[0]), max(0.01, 2/image_size[1])) if image_size else (0.01, 0.01)
    if bounds[0] < -tx or bounds[1] < -ty or bounds[2] > 1+tx or bounds[3] > 1+ty:
        raise ValueError('Label exceeds allowed minor clipping: ' + stem)
    x1, y1, x2, y2 = max(0, bounds[0]), max(0, bounds[1]), min(1, bounds[2]), min(1, bounds[3])
    if x2 <= x1 or y2 <= y1:
        raise ValueError('No positive area after clipping: ' + stem)
    converted = (int(c)-2, (x1+x2)/2, (y1+y2)/2, x2-x1, y2-y1)
    clipped = None
    if bounds != (x1, y1, x2, y2):
        clipped = {'stem': stem, 'box_index': index, 'source': list(box),
                   'exported': list(converted), 'action': 'clip exported snapshot only'}
    return converted, clipped


def validate_deferrals(rows, plan, get_state):
    by = {}
    for row in rows:
        name = row['new_name']
        if name in by or name not in plan:
            raise ValueError('Unknown or repeated deferral: ' + name)
        identity = plan[name]
        if identity['split'] != 'train' or int(row['stage']) != identity['stage']:
            raise ValueError('Deferral identity mismatch: ' + name)
        if not row.get('reason') or not row.get('decision'):
            raise ValueError('Deferral lacks explicit decision: ' + name)
        state, _ = get_state(name)
        if state['reviewed'] or state['special']:
            raise ValueError('Deferral is reviewed or special; update the explicit list: ' + name)
        by[name] = row
    return by


def select_rows(plan, images, stage, deferred, get_state, get_size=None):
    unknown = set(images) - set(plan)
    if unknown:
        raise ValueError('Unknown image identity: ' + ', '.join(sorted(unknown)[:10]))
    selected, clips, excluded = [], [], []
    for name, identity in plan.items():
        if identity['split'] not in ('train', 'val') or identity['stage'] > stage:
            continue
        if name not in images:
            raise FileNotFoundError('Missing image: ' + name)
        if name in deferred:
            excluded.append(deferred[name])
            continue
        state, log_hash = get_state(name)
        if not state['reviewed'] or state['special']:
            raise ValueError('Unreviewed or special sample not explicitly deferred: ' + name)
        boxes = []
        image_size = get_size(name) if get_size else None
        for i, box in enumerate(state['boxes']):
            converted, clip = convert_box(box, name, i, image_size)
            boxes.append(converted)
            if clip:
                clips.append(clip)
        selected.append((name, identity, boxes, log_hash))
    counts = Counter(r[1]['split'] for r in selected)
    if not counts['train'] or not counts['val']:
        raise ValueError('Train and validation must both be nonempty')
    return selected, clips, excluded


def main():
    raise SystemExit('历史快照入口已停用；请使用 tools/迭代.py snapshot --task m2。')
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--stage', required=True, type=int, choices=(1, 2, 3, 4))
    ap.add_argument('--defer-list', type=Path)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--check-only', action='store_true')
    args = ap.parse_args()
    out = args.output.resolve()
    if out.name != 'dataset_snapshot' or out.parent.parent != (ROOT / 'train').resolve():
        ap.error('output must be train/<run>/dataset_snapshot')
    if out.exists():
        raise FileExistsError('Snapshot already exists: ' + str(out))
    plan = read_plan('m2')
    if plan is None:
        raise ValueError('Fixed splits.csv is required')
    images = {p.stem: p for p in (ROOT / 'data/m2/images').glob('*.jpg')}
    cache = {}
    def get_state(name):
        if name not in cache:
            cache[name] = source_state(name)
        return cache[name]
    deferred = validate_deferrals(read_csv(args.defer_list) if args.defer_list else [], plan, get_state)
    def get_size(name):
        with Image.open(images[name]) as image:
            return image.size
    selected, clips, excluded = select_rows(plan, images, args.stage, deferred, get_state, get_size)
    counts = Counter(row[1]['split'] for row in selected)
    subject_counts = Counter(row[1]['subject_original'] for row in selected if row[1]['split'] == 'train' and row[1]['stage'] > 0)
    report = {'stage': args.stage, 'counts': dict(counts), 'new_train_subject_counts': dict(subject_counts),
              'clipping': clips, 'clipped_count': len(clips), 'deferred': excluded,
              'deferred_count': len(excluded), 'split_source': str(ROOT / 'data/m2/splits.csv'),
              'split_sha256': sha(ROOT / 'data/m2/splits.csv'),
              'defer_source': str(args.defer_list) if args.defer_list else None,
              'defer_sha256': sha(args.defer_list) if args.defer_list else None,
              'labels': 'strict event replay; no log mutation', 'image_policy': 'independent file copies',
              'clipping_limit': 'each edge: max(1 percent of axis, 2 pixels)'}
    if args.check_only:
        print(json.dumps(report, ensure_ascii=False))
        return
    # All identities, review states and labels passed before creating the snapshot.
    for sub in ('images/train', 'images/val', 'labels/train', 'labels/val'):
        (out / sub).mkdir(parents=True, exist_ok=False)
    fields = ['new_name', 'split', 'stage', 'group', 'subject_original', 'log_sha256']
    with (out / 'selection.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for name, identity, boxes, log_hash in selected:
            split = identity['split']
            shutil.copy2(images[name], out / 'images' / split / (name + '.jpg'))
            (out / 'labels' / split / (name + '.txt')).write_text(
                ''.join('%d %.8f %.8f %.8f %.8f\n' % b for b in boxes), encoding='utf-8')
            writer.writerow({**{k: identity[k] for k in fields if k != 'log_sha256'}, 'log_sha256': log_hash})
    (out / 'data.yaml').write_text('path: ' + str(out).replace('\\', '/') +
        "\ntrain: images/train\nval: images/val\nnc: 2\nnames: ['num', 'option']\n", encoding='utf-8')
    (out.parent / 'snapshot_audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'snapshot': str(out), 'counts': dict(counts), 'deferred': len(excluded),
                      'clipped': len(clips), 'new_subject_counts': dict(subject_counts)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
