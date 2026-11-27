# -*- coding: utf-8 -*-
"""整页识别与流式并发流水线（GPU 优先 + 批量 YOLO/OCR + 异步拼图与增量合并）；仅输出本次结果，不评分、不回写训练库。"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import inspect
import json
import math
import os
os.environ.setdefault('PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK', 'True')
from pathlib import Path
import sys
import time
import warnings

import cv2
import numpy as np
from PIL import Image, ImageOps
from scipy.optimize import linear_sum_assignment

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.paths import ROOT, DATA, MODELS
from common.model_registry import current
from common.recognition_api import (
    SquarePacker,
    DoubaoClient,
    VlmClient,
    digest,
    save_json,
    load_vlm_config,
    MODEL,
    PROMPT,
)
from common.runtime_diag import (
    EXIT_OK,
    EXIT_GENERAL_ERROR,
    EXIT_ALL_PAGES_FAILED,
    EXIT_ENV_MISSING,
    EXIT_API_CONFIG_ERROR,
    EXIT_IO_PERMISSION,
    EXIT_MANIFEST_CONFLICT,
    EXIT_WEIGHTS_MISSING,
    probe_compute_hardware,
    resolve_infer_device,
    diagnose_exception,
    append_error_log,
)

DEFAULT_SELECTION = ROOT / 'models/对比/m3/20261030_物理20页全链路框回归_1811/selection.json'
SETTINGS = {
    'device': 'auto',
    'precision': 'FP32',
    'imgsz': 1024,
    'conf': {'m1': 0.2, 'm2': 0.5, 'm3': 0.3},
    'iou': 0.5,
    'm3_rect': False,
}


def pair_fill(boxes):
    big = [(i, b['box']) for i, b in enumerate(boxes) if b['cls'] == 5]
    nums = [(i, b['box']) for i, b in enumerate(boxes) if b['cls'] == 6]
    if not big or not nums:
        return []
    cost = np.full((len(big), len(nums) + len(big)), 1e6)
    cost[:, len(nums):] = 100
    for i, (_, a) in enumerate(big):
        for j, (_, b) in enumerate(nums):
            nw, nh = b[2] - b[0], b[3] - b[1]
            cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
            intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
            if nw * nh <= 0 or intersection / (nw * nh) < 0.5:
                continue
            if a[0] - nw / 2 <= cx <= a[2] + nw / 2 and a[1] - nh / 2 <= cy <= a[3] + nh / 2:
                cost[i, j] = abs(cx - a[0]) / nw + abs(cy - a[1]) / nh
    rows, cols = linear_sum_assignment(cost)
    return [(big[i][0], nums[j][0], float(cost[i, j])) for i, j in zip(rows, cols) if j < len(nums) and cost[i, j] < 100]


def bounds(box, width, height):
    return [
        max(0, math.floor(box[0])),
        max(0, math.floor(box[1])),
        min(width, math.ceil(box[2])),
        min(height, math.ceil(box[3])),
    ]


def crop(image, box):
    b = bounds(box, *image.size)
    if b[2] <= b[0] or b[3] <= b[1]:
        raise ValueError('无面积裁区')
    return image.crop(b)


def translated(box, x, y):
    return [box[0] + x, box[1] + y, box[2] + x, box[3] + y]


def _emit_event(payload: dict) -> None:
    """以单行 UTF-8 JSON 输出结构化中文进度或诊断事件（工作台直接解析渲染）。"""
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _auto_tuning(device: str, requested_batch: int = 0, requested_workers: int = 0) -> tuple[int, int]:
    """根据硬件自动推荐 YOLO 批大小与 CPU 工作线程数。"""
    hw = probe_compute_hardware()
    is_gpu = device.startswith('cuda')
    if requested_batch and requested_batch > 0:
        yolo_batch = max(1, min(16, int(requested_batch)))
    else:
        if is_gpu:
            vram = float(hw.get('vram_gb') or 0.0)
            yolo_batch = 4 if vram >= 6.0 else 2
        else:
            yolo_batch = 1
    if requested_workers and requested_workers > 0:
        workers = max(1, min(12, int(requested_workers)))
    else:
        cpu_n = int(hw.get('cpu_count') or 4)
        workers = max(2, min(4, cpu_n // 2))
    return yolo_batch, workers


class Recognizer:
    """本地四阶段模型封装：方向分类 + M1/M2/M3 YOLO（支持 GPU 批推理）+ 批量 PaddleOCR + 批量涂卡 CNN。"""

    def __init__(self, device: str = 'auto'):
        import torch
        from ultralytics import YOLO
        from cnn.choice import ChoiceReader
        from cnn.printed_number import PaddleNumberReader

        eff_device, eff_label = resolve_infer_device(device)
        self.device = eff_device
        self.device_label = eff_label
        cv2.setNumThreads(1)
        torch.set_num_threads(max(2, min(4, (os.cpu_count() or 4) // 2)))
        self.models = {task: YOLO(str(current(task))) for task in ('orient', 'm1', 'm2', 'm3')}
        self.numbers = PaddleNumberReader(MODELS / 'pretrained/PP-OCRv6_medium_rec_onnx')
        # 小图涂卡 CNN 保留 CPU 批量推理，避免频繁 PCIe 拷贝
        self.choices = ChoiceReader(current('choice'), device='cpu')

    def _parse_yolo_result(self, task: str, yolo_res) -> list[dict]:
        raw = yolo_res.boxes.data.cpu().numpy()
        if not np.isfinite(raw).all():
            raise ValueError(task + '检测存在非有限值')
        mapping = {'m1': {0: 0, 1: 1, 2: 4}, 'm2': {0: 2, 1: 3}, 'm3': {0: 5, 1: 6}}[task]
        result = []
        for row in raw:
            if int(row[5]) not in mapping or row[2] <= row[0] or row[3] <= row[1]:
                raise ValueError(task + '类别或框异常')
            if min(row[2] - row[0], row[3] - row[1]) >= (16 if task == 'm1' else 6):
                result.append({
                    'cls': mapping[int(row[5])],
                    'box': row[:4].tolist(),
                    'confidence': float(row[4]),
                    'stage': task,
                })
        return result

    def infer(self, task: str, image) -> list[dict]:
        kwargs = dict(imgsz=1024, device=self.device, conf=SETTINGS['conf'][task], iou=0.5, verbose=False)
        if task == 'm3':
            kwargs['rect'] = False
        res = self.models[task](image, **kwargs)[0]
        return self._parse_yolo_result(task, res)

    def infer_batch(self, task: str, images: list) -> list[list[dict]]:
        """批量 YOLO 推理（GPU 模式下自动吃满批并行；若出现显存不足自动无缝降级逐张推理）。"""
        if not images:
            return []
        if len(images) == 1:
            return [self.infer(task, images[0])]
        kwargs = dict(imgsz=1024, device=self.device, conf=SETTINGS['conf'][task], iou=0.5, verbose=False)
        if task == 'm3':
            kwargs['rect'] = False
        try:
            results = self.models[task](images, **kwargs)
            return [self._parse_yolo_result(task, r) for r in results]
        except RuntimeError as exc:
            if 'out of memory' in str(exc).lower():
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
                return [self.infer(task, im) for im in images]
            raise

    def orient_batch(self, images: list) -> list[tuple[int, float]]:
        """批量方向分类（返回每页 (angle, confidence)）。"""
        if not images:
            return []
        try:
            preds = self.models['orient'](images, imgsz=1024, device=self.device, verbose=False)
            return [(int(r.names[int(r.probs.top1)]), float(r.probs.top1conf)) for r in preds]
        except RuntimeError as exc:
            if 'out of memory' in str(exc).lower():
                try:
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
                out = []
                for im in images:
                    r = self.models['orient'](im, imgsz=1024, device=self.device, verbose=False)[0]
                    out.append((int(r.names[int(r.probs.top1)]), float(r.probs.top1conf)))
                return out
            raise

    def finalize_page_from_detections(
        self,
        source: Path,
        index: int,
        out: Path,
        image: Image.Image,
        orientation: dict,
        m1_boxes: list[dict],
        zone_local_boxes: dict[int, tuple[str, list[int], Image.Image, list[dict]]],
        zone_errors: dict[int, str],
        started_mono: float,
    ) -> tuple[dict, list]:
        """在已知 M1/M2/M3 检测框后执行 CPU 后处理（批量 OCR + 涂卡 CNN + 裁图落盘）。"""
        from stages.choice import read_choice_zone, _crop, crop_option_row, _box_px

        pid = f'p{index:03d}'
        (out / 'images').mkdir(exist_ok=True)
        (out / 'crops').mkdir(exist_ok=True)
        image.save(out / 'images' / (pid + '.jpg'), quality=95)
        thumbnail = image.copy()
        thumbnail.thumbnail((280, 400))
        thumbnail.save(out / 'images' / (pid + '_thumb.jpg'))

        page = {
            'id': pid,
            'name': source.stem,
            'source': str(source),
            'source_sha256': digest(source.read_bytes()),
            'image': 'images/' + pid + '.jpg',
            'thumbnail': 'images/' + pid + '_thumb.jpg',
            'width': image.width,
            'height': image.height,
            'orientation': orientation,
            'boxes': list(m1_boxes),
            'items': [],
            'issues': [],
            'timings': {},
            'stage_counts': {'m1': len(m1_boxes), 'm2': 0, 'm3': 0, 'ocr': 0},
        }
        tiles = []

        def add(kind, box, number=None, issues=None, extra=None):
            ident = pid + '_' + str(len(page['items'])).zfill(3)
            im = crop(image, box)
            path = 'crops/' + ident + '.png'
            im.save(out / path)
            item = {
                'id': ident,
                'page_id': pid,
                'kind': kind,
                'box': bounds(box, *image.size),
                'crop': path,
                'number': number,
                'issues': list(issues or []),
                'status': '等待视觉转录' if kind != 'choice' else '已识别',
                **(extra or {}),
            }
            page['items'].append(item)
            if kind in ('fill', 'identity'):
                tiles.append(({'id': ident, 'kind': kind, 'page_id': pid, 'number': number, 'crop': path}, im))
            return item

        for zi, zone in enumerate(list(m1_boxes)):
            area = bounds(zone['box'], *image.size)
            x, y, right, bottom = area
            if zone['cls'] == 0:
                add('identity', area)
                continue
            if zi in zone_errors:
                page['issues'].append(zone_errors[zi])
                continue
            info = zone_local_boxes.get(zi)
            if info is None:
                continue
            task, area, local_image, local = info
            page['stage_counts'][task] += len(local)
            try:
                page['boxes'].extend({**b, 'box': translated(b['box'], x, y), 'zone': zi} for b in local)
                bgr = cv2.cvtColor(np.array(local_image), cv2.COLOR_RGB2BGR)
                norm = [
                    [
                        b['cls'],
                        (b['box'][0] + b['box'][2]) / 2 / local_image.width,
                        (b['box'][1] + b['box'][3]) / 2 / local_image.height,
                        (b['box'][2] - b['box'][0]) / local_image.width,
                        (b['box'][3] - b['box'][1]) / local_image.height,
                    ]
                    for b in local
                ]
                if task == 'm2':
                    rows = read_choice_zone(bgr, norm, self.numbers, self.choices, fix_no=True)
                    page['stage_counts']['ocr'] += len(rows)
                    paired_num, paired_opt = set(), set()
                    for row in rows:
                        ob, nb = row['opt_box'], row['num_box']
                        paired_num.add(tuple(nb))
                        paired_opt.add(tuple(ob))
                        rect = [
                            (ob[0] - ob[2] / 2) * local_image.width,
                            (ob[1] - ob[3] / 2) * local_image.height,
                            (ob[0] + ob[2] / 2) * local_image.width,
                            (ob[1] + ob[3] / 2) * local_image.height,
                        ]
                        issues = [] if row['valid'] else ['题号或涂黑结果待核对']
                        union = [
                            min(rect[0], (nb[0] - nb[2] / 2) * local_image.width),
                            min(rect[1], (nb[1] - nb[3] / 2) * local_image.height),
                            max(rect[2], (nb[0] + nb[2] / 2) * local_image.width),
                            max(rect[3], (nb[1] + nb[3] / 2) * local_image.height),
                        ]
                        item = add('choice', translated(union, x, y), row['no'], issues, {'answer': row['answer'], 'recognition': row})
                        nc = (nb[0] * local_image.width, nb[1] * local_image.height, nb[2] * local_image.width, nb[3] * local_image.height)
                        cin = crop_option_row(bgr, [], *_box_px(*ob, local_image.width, local_image.height), paired=nc)
                        cp = 'crops/' + item['id'] + '_cnn.png'
                        Image.fromarray(cv2.cvtColor(cin, cv2.COLOR_BGR2RGB)).save(out / cp)
                        item['model_input'] = cp
                    for b in norm:
                        paired = paired_num if b[0] == 2 else paired_opt
                        if tuple(b[1:]) not in paired:
                            rect = [
                                (b[1] - b[3] / 2) * local_image.width,
                                (b[2] - b[4] / 2) * local_image.height,
                                (b[1] + b[3] / 2) * local_image.width,
                                (b[2] + b[4] / 2) * local_image.height,
                            ]
                            orphan = add('unmatched', translated(rect, x, y), issues=['未配对题号框' if b[0] == 2 else '未配对选项框'])
                            orphan['status'] = '待复核'
                    if not rows:
                        page['issues'].append('选择题区未配出答案行')
                else:
                    pairs = pair_fill(local)
                    number_for_big = {bi: ni for bi, ni, _ in pairs}
                    used_numbers = {ni for _, ni, _ in pairs}
                    # 批量读取本填空区内所有配对题号，避免逐题号多次调 PaddleOCR
                    paired_bis = [bi for bi, b in enumerate(local) if b['cls'] == 5 and bi in number_for_big]
                    num_crops = []
                    for bi in paired_bis:
                        nb = local[number_for_big[bi]]['box']
                        cx, cy = (nb[0] + nb[2]) / 2 / local_image.width, (nb[1] + nb[3]) / 2 / local_image.height
                        num_crops.append(_crop(bgr, cx, cy, (nb[2] - nb[0]) / local_image.width, (nb[3] - nb[1]) / local_image.height))
                    details_by_bi = {}
                    if num_crops:
                        batch_details = self.numbers.read_details(num_crops)
                        page['stage_counts']['ocr'] += len(batch_details)
                        for bi, det in zip(paired_bis, batch_details):
                            details_by_bi[bi] = det

                    for bi, b in enumerate(local):
                        if b['cls'] != 5:
                            continue
                        issues, number, number_detail = [], None, None
                        if bi in details_by_bi:
                            number_detail = details_by_bi[bi]
                            number = number_detail['number']
                            if not number or number_detail['confidence'] < 0.3:
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
                diag = diagnose_exception(exc, default_module=f'{task.upper()} 区域后处理')
                page['issues'].append(f"{diag['module']}失败：{diag['title']}（{diag['reason'][:120]}）")
                append_error_log(out, diag['module'], exc, diag, context={'page': pid, 'zone': zi, 'task': task})

        if not page['boxes']:
            page['issues'].append('未找到可识别的答题区')
        for kind in ('fill', 'choice'):
            counts = Counter(i['number'] for i in page['items'] if i['kind'] == kind and i['number'])
            for item in page['items']:
                if item['kind'] == kind and item['number'] and counts[item['number']] > 1:
                    item['issues'].append('本页重复题号，结果分别保留')
        page['status'] = '本地识别完成，等待视觉转录'
        page['timings']['detection_seconds'] = round(time.monotonic() - started_mono, 3)
        return page, tiles

    def page(self, source: Path, index: int, out: Path, working: bool = False):
        """单页完整本地检测入口（兼容旧脚本直接调用）。"""
        from orient.deskew import detect

        start = time.monotonic()
        image = ImageOps.exif_transpose(Image.open(source)).convert('RGB')
        angle, conf = self.orient_batch([image])[0]
        orientation = {'prediction': angle, 'confidence': conf, 'working_pixels_preserved': working}
        if not working:
            image = image.rotate(-angle, expand=True, fillcolor='white')
            correction, method = detect(np.array(image.convert('L')))
            orientation.update(deskew=correction, deskew_method=method)
            if abs(correction) >= 0.1:
                image = image.rotate(correction, expand=True, resample=Image.Resampling.BICUBIC, fillcolor='white')

        m1_boxes = self.infer('m1', image)
        zone_local_boxes = {}
        zone_errors = {}
        for zi, zone in enumerate(m1_boxes):
            if zone['cls'] == 0:
                continue
            area = bounds(zone['box'], *image.size)
            task = 'm2' if zone['cls'] == 1 else 'm3'
            try:
                local_image = crop(image, area)
                local = self.infer(task, local_image)
                zone_local_boxes[zi] = (task, area, local_image, local)
            except Exception as exc:
                zone_errors[zi] = task + '区域失败：' + type(exc).__name__ + ' ' + str(exc)[:200]
        return self.finalize_page_from_detections(
            source, index, out, image, orientation, m1_boxes, zone_local_boxes, zone_errors, start
        )


def apply_responses(pages, grids, responses, provider_label: str | None = None):
    """将拼图响应增量/全量合并到对应页面条目（兼容单测旧签名与任意视觉模型提供商文案）。"""
    prefix = (provider_label or '视觉模型').strip() + '：'
    lookup = {i['id']: i for p in pages for i in p['items']}
    touched_pages = set()
    for grid, response in zip(grids, responses):
        for tile in grid['items']:
            item = lookup.get(tile['id'])
            if item is None:
                continue
            touched_pages.add(item.get('page_id'))
            item.update(grid=grid['image'], grid_id=grid['id'], grid_box=tile['grid_box'], scale=tile['scale'])
            # 清理同一来源旧 issue，兼容历史「豆包：」前缀
            item['issues'] = [
                x for x in item['issues']
                if not (x.startswith('豆包：') or x.startswith('视觉模型：') or x.startswith(prefix))
            ]
            item.pop('transcription', None)
            parsed = response.get('items', {}).get(item['id'])
            if not parsed:
                item['status'] = '调用失败' if not response.get('ok') else '结构异常'
                err_txt = response.get('errors', {}).get(item['id'], response.get('error', '缺图块输出'))
                item['issues'].append(prefix + str(err_txt))
                continue
            item['transcription'] = parsed
            item['status'] = '已识别'
            if item['kind'] == 'identity':
                if any(parsed[k] == '看不清' for k in ('name', 'sid')):
                    item['issues'].append(prefix + '身份有看不清字段')
            else:
                number = parsed['question_number']
                if not number.isdecimal() or not item['number'] or int(number) != item['number']:
                    item['issues'].append(prefix + '大题号与Paddle结果不一致或未确定')
                answers = parsed['answers']
                if any('.' in key for blanks in answers.values() for key in blanks):
                    item['issues'].append(prefix + '多级空位按原编号展示，请核对小题对应')
                if '小题号看不清' in answers:
                    item['issues'].append(prefix + '小题号看不清')
                if '无小题号' in answers and len(answers) > 1:
                    item['issues'].append(prefix + '同题同时有编号与无编号空位，请核对归属')
                if any(v == '看不清' for blanks in answers.values() for v in blanks.values()):
                    item['issues'].append(prefix + '手写有看不清空位')
            if response.get('extra_ids'):
                item['issues'].append(prefix + '本拼图含额外输出ID，已隔离未绑定')

    for page in pages:
        if touched_pages and page['id'] not in touched_pages and page.get('status') not in ('识别完成，待人工验收', '部分识别失败'):
            continue
        if page.get('error'):
            page['status'] = '本地识别失败'
        elif any(i['status'] in ('等待视觉转录', '等待豆包') for i in page['items']):
            page['status'] = '本地识别完成，等待视觉转录'
        elif any(i['status'] in ('调用失败', '结构异常') for i in page['items']):
            page['status'] = '部分识别失败'
        else:
            page['status'] = '识别完成，待人工验收'


def _preflight_check(args, out: Path) -> None:
    """启动前校验核心依赖、模型权重与 API Key，避免空跑几百页才发现配置缺失。"""
    # 1. 核心权重存在性检查
    missing_w = []
    for task in ('orient', 'm1', 'm2', 'm3', 'choice'):
        try:
            wp = current(task)
            if not wp.is_file():
                missing_w.append(f'{task} ({wp.name})')
        except Exception as exc:
            missing_w.append(f'{task} ({exc})')
    paddle_dir = MODELS / 'pretrained/PP-OCRv6_medium_rec_onnx'
    for fname in ('inference.onnx', 'inference.yml'):
        if not (paddle_dir / fname).is_file():
            missing_w.append(f'PP-OCRv6/{fname}')
    if missing_w:
        diag = {
            'module': '模型权重加载',
            'title': '缺少必要的推理模型权重文件',
            'reason': '以下权重文件不存在：' + '、'.join(missing_w),
            'fix': '请回到工作台「第 1 步：运行环境」输入密码解压加密权重，或在终端运行 python tools/下载权重.py 补齐官方与自训权重。',
            'exit_code': EXIT_WEIGHTS_MISSING,
            'raw': ', '.join(missing_w),
        }
        append_error_log(out, diag['module'], diag['reason'], diag)
        _emit_event({'event': 'error', 'diagnostic': diag})
        raise SystemExit(EXIT_WEIGHTS_MISSING)

    # 2. 核心 Python 依赖快速导入检查（特别是 paddleocr）
    for mod_name in ('torch', 'ultralytics', 'cv2', 'onnxruntime', 'paddleocr'):
        try:
            __import__(mod_name)
        except Exception as exc:
            diag = diagnose_exception(exc, default_module=f'核心依赖 ({mod_name})')
            append_error_log(out, diag['module'], exc, diag)
            _emit_event({'event': 'error', 'diagnostic': diag})
            raise SystemExit(diag.get('exit_code') or EXIT_ENV_MISSING)

    # 3. 视觉大模型 API Key 校验（非 --detect-only 时强校验）
    if not args.detect_only:
        cfg = load_vlm_config()
        if not cfg.get('api_key'):
            reason = (
                '.env 中的 API Key 仍是模板占位文字（your_...），未填入真实密钥。'
                if cfg.get('placeholder_detected')
                else f"未检测到 {cfg.get('provider_label') or '视觉大模型'} 的有效 API Key。"
            )
            diag = {
                'module': '视觉大模型 API 配置',
                'title': '未配置有效的 API Key',
                'reason': reason,
                'fix': '请点击工作台右上角「⚙ 大模型 API」选择服务商、填入真实密钥并点击「测试连通性」通过后再启动识别。',
                'exit_code': EXIT_API_CONFIG_ERROR,
                'raw': reason,
            }
            append_error_log(out, diag['module'], reason, diag)
            _emit_event({'event': 'error', 'diagnostic': diag})
            raise SystemExit(EXIT_API_CONFIG_ERROR)


def run(args):
    warnings.filterwarnings('ignore', message='.*quantize.*', category=DeprecationWarning)
    start = time.monotonic()
    out = args.out.resolve()
    if out == ROOT or out.is_relative_to(DATA.resolve()):
        raise ValueError('输出不能覆盖项目根或训练数据目录')
    out.mkdir(parents=True, exist_ok=True)

    _preflight_check(args, out)

    if args.input:
        sources = sorted(p for p in args.input.iterdir() if p.suffix.lower() in ('.jpg', '.jpeg', '.png'))
    else:
        if not args.selection.is_file():
            raise FileNotFoundError(
                f'未提供 --input 且默认清单不存在：{args.selection}\n'
                '请通过 --input 指定待识别的试卷图片目录，或通过 --selection 指定有效的清单 JSON。'
            )
        selected = json.loads(args.selection.read_text(encoding='utf-8'))['selected']
        sources = [DATA / 'm1/images' / (r['new_name'] + '.jpg') for r in selected]
    if not sources:
        raise ValueError('输入名单为空')

    eff_device, device_label = resolve_infer_device(getattr(args, 'device', 'auto'))
    yolo_batch, workers = _auto_tuning(
        eff_device,
        requested_batch=getattr(args, 'yolo_batch', 0),
        requested_workers=getattr(args, 'workers', 0),
    )
    run_settings = {**SETTINGS, 'device': eff_device, 'yolo_batch': yolo_batch, 'workers': workers}

    working = args.working_images or not args.input
    model_info = {t: {'path': str(current(t)), 'sha256': digest(current(t).read_bytes())} for t in ('orient', 'm1', 'm2', 'm3', 'choice')}
    paddle_dir = MODELS / 'pretrained/PP-OCRv6_medium_rec_onnx'
    paddle_info = {p.name: digest(p.read_bytes()) for p in sorted(paddle_dir.iterdir()) if p.is_file()}
    manifest = {
        'schema': 2,
        'sources': [{'path': str(p), 'sha256': digest(p.read_bytes()) if p.exists() else None} for p in sources],
        'models': model_info,
        'paddle': paddle_info,
        'settings': {k: run_settings[k] for k in ('precision', 'imgsz', 'conf', 'iou', 'm3_rect')},
        'working_images': working,
        'detection_code_sha256': digest(inspect.getsource(Recognizer).encode()),
        'choice_matching_sha256': digest((Path(__file__).parent / 'stages/match.py').read_bytes()),
        'grid_size': 1500,
    }
    frozen = out / 'manifest.json'
    if frozen.exists() and json.loads(frozen.read_text(encoding='utf-8')) != manifest:
        diag = diagnose_exception('当前输入、代码或模型不同于原运行；请指定新的输出目录', default_module='断点续跑校验')
        append_error_log(out, diag['module'], diag['reason'], diag)
        _emit_event({'event': 'error', 'diagnostic': diag})
        raise SystemExit(EXIT_MANIFEST_CONFLICT)
    save_json(frozen, manifest)
    (out / 'prompt.txt').write_text(PROMPT, encoding='utf-8')

    client = VlmClient(out)
    packers = {'fill': SquarePacker(out), 'identity': SquarePacker(out, prefix='i')}
    recognizer: Recognizer | None = None

    pages: list[dict] = []
    grids: list[dict] = []
    grid_by_id: dict[str, dict] = {}
    records: dict[str, dict] = {}
    futures: dict = {}
    merged_grid_ids: set[str] = set()
    counters = {
        'm1_boxes': 0,
        'm2_boxes': 0,
        'm3_boxes': 0,
        'ocr_numbers': 0,
        'choice_items': 0,
        'fill_items': 0,
        'identity_items': 0,
    }
    recent_error: dict | None = None
    last_checkpoint_ts = 0.0
    total_pages = len(sources)

    def current_stats_snapshot():
        api_done = len(records)
        api_success = sum(1 for r in records.values() if r.get('ok'))
        api_failures = sum(1 for r in records.values() if not r.get('ok'))
        page_failures = sum(1 for p in pages if p.get('error'))
        items_trans = sum(1 for p in pages for i in p.get('items', []) if 'transcription' in i)
        return {
            'pages': len(pages),
            'pages_total': total_pages,
            'page_failures': page_failures,
            'grids': len(grids),
            'api_done': api_done,
            'api_success': api_success,
            'api_failures': api_failures,
            'api_inflight': max(0, len(futures) - api_done),
            'items_with_transcription': items_trans,
        }

    def emit_progress(stage: str, message: str = '', force_error: dict | None = None):
        snap = current_stats_snapshot()
        elapsed = round(time.monotonic() - start, 1)
        done_p = snap['pages']
        ppm = round(done_p / (elapsed / 60.0), 1) if elapsed >= 1.0 and done_p > 0 else 0.0
        eta = None
        if done_p > 0 and done_p < total_pages and elapsed >= 1.0:
            eta = round((elapsed / done_p) * (total_pages - done_p), 1)
        elif done_p >= total_pages and snap['api_inflight'] > 0 and snap['api_done'] > 0:
            avg_api = elapsed / max(1, snap['api_done'])
            eta = round(avg_api * snap['api_inflight'] / max(1, args.concurrency), 1)
        _emit_event({
            'event': 'progress',
            'stage': stage,
            'message': message,
            'pages_done': done_p,
            'pages_total': total_pages,
            'page_failures': snap['page_failures'],
            'm1_boxes': counters['m1_boxes'],
            'm2_boxes': counters['m2_boxes'],
            'm3_boxes': counters['m3_boxes'],
            'ocr_numbers': counters['ocr_numbers'],
            'choice_items': counters['choice_items'],
            'fill_items': counters['fill_items'],
            'identity_items': counters['identity_items'],
            'grids_total': snap['grids'],
            'api_done': snap['api_done'],
            'api_success': snap['api_success'],
            'api_failures': snap['api_failures'],
            'api_inflight': snap['api_inflight'],
            'items_with_transcription': snap['items_with_transcription'],
            'device': eff_device,
            'device_label': device_label,
            'model': client.model,
            'provider_label': client.provider_label,
            'yolo_batch': yolo_batch,
            'workers': workers,
            'concurrency': args.concurrency,
            'elapsed_seconds': elapsed,
            'pages_per_min': ppm,
            'eta_seconds': eta,
            'recent_error': force_error or recent_error,
        })

    def checkpoint(phase: str, force: bool = False):
        nonlocal last_checkpoint_ts
        now_m = time.monotonic()
        if not force and (now_m - last_checkpoint_ts) < 0.8:
            return
        last_checkpoint_ts = now_m
        snap = current_stats_snapshot()
        save_json(out / 'results.json', {
            'pages': pages,
            'grids': grids,
            'model': client.model,
            'provider': client.provider,
            'provider_label': client.provider_label,
            'status': phase,
            'settings': run_settings,
            'stats': snap,
            'elapsed_seconds': round(now_m - start, 2),
            'note': '仅识别，无标准答案、未评分；正确性须人工核对；识别进行中会实时合并已完成图块。',
        })

    def harvest_ready_api(block_wait: bool = False) -> bool:
        """收割已完成的视觉大模型拼图响应，并立刻增量合并到 pages！"""
        nonlocal recent_error
        changed = False
        pending = [f for f in list(futures.keys()) if futures[f] not in records]
        if not pending:
            return False
        iterator = as_completed(pending) if block_wait else [f for f in pending if f.done()]
        for fut in iterator:
            gid = futures[fut]
            if gid in records:
                continue
            try:
                rec = fut.result()
            except Exception as exc:
                diag = diagnose_exception(exc, default_module=f'{client.provider_label} 视觉转录')
                append_error_log(out, diag['module'], exc, diag, context={'grid': gid})
                recent_error = diag
                rec = {'grid': gid, 'ok': False, 'error': diag['title'] + '：' + diag['reason'], 'items': {}, 'errors': {}}
            records[gid] = rec
            if not rec.get('ok'):
                diag = diagnose_exception(str(rec.get('error') or '调用失败'), default_module=f'{client.provider_label} 视觉转录')
                append_error_log(out, diag['module'], str(rec.get('error') or '调用失败'), diag, context={'grid': gid})
                recent_error = diag
            if gid not in merged_grid_ids and gid in grid_by_id:
                apply_responses(pages, [grid_by_id[gid]], [rec], provider_label=client.provider_label)
                merged_grid_ids.add(gid)
            changed = True
            if block_wait:
                emit_progress('大模型视觉转录', f"拼图 {gid} {'完成' if rec.get('ok') else '失败'}")
                checkpoint('大模型拼图转录与实时合并中', force=True)
        return changed

    def accumulate_page_counts(page: dict) -> None:
        sc = page.get('stage_counts')
        if isinstance(sc, dict):
            counters['m1_boxes'] += int(sc.get('m1', 0))
            counters['m2_boxes'] += int(sc.get('m2', 0))
            counters['m3_boxes'] += int(sc.get('m3', 0))
            counters['ocr_numbers'] += int(sc.get('ocr', 0))
        else:
            for b in page.get('boxes', []):
                st = b.get('stage')
                if st in ('m1', 'm2', 'm3'):
                    counters[f'{st}_boxes'] += 1
        for it in page.get('items', []):
            k = it.get('kind')
            if k == 'choice':
                counters['choice_items'] += 1
            elif k == 'fill':
                counters['fill_items'] += 1
            elif k == 'identity':
                counters['identity_items'] += 1

    emit_progress('准备模型与环境', f'启动识别（设备：{device_label}，YOLO 批大小 {yolo_batch}，工作线程 {workers}，API 并发 {args.concurrency}）')

    def _load_rgb(src: Path) -> Image.Image:
        return ImageOps.exif_transpose(Image.open(src)).convert('RGB')

    def _deskew_image(im: Image.Image, angle: int) -> tuple[Image.Image, dict]:
        from orient.deskew import detect
        info = {'working_pixels_preserved': working}
        if not working:
            im = im.rotate(-angle, expand=True, fillcolor='white')
            correction, method = detect(np.array(im.convert('L')))
            info.update(deskew=correction, deskew_method=method)
            if abs(correction) >= 0.1:
                im = im.rotate(correction, expand=True, resample=Image.Resampling.BICUBIC, fillcolor='white')
        return im, info

    with (
        ThreadPoolExecutor(max_workers=args.concurrency) as api_pool,
        ThreadPoolExecutor(max_workers=workers) as cpu_pool,
    ):
        def submit_grid(grid):
            if grid:
                grids.append(grid)
                grid_by_id[grid['id']] = grid
                if not args.detect_only:
                    futures[api_pool.submit(client.call, grid)] = grid['id']

        def commit_finished_page(page: dict, tiles: list):
            pages.append(page)
            accumulate_page_counts(page)
            for item, tile_im in tiles:
                submit_grid(packers[item['kind']].add(item, tile_im))
            merged = harvest_ready_api(block_wait=False)
            checkpoint('本地检测与拼图处理中', force=merged or len(pages) == total_pages)
            emit_progress(
                '本地检测与并行转录',
                f"已完成第 {len(pages)}/{total_pages} 页（{page.get('name', '')}）",
            )

        # 异步后处理队列（让 CPU OCR/涂卡 与 下一批 GPU YOLO 重叠并行）
        pending_post_futs: list = []

        def drain_post(max_keep: int = 0):
            nonlocal pending_post_futs, recent_error
            while len(pending_post_futs) > max_keep:
                idx_p, src_p, fut_p = pending_post_futs.pop(0)
                try:
                    page, tiles = fut_p.result()
                    save_json(out / 'pages' / f'p{idx_p:03d}.json', page)
                except Exception as exc:
                    diag = diagnose_exception(exc, default_module='本地检测与后处理')
                    append_error_log(out, diag['module'], exc, diag, context={'page': f'p{idx_p:03d}', 'source': str(src_p)})
                    recent_error = diag
                    page = {
                        'id': f'p{idx_p:03d}',
                        'name': src_p.stem,
                        'source': str(src_p),
                        'items': [],
                        'boxes': [],
                        'issues': [f"{diag['module']}失败：{diag['title']}（{diag['reason'][:180]}）"],
                        'error': type(exc).__name__,
                        'error_detail': diag,
                        'status': '本地识别失败',
                    }
                    tiles = []
                commit_finished_page(page, tiles)

        # 按批次窗口流式推进（支持断点缓存与未缓存页混排）
        i = 0
        # 预读下一批原图句柄
        preload_map: dict[int, any] = {}

        def schedule_preload(start_idx: int):
            end_idx = min(total_pages, start_idx + yolo_batch)
            for k in range(start_idx, end_idx):
                if k not in preload_map and not (out / 'pages' / f'p{k:03d}.json').exists():
                    preload_map[k] = cpu_pool.submit(_load_rgb, sources[k])

        schedule_preload(0)

        while i < total_pages:
            cache = out / 'pages' / f'p{i:03d}.json'
            if cache.exists():
                # 缓存页先排干前面的异步后处理以保持页序一致
                drain_post(0)
                try:
                    page = json.loads(cache.read_text(encoding='utf-8'))
                    tiles = [
                        (
                            {'id': it['id'], 'kind': it['kind'], 'page_id': page['id'], 'number': it['number'], 'crop': it['crop']},
                            Image.open(out / it['crop']).convert('RGB'),
                        )
                        for it in page.get('items', [])
                        if it.get('kind') in ('fill', 'identity') and (out / it['crop']).is_file()
                    ]
                except Exception as exc:
                    diag = diagnose_exception(exc, default_module='缓存读取')
                    append_error_log(out, diag['module'], exc, diag, context={'page': f'p{i:03d}'})
                    recent_error = diag
                    page = {
                        'id': f'p{i:03d}',
                        'name': sources[i].stem,
                        'source': str(sources[i]),
                        'items': [],
                        'boxes': [],
                        'issues': [f"{diag['module']}失败：{diag['title']}"],
                        'error': type(exc).__name__,
                        'status': '本地识别失败',
                    }
                    tiles = []
                commit_finished_page(page, tiles)
                i += 1
                schedule_preload(i)
                continue

            # 收集连续最多 yolo_batch 个未缓存页组成一个 GPU 批次窗口
            batch_indices = []
            while i < total_pages and len(batch_indices) < yolo_batch and not (out / 'pages' / f'p{i:03d}.json').exists():
                batch_indices.append(i)
                i += 1
            # 提前预读下一窗口的图片
            schedule_preload(i)

            # 懒加载模型（仅真正需要本地检测时加载）
            if recognizer is None:
                emit_progress('加载本地检测模型', f'正在将 orient / M1 / M2 / M3 加载至 {device_label}...')
                recognizer = Recognizer(device=eff_device)

            batch_start = time.monotonic()
            # 1) 取本批原图（并行预读已完成或等待）
            loaded_entries = []
            failed_entries: dict[int, tuple[BaseException, dict]] = {}
            for idx_p in batch_indices:
                fut_img = preload_map.pop(idx_p, None)
                try:
                    im = fut_img.result() if fut_img is not None else _load_rgb(sources[idx_p])
                    loaded_entries.append((idx_p, sources[idx_p], im))
                except Exception as exc:
                    diag = diagnose_exception(exc, default_module='原图读取')
                    append_error_log(out, diag['module'], exc, diag, context={'page': f'p{idx_p:03d}', 'source': str(sources[idx_p])})
                    failed_entries[idx_p] = (exc, diag)

            # 2) GPU 批量方向检测 + CPU 并发纠偏
            oriented_entries = []
            if loaded_entries:
                try:
                    orient_preds = recognizer.orient_batch([e[2] for e in loaded_entries])
                    deskew_futs = [
                        cpu_pool.submit(_deskew_image, e[2], angle)
                        for e, (angle, _) in zip(loaded_entries, orient_preds)
                    ]
                    for (idx_p, src_p, _), (angle, conf), dfut in zip(loaded_entries, orient_preds, deskew_futs):
                        try:
                            rot_im, d_info = dfut.result()
                            ori = {'prediction': angle, 'confidence': conf, **d_info}
                            oriented_entries.append((idx_p, src_p, rot_im, ori))
                        except Exception as exc:
                            diag = diagnose_exception(exc, default_module='方向与纠偏')
                            append_error_log(out, diag['module'], exc, diag, context={'page': f'p{idx_p:03d}'})
                            failed_entries[idx_p] = (exc, diag)
                except Exception as exc:
                    diag = diagnose_exception(exc, default_module='方向分类模型 (orient)')
                    for idx_p, src_p, _ in loaded_entries:
                        append_error_log(out, diag['module'], exc, diag, context={'page': f'p{idx_p:03d}'})
                        failed_entries[idx_p] = (exc, diag)

            # 3) GPU 批量 M1 版面检测 + 跨页汇总 M2/M3 裁剪批量检测
            m1_by_idx: dict[int, list[dict]] = {}
            zone_local_by_idx: dict[int, dict[int, tuple]] = {}
            zone_err_by_idx: dict[int, dict[int, str]] = {}
            if oriented_entries:
                try:
                    m1_batch_res = recognizer.infer_batch('m1', [e[2] for e in oriented_entries])
                    m2_tasks = []  # (idx_p, zi, area, crop_im)
                    m3_tasks = []
                    for (idx_p, _, rot_im, _), m1_boxes in zip(oriented_entries, m1_batch_res):
                        m1_by_idx[idx_p] = m1_boxes
                        zone_local_by_idx[idx_p] = {}
                        zone_err_by_idx[idx_p] = {}
                        for zi, zone in enumerate(m1_boxes):
                            if zone['cls'] == 0:
                                continue
                            area = bounds(zone['box'], *rot_im.size)
                            task = 'm2' if zone['cls'] == 1 else 'm3'
                            try:
                                local_im = crop(rot_im, area)
                                if task == 'm2':
                                    m2_tasks.append((idx_p, zi, area, local_im))
                                else:
                                    m3_tasks.append((idx_p, zi, area, local_im))
                            except Exception as exc:
                                zone_err_by_idx[idx_p][zi] = f'{task}裁剪失败：{exc}'

                    # 批量跑 M2 与 M3（GPU 上显著提速；单块异常自动兜底隔离）
                    for task_name, task_list in (('m2', m2_tasks), ('m3', m3_tasks)):
                        if not task_list:
                            continue
                        try:
                            # 分批避免单次超大区域数吃爆显存
                            step = max(2, yolo_batch * 2)
                            for s0 in range(0, len(task_list), step):
                                chunk = task_list[s0:s0 + step]
                                dets = recognizer.infer_batch(task_name, [c[3] for c in chunk])
                                for (idx_p, zi, area, local_im), local_boxes in zip(chunk, dets):
                                    zone_local_by_idx[idx_p][zi] = (task_name, area, local_im, local_boxes)
                        except Exception:
                            # 批次异常则逐区单独推理，精确隔离坏区域
                            for idx_p, zi, area, local_im in task_list:
                                try:
                                    local_boxes = recognizer.infer(task_name, local_im)
                                    zone_local_by_idx[idx_p][zi] = (task_name, area, local_im, local_boxes)
                                except Exception as exc:
                                    diag = diagnose_exception(exc, default_module=f'{task_name.upper()} 检测')
                                    zone_err_by_idx[idx_p][zi] = f"{diag['module']}失败：{diag['title']}"
                                    append_error_log(out, diag['module'], exc, diag, context={'page': f'p{idx_p:03d}', 'zone': zi})
                except Exception as exc:
                    diag = diagnose_exception(exc, default_module='M1 版面检测')
                    for idx_p, _, _, _ in oriented_entries:
                        append_error_log(out, diag['module'], exc, diag, context={'page': f'p{idx_p:03d}'})
                        failed_entries[idx_p] = (exc, diag)
                    oriented_entries = []

            # 4) 把本批的 OCR/涂卡/落盘后处理交给 CPU 后台池，并排干前一批后处理（双缓冲重叠）
            drain_post(max_keep=yolo_batch)
            oriented_map = {e[0]: e for e in oriented_entries}
            for idx_p in batch_indices:
                if idx_p in failed_entries:
                    drain_post(0)
                    exc, diag = failed_entries[idx_p]
                    recent_error = diag
                    page = {
                        'id': f'p{idx_p:03d}',
                        'name': sources[idx_p].stem,
                        'source': str(sources[idx_p]),
                        'items': [],
                        'boxes': [],
                        'issues': [f"{diag['module']}失败：{diag['title']}（{diag['reason'][:180]}）"],
                        'error': type(exc).__name__,
                        'error_detail': diag,
                        'status': '本地识别失败',
                    }
                    commit_finished_page(page, [])
                elif idx_p in oriented_map:
                    _, src_p, rot_im, ori = oriented_map[idx_p]
                    fut_post = cpu_pool.submit(
                        recognizer.finalize_page_from_detections,
                        src_p,
                        idx_p,
                        out,
                        rot_im,
                        ori,
                        m1_by_idx.get(idx_p, []),
                        zone_local_by_idx.get(idx_p, {}),
                        zone_err_by_idx.get(idx_p, {}),
                        batch_start,
                    )
                    pending_post_futs.append((idx_p, src_p, fut_post))

        # 排干最后一批后台后处理
        drain_post(0)

        # 收尾未满拼图并提交剩余 API 请求
        for packer in packers.values():
            submit_grid(packer.finish())
        checkpoint('大模型拼图转录与实时合并中', force=True)

        if args.detect_only:
            responses = [{'error': '尚未调用视觉模型（detect-only）', 'ok': False} for _ in grids]
            apply_responses(pages, grids, responses, provider_label=client.provider_label)
        else:
            emit_progress('大模型视觉转录', f'本地检测已全部完成，正在等待最后 {max(0, len(futures) - len(records))} 个拼图返回...')
            harvest_ready_api(block_wait=True)
            responses = [records[g['id']] for g in grids]
            # 对尚未合并过的做一次完整校验合并
            unmerged = [(g, r) for g, r in zip(grids, responses) if g['id'] not in merged_grid_ids]
            if unmerged:
                apply_responses(pages, [x[0] for x in unmerged], [x[1] for x in unmerged], provider_label=client.provider_label)

        # 结构异常单轮有界自动补修（绝不重跑已成功题块）
        if not args.detect_only:
            repair_packer = SquarePacker(out, prefix='r')
            repair_grids = []
            for page in pages:
                for item in page['items']:
                    if item['status'] == '结构异常' and item['kind'] in ('fill', 'identity'):
                        tile = {k: item[k] for k in ('id', 'kind', 'page_id', 'number', 'crop')}
                        finished = repair_packer.add(tile, Image.open(out / item['crop']).convert('RGB'))
                        if finished:
                            repair_grids.append(finished)
            finished = repair_packer.finish()
            if finished:
                repair_grids.append(finished)
            if repair_grids:
                emit_progress('结构异常自动补修', f'检测到少量结构异常题块，正在重排为 {len(repair_grids)} 个补修拼图...')
                repair_jobs = {api_pool.submit(client.call, g): g['id'] for g in repair_grids}
                repair_responses = {}
                for fut in as_completed(repair_jobs):
                    gid = repair_jobs[fut]
                    rec = fut.result()
                    repair_responses[gid] = rec
                    records[gid] = rec
                ordered = [repair_responses[g['id']] for g in repair_grids]
                apply_responses(pages, repair_grids, ordered, provider_label=client.provider_label)
                grids.extend(repair_grids)
                responses.extend(ordered)

    all_records = [json.loads(p.read_text(encoding='utf-8')) for p in (out / 'api').glob('*.json')] if (out / 'api').exists() else []
    returned_models = sorted({str(r.get('returned_model')) for r in all_records if r.get('returned_model')})
    page_failures = sum(bool(p.get('error')) for p in pages)
    api_success = sum(bool(r.get('ok')) for r in responses)
    api_failures = sum(not r.get('ok') for r in responses)
    stats = {
        'pages': len(pages),
        'page_failures': page_failures,
        'grids': len(grids),
        'api_success': api_success,
        'api_failures': api_failures,
        'reused': sum(bool(r.get('reused')) for r in responses),
        'prompt_tokens': sum(r.get('usage', {}).get('prompt_tokens', 0) for r in all_records),
        'completion_tokens': sum(r.get('usage', {}).get('completion_tokens', 0) for r in all_records),
        'attempts': sum(len(r.get('attempts', [])) for r in all_records),
        'new_api_requests': sum(not r.get('reused', False) for r in responses),
        'structural_failures': sum(i['status'] == '结构异常' for p in pages for i in p['items']),
        'items_with_transcription': sum('transcription' in i for p in pages for i in p['items']),
        'api_sum_seconds': round(sum(r.get('seconds', 0) for r in responses), 2),
        'detection_sum_seconds': round(sum(p.get('timings', {}).get('detection_seconds', 0) for p in pages), 2),
        'wall_seconds': round(time.monotonic() - start, 2),
        'concurrency': args.concurrency,
        'device': eff_device,
        'yolo_batch': yolo_batch,
        'workers': workers,
        'human_accuracy': None,
        'api_extra_ids': {g['id']: r['extra_ids'] for g, r in zip(grids, responses) if r.get('extra_ids')},
    }

    # 结果级健康判定（绝不把全部失败伪装成“待人工验收”）
    all_pages_failed = len(pages) > 0 and page_failures == len(pages)
    if all_pages_failed:
        final_status = '识别失败：全部页面本地检测失败'
    elif page_failures > 0:
        final_status = '部分页面本地失败，待补跑'
    elif api_failures > 0:
        final_status = '有未完成调用，待补跑'
    else:
        final_status = '待人工验收'

    result = {
        'pages': pages,
        'grids': grids,
        'model': client.model,
        'returned_model': returned_models[0] if len(returned_models) == 1 else (returned_models or None),
        'provider': client.provider,
        'provider_label': client.provider_label,
        'settings': run_settings,
        'stats': stats,
        'status': final_status,
        'note': '只识别，不评分。未标注的空白区域也可能有漏检，请结合整页原图核对。工作图保留原转正像素；正反面不归并。',
    }
    save_json(out / 'results.json', result)
    emit_progress('完成' if not all_pages_failed else '全部失败', f'状态：{final_status}')
    _emit_event({'event': 'summary', 'status': final_status, 'stats': stats, 'recent_error': recent_error})

    if all_pages_failed:
        exit_code = int((recent_error or {}).get('exit_code') or EXIT_ALL_PAGES_FAILED)
        if exit_code == EXIT_OK:
            exit_code = EXIT_ALL_PAGES_FAILED
        raise SystemExit(exit_code)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--input', type=Path, help='新扫描图目录；不提供则沿用固定20页')
    parser.add_argument('--selection', type=Path, default=DEFAULT_SELECTION)
    parser.add_argument('--working-images', action='store_true', help='已转正工作图，保留像素，不再次旋转')
    parser.add_argument('--detect-only', action='store_true')
    parser.add_argument('--device', default='auto', help='推理设备：auto（默认优先GPU）/ cpu / cuda:0')
    parser.add_argument('--yolo-batch', type=int, default=0, help='YOLO 批大小（0=自动按硬件选择）')
    parser.add_argument('--workers', type=int, default=0, help='CPU 读图/纠偏/后处理线程数（0=自动）')
    parser.add_argument('--concurrency', type=int, default=4, choices=range(1, 13))
    args = parser.parse_args()
    try:
        run(args)
    except SystemExit:
        raise
    except Exception as exc:
        diag = diagnose_exception(exc, default_module='自动识别主程序')
        try:
            append_error_log(args.out, diag['module'], exc, diag)
        except Exception:
            pass
        _emit_event({'event': 'error', 'diagnostic': diag})
        raise SystemExit(int(diag.get('exit_code') or EXIT_GENERAL_ERROR))


if __name__ == '__main__':
    main()
