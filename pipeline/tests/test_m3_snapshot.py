"""M3分批准入回归；所有假数据写入显式提供的D盘工作目录。"""
import argparse
import csv
import sys
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import yolo_snapshot as snapshot


def main(workdir):
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=False)
    data, train = root / 'data', root / 'train'
    images = data / 'm3' / 'images'
    images.mkdir(parents=True)
    names = ['good', 'bad', 'unreviewed', 'val1', 'val2', 'reserved']
    plan = {n: dict(new_name=n, split='val' if n.startswith('val') or n == 'reserved' else 'train',
                    stage=0, group=n, subject_original='fake') for n in names}
    states = {n: dict(reviewed=n != 'unreviewed', special=False, boxes=[[5, .5, .5, .4, .3]]) for n in names}
    states['bad']['boxes'] = [[5, .5, .5, 1.01, .3], [4, .5, .5, .4, .3]]
    for n in names:
        Image.new('RGB', (100, 100)).save(images / (n + '.jpg'))

    def write_list(path, selected):
        with path.open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=['new_name'])
            writer.writeheader()
            writer.writerows({'new_name': n} for n in selected)

    baseline, subset = root / 'baseline.csv', root / 'subset.csv'
    write_list(baseline, ['val1', 'val2'])
    write_list(subset, ['good', 'bad', 'val1', 'val2'])

    def rejects(call, fragment):
        try:
            call()
        except ValueError as error:
            assert fragment in str(error), str(error)
        else:
            raise AssertionError('应拒绝：' + fragment)

    with patch.object(snapshot, 'ROOT', root), patch.object(snapshot, 'DATA', data), \
            patch.object(snapshot, 'TRAIN', train), patch.object(snapshot, 'read_plan', lambda task: plan), \
            patch.object(snapshot.events, 'load_state', lambda task, name: states[name]), \
            patch.dict(snapshot.VALIDATION_BASELINES, m3=('baseline.csv', 2)):
        output = train / 'm3_fake' / 'dataset_snapshot'
        rejects(lambda: snapshot.freeze('m3', None, output, stage=0), '显式指定')
        rejects(lambda: snapshot.freeze('m3', subset, output, stage=0), '非法类别')
        audit = snapshot.freeze('m3', subset, output, stage=0, skip_invalid=True)
        assert audit['counts'] == {'train': 1, 'val': 2}
        assert len(audit['skipped_invalid']) == 1 and not audit['clips']
        assert audit['reserved_val'] == 1 and audit['outside_review_batch'] == 1
        snapshot.validate_training_input('m3', output.parent, output / 'data.yaml')
        assert not (output / 'images').exists()
        assert (output / 'labels/train/good.txt').read_text().startswith('0 ')
        write_list(subset, ['good', 'unreviewed'])
        rejects(lambda: snapshot.freeze('m3', subset, train / 'next', stage=0, check_only=True), '未审')
        states['val1']['boxes'] = [[4, .5, .5, .4, .3]]
        write_list(subset, ['good'])
        rejects(lambda: snapshot.freeze('m3', subset, train / 'next', stage=0, skip_invalid=True, check_only=True), '非法类别')
        write_list(baseline, ['val1'])
        rejects(lambda: snapshot.validation_names('m3', plan), '名单变化')
    print('M3准入回归通过：固定验证、显式分批、拒绝未审/坏验证、坏训练留据、独立快照。')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--workdir', required=True)
    main(parser.parse_args().workdir)
