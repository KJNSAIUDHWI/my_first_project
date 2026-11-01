"""整页识别与拼图并发；仅输出本次结果，不评分、不回写训练库。"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import inspect
import json
import math
from pathlib import Path
import sys
import time

import cv2
import numpy as np
from PIL import Image, ImageOps
from scipy.optimize import linear_sum_assignment

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.paths import ROOT, DATA, MODELS
from common.model_registry import current
from common.recognition_api import SquarePacker, DoubaoClient, digest, save_json, MODEL, PROMPT

DEFAULT_SELECTION = ROOT/'models/对比/m3/20261030_物理20页全链路框回归_1811/selection.json'
SETTINGS = {'device': 'cpu', 'precision': 'FP32', 'imgsz': 1024,
            'conf': {'m1': .2, 'm2': .5, 'm3': .3}, 'iou': .5, 'm3_rect': False}


def pair_fill(boxes):
    big = [(i, b['box']) for i, b in enumerate(boxes) if b['cls'] == 5]
    nums = [(i, b['box']) for i, b in enumerate(boxes) if b['cls'] == 6]
    if not big or not nums:
        return []
    cost = np.full((len(big), len(nums) + len(big)), 1e6)
    cost[:, len(nums):] = 100
    for i, (_, a) in enumerate(big):
        for j, (_, b) in enumerate(nums):
            nw, nh = b[2]-b[0], b[3]-b[1]
            cx, cy = (b[0]+b[2])/2, (b[1]+b[3])/2
            intersection = max(0, min(a[2], b[2])-max(a[0], b[0])) * max(0, min(a[3], b[3])-max(a[1], b[1]))
            if nw*nh <= 0 or intersection/(nw*nh) < .5:
                continue
            if a[0]-nw/2 <= cx <= a[2]+nw/2 and a[1]-nh/2 <= cy <= a[3]+nh/2:
                cost[i, j] = abs(cx-a[0])/nw + abs(cy-a[1])/nh
    rows, cols = linear_sum_assignment(cost)
    return [(big[i][0], nums[j][0], float(cost[i, j])) for i, j in zip(rows, cols) if j < len(nums) and cost[i, j] < 100]


def bounds(box, width, height):
    return [max(0, math.floor(box[0])), max(0, math.floor(box[1])),
            min(width, math.ceil(box[2])), min(height, math.ceil(box[3]))]


def crop(image, box):
    b = bounds(box, *image.size)
    if b[2] <= b[0] or b[3] <= b[1]:
        raise ValueError('无面积裁区')
    return image.crop(b)


def translated(box, x, y):
    return [box[0]+x, box[1]+y, box[2]+x, box[3]+y]


class Recognizer:
    def __init__(self):
        import torch
        from ultralytics import YOLO
        from cnn.choice import ChoiceReader
        from cnn.printed_number import PaddleNumberReader
        cv2.setNumThreads(1)
        torch.set_num_threads(4)
        self.models = {task: YOLO(str(current(task))) for task in ('orient', 'm1', 'm2', 'm3')}
        self.numbers = PaddleNumberReader(MODELS/'pretrained/PP-OCRv6_medium_rec_onnx')
        self.choices = ChoiceReader(current('choice'), device='cpu')

    def infer(self, task, image):
        kwargs = dict(imgsz=1024, device='cpu', half=False, conf=SETTINGS['conf'][task], iou=.5, verbose=False)
        if task == 'm3':
            kwargs['rect'] = False
        raw = self.models[task](image, **kwargs)[0].boxes.data.cpu().numpy()
        if not np.isfinite(raw).all():
            raise ValueError(task + '检测存在非有限值')
        mapping = {'m1': {0: 0, 1: 1, 2: 4}, 'm2': {0: 2, 1: 3}, 'm3': {0: 5, 1: 6}}[task]
        result = []
        for row in raw:
            if int(row[5]) not in mapping or row[2] <= row[0] or row[3] <= row[1]:
                raise ValueError(task + '类别或框异常')
            if min(row[2]-row[0], row[3]-row[1]) >= (16 if task == 'm1' else 6):
                result.append({'cls': mapping[int(row[5])], 'box': row[:4].tolist(), 'confidence': float(row[4]), 'stage': task})
        return result

    def page(self, source, index, out, working=False):
        from stages.choice import read_choice_zone, _crop
        from orient.deskew import detect
        start = time.monotonic()
        image = ImageOps.exif_transpose(Image.open(source)).convert('RGB')
        orient = self.models['orient'](image, imgsz=1024, device='cpu', verbose=False)[0]
        angle = int(orient.names[int(orient.probs.top1)])
        orientation = {'prediction': angle, 'confidence': float(orient.probs.top1conf), 'working_pixels_preserved': working}
        if not working:
            image = image.rotate(-angle, expand=True, fillcolor='white')
            correction, method = detect(np.array(image.convert('L')))
            orientation.update(deskew=correction, deskew_method=method)
            if abs(correction) >= .1:
                image = image.rotate(correction, expand=True, resample=Image.Resampling.BICUBIC, fillcolor='white')
        pid = f'p{index:03d}'
        (out/'images').mkdir(exist_ok=True)
        (out/'crops').mkdir(exist_ok=True)
        image.save(out/'images'/(pid+'.jpg'), quality=95)
        thumbnail = image.copy()
        thumbnail.thumbnail((280, 400))
        thumbnail.save(out/'images'/(pid+'_thumb.jpg'))
        page = {'id': pid, 'name': source.stem, 'source': str(source), 'source_sha256': digest(source.read_bytes()),
                'image': 'images/'+pid+'.jpg', 'thumbnail': 'images/'+pid+'_thumb.jpg', 'width': image.width,
                'height': image.height, 'orientation': orientation, 'boxes': [], 'items': [], 'issues': [], 'timings': {}}
        page['boxes'] = self.infer('m1', image)
        tiles = []
        def add(kind, box, number=None, issues=None, extra=None):
            ident = pid + '_' + str(len(page['items'])).zfill(3)
            im = crop(image, box)
            path = 'crops/' + ident + '.png'
            im.save(out/path)
            item = {'id': ident, 'page_id': pid, 'kind': kind, 'box': bounds(box, *image.size), 'crop': path,
                    'number': number, 'issues': list(issues or []), 'status': '等待豆包' if kind != 'choice' else '已识别', **(extra or {})}
            page['items'].append(item)
            if kind in ('fill', 'identity'):
                tiles.append(({'id': ident, 'kind': kind, 'page_id': pid, 'number': number, 'crop': path}, im))
            return item
        for zi, zone in enumerate(list(page['boxes'])):
            area = bounds(zone['box'], *image.size)
            x, y, right, bottom = area
            if zone['cls'] == 0:
                add('identity', area)
                continue
            task = 'm2' if zone['cls'] == 1 else 'm3'
            try:
                local_image = crop(image, area)
                local = self.infer(task, local_image)
                page['boxes'].extend({**b, 'box': translated(b['box'], x, y), 'zone': zi} for b in local)
                bgr = cv2.cvtColor(np.array(local_image), cv2.COLOR_RGB2BGR)
                norm = [[b['cls'], (b['box'][0]+b['box'][2])/2/local_image.width,
                         (b['box'][1]+b['box'][3])/2/local_image.height,
                         (b['box'][2]-b['box'][0])/local_image.width, (b['box'][3]-b['box'][1])/local_image.height] for b in local]
                if task == 'm2':
                    rows = read_choice_zone(bgr, norm, self.numbers, self.choices, fix_no=False)
                    paired_num, paired_opt = set(), set()
                    for row in rows:
                        ob, nb = row['opt_box'], row['num_box']
                        paired_num.add(tuple(nb)); paired_opt.add(tuple(ob))
                        rect = [(ob[0]-ob[2]/2)*local_image.width, (ob[1]-ob[3]/2)*local_image.height,
                                (ob[0]+ob[2]/2)*local_image.width, (ob[1]+ob[3]/2)*local_image.height]
                        issues = [] if row['valid'] else ['题号或涂黑结果待核对']
                        union = [min(rect[0], (nb[0]-nb[2]/2)*local_image.width), min(rect[1], (nb[1]-nb[3]/2)*local_image.height),
                                 max(rect[2], (nb[0]+nb[2]/2)*local_image.width), max(rect[3], (nb[1]+nb[3]/2)*local_image.height)]
                        item = add('choice', translated(union, x, y), row['no'], issues, {'answer': row['answer'], 'recognition': row})
                        # Exact CNN input is separate from the human context crop.
                        from stages.choice import crop_option_row, _box_px
                        nc = (nb[0]*local_image.width, nb[1]*local_image.height, nb[2]*local_image.width, nb[3]*local_image.height)
                        cin = crop_option_row(bgr, [], *_box_px(*ob, local_image.width, local_image.height), paired=nc)
                        cp = 'crops/' + item['id'] + '_cnn.png'
                        Image.fromarray(cv2.cvtColor(cin, cv2.COLOR_BGR2RGB)).save(out/cp)
                        item['model_input'] = cp
                    for b in norm:
                        paired = paired_num if b[0] == 2 else paired_opt
                        if tuple(b[1:]) not in paired:
                            rect = [(b[1]-b[3]/2)*local_image.width, (b[2]-b[4]/2)*local_image.height,
                                    (b[1]+b[3]/2)*local_image.width, (b[2]+b[4]/2)*local_image.height]
                            orphan = add('unmatched', translated(rect, x, y), issues=['未配对题号框' if b[0] == 2 else '未配对选项框'])
                            orphan['status'] = '待复核'
                    if not rows:
                        page['issues'].append('选择题区未配出答案行')
                else:
                    pairs = pair_fill(local)
                    number_for_big = {bi: ni for bi, ni, _ in pairs}
                    used_numbers = {ni for _, ni, _ in pairs}
                    for bi, b in enumerate(local):
                        if b['cls'] != 5:
                            continue
                        issues, number, number_detail = [], None, None
                        if bi in number_for_big:
                            nb = local[number_for_big[bi]]['box']
                            cx, cy = (nb[0]+nb[2])/2/local_image.width, (nb[1]+nb[3])/2/local_image.height
                            ni = _crop(bgr, cx, cy, (nb[2]-nb[0])/local_image.width, (nb[3]-nb[1])/local_image.height)
                            number_detail = self.numbers.read_details([ni])[0]
                            number = number_detail['number']
                            if not number or number_detail['confidence'] < .3:
                                issues.append('Paddle大题号未可靠识别')
                        else:
                            issues.append('大题未配到题号框')
                        add('fill', translated(b['box'], x, y), number, issues, {'number_detail': number_detail})
                    for ni, b in enumerate(local):
                        if b['cls'] == 6 and ni not in used_numbers:
                            item = add('unmatched', translated(b['box'], x, y), issues=['未配对大题号框'])
                            item['status'] = '待复核'
                    if not any(b['cls'] == 5 for b in local):
                        page['issues'].append('填空区未检测出大题')
            except Exception as exc:
                page['issues'].append(task + '区域失败：' + type(exc).__name__ + ' ' + str(exc)[:200])
        if not page['boxes']:
            page['issues'].append('未找到可识别的答题区')
        for kind in ('fill', 'choice'):
            counts = Counter(i['number'] for i in page['items'] if i['kind'] == kind and i['number'])
            for item in page['items']:
                if item['kind'] == kind and item['number'] and counts[item['number']] > 1:
                    item['issues'].append('本页重复题号，结果分别保留')
        page['status'] = '本地识别完成，等待豆包'
        page['timings']['detection_seconds'] = round(time.monotonic()-start, 3)
        return page, tiles


def apply_responses(pages, grids, responses):
    lookup = {i['id']: i for p in pages for i in p['items']}
    for grid, response in zip(grids, responses):
        for tile in grid['items']:
            item = lookup[tile['id']]
            item.update(grid=grid['image'], grid_id=grid['id'], grid_box=tile['grid_box'], scale=tile['scale'])
            item['issues'] = [x for x in item['issues'] if not x.startswith('豆包：')]
            item.pop('transcription', None)
            parsed = response.get('items', {}).get(item['id'])
            if not parsed:
                item['status'] = '调用失败' if not response.get('ok') else '结构异常'
                item['issues'].append('豆包：' + response.get('errors', {}).get(item['id'], response.get('error', '缺图块输出')))
                continue
            item['transcription'] = parsed
            item['status'] = '已识别'
            if item['kind'] == 'identity':
                if any(parsed[k] == '看不清' for k in ('name', 'sid')):
                    item['issues'].append('豆包：身份有看不清字段')
            else:
                number = parsed['question_number']
                if not number.isdecimal() or not item['number'] or int(number) != item['number']:
                    item['issues'].append('豆包：大题号与Paddle结果不一致或未确定')
                answers = parsed['answers']
                if any('.' in key for blanks in answers.values() for key in blanks):
                    item['issues'].append('豆包：多级空位按原编号展示，请核对小题对应')
                if '小题号看不清' in answers:
                    item['issues'].append('豆包：小题号看不清')
                if '无小题号' in answers and len(answers) > 1:
                    item['issues'].append('豆包：同题同时有编号与无编号空位，请核对归属')
                if any(v == '看不清' for blanks in answers.values() for v in blanks.values()):
                    item['issues'].append('豆包：手写有看不清空位')
            if response.get('extra_ids'):
                item['issues'].append('豆包：本拼图含额外输出ID，已隔离未绑定')
    for page in pages:
        if page.get('error'):
            page['status'] = '本地识别失败'
        elif any(i['status'] in ('调用失败', '结构异常') for i in page['items']):
            page['status'] = '部分识别失败'
        else:
            page['status'] = '识别完成，待人工验收'


def run(args):
    start = time.monotonic()
    out = args.out.resolve()
    if out == ROOT or out.is_relative_to(DATA.resolve()):
        raise ValueError('输出不能覆盖项目根或训练数据目录')
    out.mkdir(parents=True, exist_ok=True)
    if args.input:
        sources = sorted(p for p in args.input.iterdir() if p.suffix.lower() in ('.jpg', '.jpeg', '.png'))
    else:
        selected = json.loads(args.selection.read_text(encoding='utf-8'))['selected']
        sources = [DATA/'m1/images'/(r['new_name']+'.jpg') for r in selected]
    if not sources:
        raise ValueError('输入名单为空')
    working = args.working_images or not args.input
    model_info = {t: {'path': str(current(t)), 'sha256': digest(current(t).read_bytes())} for t in ('orient','m1','m2','m3','choice')}
    paddle_dir = MODELS/'pretrained/PP-OCRv6_medium_rec_onnx'
    paddle_info = {p.name: digest(p.read_bytes()) for p in sorted(paddle_dir.iterdir()) if p.is_file()}
    manifest = {'schema': 1, 'sources': [{'path': str(p), 'sha256': digest(p.read_bytes()) if p.exists() else None} for p in sources],
                'models': model_info, 'paddle': paddle_info, 'settings': SETTINGS, 'working_images': working,
                'detection_code_sha256': digest(inspect.getsource(Recognizer).encode()),
                'choice_matching_sha256': digest((Path(__file__).parent/'stages/match.py').read_bytes()), 'grid_size': 1500}
    frozen = out/'manifest.json'
    if frozen.exists() and json.loads(frozen.read_text(encoding='utf-8')) != manifest:
        raise ValueError('当前输入、代码或模型不同于原运行；请指定新的输出目录')
    save_json(frozen, manifest)
    (out/'prompt.txt').write_text(PROMPT, encoding='utf-8')
    pages, grids, futures = [], [], {}
    client = DoubaoClient(out)
    packers = {'fill': SquarePacker(out), 'identity': SquarePacker(out, prefix='i')}
    recognizer = None
    def checkpoint(phase):
        save_json(out/'results.json', {'pages': pages, 'grids': grids, 'model': MODEL, 'status': phase,
                                     'settings': SETTINGS, 'elapsed_seconds': round(time.monotonic()-start, 2),
                                     'note': '仅识别，无标准答案、未评分；正确性须人工核对；工作图已转正，未二次旋转。'})
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        def submit(grid):
            if grid:
                grids.append(grid)
                if not args.detect_only:
                    futures[pool.submit(client.call, grid)] = grid['id']
        for index, source in enumerate(sources):
            cache = out/'pages'/f'p{index:03d}.json'
            try:
                if cache.exists():
                    page = json.loads(cache.read_text(encoding='utf-8'))
                    tiles = [({'id': i['id'], 'kind': i['kind'], 'page_id': page['id'], 'number': i['number'], 'crop': i['crop']},
                              Image.open(out/i['crop']).convert('RGB')) for i in page['items'] if i['kind'] in ('fill','identity')]
                else:
                    if recognizer is None:
                        recognizer = Recognizer()
                    page, tiles = recognizer.page(source, index, out, working)
                    save_json(cache, page)
            except Exception as exc:
                page = {'id': f'p{index:03d}', 'name': source.stem, 'source': str(source), 'items': [], 'boxes': [],
                        'issues': ['本地失败：' + type(exc).__name__ + ' ' + str(exc)[:250]], 'error': type(exc).__name__, 'status': '本地识别失败'}
                tiles = []
            pages.append(page)
            for item, image in tiles:
                submit(packers[item['kind']].add(item, image))
            checkpoint('本地检测与拼图处理中')
            print(json.dumps({'page': index+1, 'total': len(sources), 'items': len(page['items']), 'grids': len(grids),
                              'error': page.get('error'), 'elapsed_seconds': round(time.monotonic()-start, 1)}), flush=True)
        for packer in packers.values():
            submit(packer.finish())
        records = {}
        if args.detect_only:
            responses = [{'error': '尚未调用豆包（detect-only）', 'ok': False} for _ in grids]
        else:
            for future in as_completed(futures):
                record = future.result()
                records[futures[future]] = record
                print(json.dumps({'grid': record['grid'], 'ok': record['ok'], 'reused': record.get('reused', False),
                                  'seconds': record.get('seconds'), 'error': record.get('error')}, ensure_ascii=True), flush=True)
            responses = [records[g['id']] for g in grids]
        apply_responses(pages, grids, responses)
        # One bounded repair round for structurally invalid blocks only; never redo successful blocks.
        if not args.detect_only:
            repair_packer = SquarePacker(out, prefix='r')
            repair_grids = []
            for page in pages:
                for item in page['items']:
                    if item['status'] == '结构异常' and item['kind'] in ('fill','identity'):
                        tile = {k: item[k] for k in ('id','kind','page_id','number','crop')}
                        finished = repair_packer.add(tile, Image.open(out/item['crop']).convert('RGB'))
                        if finished:
                            repair_grids.append(finished)
            finished = repair_packer.finish()
            if finished:
                repair_grids.append(finished)
            repair_jobs = {pool.submit(client.call,g):g['id'] for g in repair_grids}
            repair_responses = {}
            for future in as_completed(repair_jobs):
                repair_responses[repair_jobs[future]] = future.result()
            ordered = [repair_responses[g['id']] for g in repair_grids]
            apply_responses(pages, repair_grids, ordered)
            grids.extend(repair_grids)
            responses.extend(ordered)
    all_records = [json.loads(p.read_text(encoding='utf-8')) for p in (out/'api').glob('*.json')] if (out/'api').exists() else []
    stats = {'pages': len(pages), 'page_failures': sum(bool(p.get('error')) for p in pages),
             'grids': len(grids), 'api_success': sum(bool(r.get('ok')) for r in responses),
             'api_failures': sum(not r.get('ok') for r in responses),
             'reused': sum(bool(r.get('reused')) for r in responses),
             'prompt_tokens': sum(r.get('usage', {}).get('prompt_tokens', 0) for r in all_records),
             'completion_tokens': sum(r.get('usage', {}).get('completion_tokens', 0) for r in all_records),
             'attempts': sum(len(r.get('attempts', [])) for r in all_records),
             'new_api_requests': sum(not r.get('reused',False) for r in responses),
             'structural_failures': sum(i['status']=='结构异常' for p in pages for i in p['items']),
             'items_with_transcription': sum('transcription' in i for p in pages for i in p['items']),
             'api_sum_seconds': round(sum(r.get('seconds', 0) for r in responses), 2),
             'detection_sum_seconds': round(sum(p.get('timings', {}).get('detection_seconds', 0) for p in pages), 2),
             'wall_seconds': round(time.monotonic()-start, 2), 'concurrency': args.concurrency,
             'human_accuracy': None, 'api_extra_ids': {g['id']: r['extra_ids'] for g,r in zip(grids,responses) if r.get('extra_ids')}}
    result = {'pages': pages, 'grids': grids, 'model': MODEL, 'settings': SETTINGS, 'stats': stats,
              'status': '待人工验收' if not stats['api_failures'] else '有未完成调用，待补跑',
              'note': '只识别，不评分。未标注的空白区域也可能有漏检，请结合整页原图核对。工作图保留原转正像素；正反面不归并。'}
    save_json(out/'results.json', result)
    print(json.dumps(stats), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--input', type=Path, help='新扫描图目录；不提供则沿用固定20页')
    parser.add_argument('--selection', type=Path, default=DEFAULT_SELECTION)
    parser.add_argument('--working-images', action='store_true', help='已转正工作图，保留像素，不再次旋转')
    parser.add_argument('--detect-only', action='store_true')
    parser.add_argument('--concurrency', type=int, default=4, choices=range(1, 9))
    args = parser.parse_args()
    run(args)


if __name__ == '__main__':
    main()
