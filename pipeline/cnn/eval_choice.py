"""在同一冻结验证名单比较涂黑TorchScript候选；不写审核、不部署。"""
import argparse
import io
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.cnn_data import load_frozen
from common.records import read_json, write_json, sha
from train_choice import prep, LETTERS


def evaluate(items, weights, layouts):
    torch.set_num_threads(2)
    x = torch.from_numpy(np.stack([prep(r['img']) for r in items])).unsqueeze(1)
    report = {}
    for label, path in weights:
        model = torch.jit.load(io.BytesIO(Path(path).read_bytes()), map_location='cpu').eval()
        with torch.no_grad():
            p = torch.cat([model(t).sigmoid() for t in x.split(128)]).numpy()
        groups = defaultdict(lambda: {'total': 0, 'correct': 0, 'errors': []})
        predictions = []
        for r, probs in zip(items, p):
            answer = ''.join(c for c, v in zip(LETTERS, probs) if v >= 0.5)
            ok = answer == r['answer']
            layout = str(layouts.get(r['file'], 'unknown'))
            predictions.append({'file': r['file'], 'truth': r['answer'], 'prediction': answer,
                                'layout': layout, 'probabilities': probs.tolist()})
            for key in ('all', 'layout_' + layout, 'source_' + r['stem']):
                groups[key]['total'] += 1
                groups[key]['correct'] += int(ok)
                if not ok:
                    groups[key]['errors'].append(predictions[-1])
        for group in groups.values():
            group['exact'] = group['correct'] / group['total']
        report[label] = {'weight': str(path), 'sha256': sha(path),
                         'groups': dict(groups), 'predictions': predictions}
    return report


def main():
    ap = argparse.ArgumentParser(__doc__)
    ap.add_argument('--data', type=Path, required=True)
    ap.add_argument('--weight', nargs=2, action='append', required=True, metavar=('LABEL', 'PATH'))
    ap.add_argument('--layouts', type=Path, help='目视核对的选项数量清单；缺失项不猜测')
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    items = [r for r in load_frozen(args.data, 'choice') if r['split'] == 'val']
    if not items:
        raise ValueError('验证名单为空')
    layouts = read_json(args.layouts, {}).get('items', {}) if args.layouts else {}
    result = {'snapshot_sha256': sha(args.data/'selection.json'),
              'scope': '固定英语验证；参与选模，不是独立测试集；旧模型可能见过同页其他区域',
              'models': evaluate(items, args.weight, layouts)}
    write_json(args.output, result)
    for name, model in result['models'].items():
        print(name, model['groups']['all'])


if __name__ == '__main__':
    main()
