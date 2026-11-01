"""从已审M2最终框增量裁小图、写待审预测；不覆盖旧库、不自动审核。"""
import argparse
from collections import Counter
from pathlib import Path
import math
import re
import sys

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.paths import DATA, ROOT
from common import cnn_data, events, row_log
from common.fixed_dataset import read_plan
from common.records import read_csv, write_json, write_csv, now, lock
from common.model_registry import current
from common.digit_classes import MAX_DIGIT
from stages.choice import _crop, _box_px, crop_option_row
from stages.match import match_nums_to_options


def known_stems(task):
    seen = set()
    field = 'labels.csv' if task == 'digit' else 'manifest.csv'
    for base, _ in cnn_data.data_bases(task):
        if (base / field).exists():
            seen.update(r['stem'] for r in read_csv(base / field))
    return seen


def source_plan(subset=None):
    plan = read_plan('m2') or {}
    names = sorted(plan) if subset is None else [r['new_name'] for r in read_csv(subset)]
    if len(names) != len(set(names)):
        raise ValueError('来源清单重复')
    out, skipped = [], []
    for stem in names:
        if stem not in plan:
            raise ValueError('M2分区缺少：' + stem)
        identity = plan[stem]
        st = events.load_state('m2', stem)
        if identity['split'] not in ('train', 'val', 'test') or not st['reviewed'] or st['special']:
            skipped.append({'stem': stem, 'reason': '非可用分区、未审或特殊'})
            continue
        p = DATA / 'm2' / 'images' / (stem + '.jpg')
        if not p.is_file():
            raise FileNotFoundError(p)
        with Image.open(p) as im:
            width, height = im.size
        for b in st['boxes']:
            if len(b) != 5 or b[0] not in (2, 3) or not all(math.isfinite(v) for v in b):
                raise ValueError('非法M2框：' + stem)
            if b[3] <= 0 or b[4] <= 0:
                raise ValueError('无面积M2框：' + stem)
            for c, size, pixels in ((b[1], b[3], width), (b[2], b[4], height)):
                tolerance = max(.01, 2/pixels)
                if c-size/2 < -tolerance or c+size/2 > 1+tolerance:
                    raise ValueError('M2框超限出界：' + stem)
        out.append({'stem': stem, 'image': p, 'boxes': st['boxes'],
                    'split': identity['split'], 'group': identity['group'] or stem.split('.选择题区')[0],
                    'stage': identity['stage'], 'subject': identity['subject_original'],
                    'm2_reviewed_at': st['reviewed_at']})
    return out, skipped


def crops(img, boxes):
    """所有题号及选项框均保留；孤立选项不强配，仍可单独审核答案。"""
    h, w = img.shape[:2]
    nums = [b[1:] for b in boxes if b[0] == 2]
    opts = [b[1:] for b in boxes if b[0] == 3]
    nums_px = [(x*w, y*h, bw*w, bh*h) for x, y, bw, bh in nums]
    opts_px = [(x*w, y*h, bw*w, bh*h) for x, y, bw, bh in opts]
    pairs = {j: i for i, j, _ in match_nums_to_options(nums_px, opts_px)}
    digits = [(i, b, _crop(img, *b)) for i, b in enumerate(nums)]
    choices = []
    for j, b in enumerate(opts):
        bounds = _box_px(*b, w, h)
        i = pairs.get(j)
        # 空列表确保孤立选项不使用附近未配对题号裁切。
        crop = crop_option_row(img, [], *bounds, paired=nums_px[i] if i is not None else None)
        choices.append((j, b, i, bounds, crop))
    if any(c.size == 0 for _, _, c in digits) or any(c.size == 0 for *_, c in choices):
        raise ValueError('裁出空小图')
    return digits, choices


def save_png(path, crop):
    ok, encoded = cv2.imencode('.png', crop)
    if not ok:
        raise ValueError('图片编码失败：' + str(path))
    with path.open('xb') as f:
        f.write(encoded.tobytes())


def prepare(batch, subset=None, check_only=False):
    if not re.fullmatch(r'[\w-]{1,60}', batch):
        raise ValueError('批次名只能使用文字、数字、下划线或连字符')
    destinations = {t: DATA / t / 'increments' / batch for t in ('digit', 'choice')}
    for p in destinations.values():
        if p.exists():
            raise FileExistsError('批次已存在，禁止覆盖：' + str(p))
    sources, skipped = source_plan(subset)
    known = {t: known_stems(t) for t in destinations}
    selected = {t: [r for r in sources if r['stem'] not in known[t]] for t in destinations}
    summary = {t: {'source_count': len(rows), 'splits': dict(Counter(r['split'] for r in rows)),
                    'subjects': dict(Counter(r['subject'] for r in rows)),
                    'duplicate_sources_skipped': len(sources)-len(rows)} for t, rows in selected.items()}
    print(summary, flush=True)
    if check_only:
        return summary
    from cnn.digit import DigitReader
    from cnn.choice import ChoiceReader
    weights = {t: current(t) for t in destinations}
    dr, cr = DigitReader(weights['digit']), ChoiceReader(weights['choice'])
    rows = {t: [] for t in destinations}
    for p in destinations.values():
        (p / 'images').mkdir(parents=True)
    for count, source in enumerate(sources, 1):
        stem = source['stem']
        tasks = [t for t in destinations if stem not in known[t]]
        if not tasks:
            continue
        img = cv2.imdecode(np.frombuffer(source['image'].read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError('图片无法读取：' + stem)
        digits, choices = crops(img, source['boxes'])
        identity = {k: source[k] for k in ('split', 'group', 'stage', 'subject', 'm2_reviewed_at')}
        numbers = {}
        for i, box, crop in digits:
            no, conf = dr.read(crop)
            numbers[i] = no
            if 'digit' not in tasks:
                continue
            if not 0 <= no <= MAX_DIGIT:
                raise ValueError('模型预测题号超范围')
            name = '%s_%03d' % (stem, i+1)
            save_png(destinations['digit'] / 'images' / (name+'.png'), crop)
            rows['digit'].append({'file': name+'.png', 'label': no, 'stem': stem, **identity,
                                  'box': box, 'confidence': conf})
            row_log.append(destinations['digit'], name, {'事件': '预标注', '标签': no,
                '置信度': conf, '模型': str(weights['digit'].relative_to(ROOT)),
                '来源图': stem, '来源框': box, '说明': '仅模型建议，待人工确认；未做位置推断'})
        for j, box, i, bounds, crop in choices:
            if 'choice' not in tasks:
                continue
            answer, conf = cr.read(crop)
            name = '%s_%03d' % (stem, j+1)
            save_png(destinations['choice'] / 'images' / (name+'.png'), crop)
            rows['choice'].append({'stem': stem, 'row': j+1, 'no': numbers.get(i, 0),
                'n_opts': 6, 'width': crop.shape[1], 'height': crop.shape[0],
                'x1': bounds[0], 'y1': bounds[1], 'x2': bounds[2], 'y2': bounds[3],
                **identity, 'box': box, 'num_index': i, 'confidence': conf})
            row_log.append(destinations['choice'], name, {'事件': '预标注', '预测答案': answer,
                '置信度': conf, '模型': str(weights['choice'].relative_to(ROOT)),
                '来源图': stem, '来源框': box, '配对题号框': i,
                '说明': '仅模型建议，待人工确认；题号读数也未审核；支持A-F，不猜选项数'})
        if count % 50 == 0:
            print('processed %d/%d, digit %d, choice %d' % (count,len(sources),len(rows['digit']),len(rows['choice'])), flush=True)
    for task, p in destinations.items():
        fields = (['file','label','split','stem','group','stage','subject','m2_reviewed_at','box','confidence']
                  if task == 'digit' else ['stem','row','no','n_opts','width','height','x1','y1','x2','y2',
                      'split','group','stage','subject','m2_reviewed_at','box','num_index','confidence'])
        write_csv(p / ('labels.csv' if task == 'digit' else 'manifest.csv'), rows[task], fields)
        write_json(p / 'batch.json', {'task': task, 'batch': batch, 'complete': True, 'created': now(),
            'max_digit': MAX_DIGIT, 'samples': len(rows[task]), 'summary': summary[task],
            'sources': [{k: str(v) if isinstance(v,Path) else v for k,v in r.items()} for r in selected[task]],
            'skipped': skipped, 'model': str(weights[task].relative_to(ROOT)),
            'admission': '仅人工已审train；新val/test预留；旧验证名单保持不变'})
    return summary


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--batch', required=True)
    ap.add_argument('--subset', help='可选M2来源清单，字段new_name')
    ap.add_argument('--check-only', action='store_true')
    args = ap.parse_args()
    with lock(DATA / '.cnn_increment.lock'):
        prepare(args.batch, args.subset, args.check_only)


if __name__ == '__main__':
    main()
