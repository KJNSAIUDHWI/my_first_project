# -*- coding: utf-8 -*-
"""本机推理 Batch 与并发性能压测工具（零 API 费用，自动推荐最优设备/批大小/线程数）。

用法：
    python scripts/benchmark_local.py [--samples 4] [--input <样卷目录>]
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import tempfile
import time

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw  # noqa: E402
from engine.diagnostics import probe_compute_hardware, diagnose_exception  # noqa: E402
from engine.model_registry import current  # noqa: E402
from engine.paths import DATA, MODELS  # noqa: E402


def _collect_sample_images(root: Path, custom_dir: Path | None, count: int = 4) -> tuple[list[Path], str, tempfile.TemporaryDirectory | None]:
    """寻找真实样卷用于测速；若本机尚无试卷则生成合成标准尺寸样卷。"""
    exts = {'.jpg', '.jpeg', '.png'}
    candidates: list[tuple[Path, str]] = []
    if custom_dir and custom_dir.is_dir():
        candidates.append((custom_dir, f'指定目录 {custom_dir.name}'))
    # 尝试读取活跃批次
    active_file = root / 'batches' / 'active.json'
    if active_file.is_file():
        try:
            bid = json.loads(active_file.read_text(encoding='utf-8')).get('batch_id')
            if bid:
                b_imgs = root / 'batches' / bid / 'images'
                if b_imgs.is_dir():
                    candidates.append((b_imgs, f'当前批次 {bid}'))
        except Exception:
            pass
    # 其次找任意批次
    batches_dir = root / 'batches'
    if batches_dir.is_dir():
        for p in sorted(batches_dir.iterdir(), reverse=True):
            im_dir = p / 'images'
            if im_dir.is_dir():
                candidates.append((im_dir, f'历史批次 {p.name}'))
                break
    # 再其次用训练集样图
    m1_dir = root / 'data' / 'm1' / 'images'
    if m1_dir.is_dir():
        candidates.append((m1_dir, '本地 M1 样卷'))

    for folder, label in candidates:
        imgs = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts)[:count]
        if len(imgs) >= 2:
            # 不够 count 张时循环补齐，保证 batch 对比公平
            while len(imgs) < count:
                imgs.append(imgs[len(imgs) % len(imgs)])
            return imgs, label, None

    # 兜底：生成标准扫描分辨率的合成答题卡样图（纯测试吞吐用，不落入项目目录）
    td = tempfile.TemporaryDirectory(prefix='autograde_bench_')
    tpath = Path(td.name)
    out = []
    for i in range(count):
        im = Image.new('RGB', (1654, 2339), (248, 248, 246))
        dr = ImageDraw.Draw(im)
        dr.rectangle([80, 120, 1574, 340], outline=(30, 30, 30), width=3)
        dr.rectangle([80, 380, 1574, 920], outline=(30, 30, 30), width=3)
        dr.rectangle([80, 960, 1574, 2200], outline=(30, 30, 30), width=3)
        for k in range(12):
            y = 1040 + k * 90
            dr.line([(160, y), (1480, y)], fill=(120, 120, 120), width=2)
        p = tpath / f'synthetic_sample_{i + 1:02d}.jpg'
        im.save(p, quality=92)
        out.append(p)
    return out, '合成标准尺寸样卷 (1654×2339)', td


def run_benchmark(
    root: Path = ROOT,
    custom_dir: Path | None = None,
    sample_count: int = 4,
    progress_cb=None,
) -> dict:
    """运行多组 (device, yolo_batch, cpu_workers) 组合测速，返回对比表与最优推荐配置。"""
    hw = probe_compute_hardware(force=True)
    if progress_cb:
        progress_cb(f"硬件检测完成：{hw['recommended_label']}（CPU {hw['cpu_count']} 线程）")

    # 检查关键权重与依赖
    try:
        import torch
        from ultralytics import YOLO
    except Exception as exc:
        diag = diagnose_exception(exc, default_module='性能压测')
        return {'ok': False, 'error': f"{diag['title']}：{diag['reason']}", 'diagnostic': diag}

    missing_w = []
    for task in ('orient', 'm1', 'm2', 'm3'):
        try:
            wp = current(task)
            if not wp.is_file():
                missing_w.append(task)
        except Exception:
            missing_w.append(task)
    if missing_w:
        return {'ok': False, 'error': f"缺少模型权重（{', '.join(missing_w)}），请先在「运行环境」解压权重后再测速。"}

    samples, sample_source, temp_dir = _collect_sample_images(root, custom_dir, count=max(2, min(8, int(sample_count))))
    try:
        cpu_n = int(hw.get('cpu_count') or 4)
        # 构造要测试的候选组合（控制在 25~50 秒内完成，兼顾准确与等待体验）
        configs: list[dict] = []
        if hw['cuda']:
            configs.append({'label': 'CPU 基准 (Batch 1, 2线程)', 'device': 'cpu', 'yolo_batch': 1, 'cpu_workers': 2})
            configs.append({'label': 'GPU 单张 (Batch 1, 2线程)', 'device': 'cuda:0', 'yolo_batch': 1, 'cpu_workers': 2})
            configs.append({'label': 'GPU 小批 (Batch 2, 2线程)', 'device': 'cuda:0', 'yolo_batch': 2, 'cpu_workers': 2})
            configs.append({'label': 'GPU 中批 (Batch 4, 4线程)', 'device': 'cuda:0', 'yolo_batch': 4, 'cpu_workers': min(4, cpu_n)})
            if float(hw.get('vram_gb') or 0) >= 6.0 and len(samples) >= 4:
                configs.append({'label': 'GPU 大批 (Batch 6, 4线程)', 'device': 'cuda:0', 'yolo_batch': 6, 'cpu_workers': min(4, cpu_n)})
        else:
            configs.append({'label': 'CPU 单线程 (Batch 1, 1线程)', 'device': 'cpu', 'yolo_batch': 1, 'cpu_workers': 1})
            configs.append({'label': 'CPU 双线程 (Batch 1, 2线程)', 'device': 'cpu', 'yolo_batch': 1, 'cpu_workers': 2})
            if cpu_n >= 4:
                configs.append({'label': 'CPU 四线程 (Batch 1, 4线程)', 'device': 'cpu', 'yolo_batch': 1, 'cpu_workers': 4})

        # 预加载 YOLO 模型一次，避免把磁盘加载模型算成推理时间
        if progress_cb:
            progress_cb(f'使用样卷：{sample_source}（共 {len(samples)} 张），正在加载模型预热...')
        m_orient = YOLO(str(current('orient')))
        m_m1 = YOLO(str(current('m1')))
        m_m2 = YOLO(str(current('m2')))
        m_m3 = YOLO(str(current('m3')))

        # 预热 CUDA（避免首轮编译耗时污染第 1 组 GPU 成绩）
        if hw['cuda']:
            warm_im = Image.open(samples[0]).convert('RGB')
            _ = m_orient(warm_im, imgsz=1024, device='cuda:0', verbose=False)
            _ = m_m1(warm_im, imgsz=1024, device='cuda:0', verbose=False)
            torch.cuda.synchronize()

        def load_rgb(p: Path):
            return Image.open(p).convert('RGB')

        rows = []
        for idx, cfg in enumerate(configs, 1):
            dev = cfg['device']
            bs = cfg['yolo_batch']
            wk = cfg['cpu_workers']
            if progress_cb:
                progress_cb(f"正在测速 ({idx}/{len(configs)})：{cfg['label']}...")
            peak_vram_mb = 0.0
            try:
                if hw['cuda'] and dev.startswith('cuda'):
                    torch.cuda.reset_peak_memory_stats()
                t0 = time.perf_counter()
                with ThreadPoolExecutor(max_workers=wk) as pool:
                    # 模拟真实流式读图 + 批量 orient + 批量 M1 + 区域 M2/M3 推理
                    for s0 in range(0, len(samples), bs):
                        chunk_paths = samples[s0:s0 + bs]
                        imgs = list(pool.map(load_rgb, chunk_paths))
                        _ = m_orient(imgs, imgsz=1024, device=dev, verbose=False)
                        m1_res = m_m1(imgs, imgsz=1024, device=dev, conf=0.2, iou=0.5, verbose=False)
                        m2_crops, m3_crops = [], []
                        for im, r in zip(imgs, m1_res):
                            raw = r.boxes.data.cpu().numpy()
                            for box_row in raw:
                                cls_id = int(box_row[5])
                                x1, y1, x2, y2 = [int(max(0, v)) for v in box_row[:4]]
                                x2, y2 = min(im.width, x2), min(im.height, y2)
                                if x2 - x1 < 16 or y2 - y1 < 16:
                                    continue
                                if cls_id == 1:
                                    m2_crops.append(im.crop((x1, y1, x2, y2)))
                                elif cls_id == 2:
                                    m3_crops.append(im.crop((x1, y1, x2, y2)))
                        if m2_crops:
                            for c0 in range(0, len(m2_crops), max(2, bs * 2)):
                                _ = m_m2(m2_crops[c0:c0 + max(2, bs * 2)], imgsz=1024, device=dev, conf=0.5, iou=0.5, verbose=False)
                        if m3_crops:
                            for c0 in range(0, len(m3_crops), max(2, bs * 2)):
                                _ = m_m3(m3_crops[c0:c0 + max(2, bs * 2)], imgsz=1024, device=dev, conf=0.3, iou=0.5, rect=False, verbose=False)
                if hw['cuda'] and dev.startswith('cuda'):
                    torch.cuda.synchronize()
                    peak_vram_mb = round(torch.cuda.max_memory_allocated() / (1024 * 1024), 1)
                elapsed = time.perf_counter() - t0
                sec_per_page = round(elapsed / len(samples), 3)
                ppm = round(len(samples) / (elapsed / 60.0), 1)
                rows.append({
                    **cfg,
                    'ok': True,
                    'total_seconds': round(elapsed, 2),
                    'sec_per_page': sec_per_page,
                    'pages_per_min': ppm,
                    'peak_vram_mb': peak_vram_mb,
                    'error': '',
                })
            except Exception as exc:
                if hw['cuda']:
                    try:
                        torch.cuda.empty_cache()
                    except Exception:
                        pass
                diag = diagnose_exception(exc, default_module='本机性能压测')
                rows.append({
                    **cfg,
                    'ok': False,
                    'total_seconds': 0.0,
                    'sec_per_page': 9999.0,
                    'pages_per_min': 0.0,
                    'peak_vram_mb': peak_vram_mb,
                    'error': diag['title'],
                })

        ok_rows = [r for r in rows if r['ok']]
        if not ok_rows:
            return {'ok': False, 'error': '所有测速组合均失败，请先检查环境自检结果。', 'rows': rows, 'hardware': hw}

        best = min(ok_rows, key=lambda r: r['sec_per_page'])
        cpu_base = next((r for r in ok_rows if r['device'] == 'cpu'), None)
        speedup = round(cpu_base['sec_per_page'] / best['sec_per_page'], 2) if (cpu_base and best['sec_per_page'] > 0) else 1.0
        # API 并发推荐：GPU 快时提高到 6，普通 4，纯慢速 CPU 3
        rec_api = 6 if (best['device'].startswith('cuda') and best['pages_per_min'] >= 30) else (4 if best['pages_per_min'] >= 15 else 3)
        recommended = {
            'infer_device': 'auto' if best['device'].startswith('cuda') else 'cpu',
            'resolved_device': best['device'],
            'yolo_batch': best['yolo_batch'],
            'cpu_workers': best['cpu_workers'],
            'api_concurrency': rec_api,
            'sec_per_page': best['sec_per_page'],
            'pages_per_min': best['pages_per_min'],
            'speedup_vs_cpu': speedup,
            'label': best['label'],
            'tested_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        summary = (
            f"最优组合：{best['label']}（本地检测约 {best['sec_per_page']} 秒/页，"
            f"约 {best['pages_per_min']} 页/分钟"
            + (f"，较 CPU 提速 {speedup}x" if speedup > 1.05 else "")
            + f"；建议 API 并发 {rec_api}）。"
        )
        if progress_cb:
            progress_cb(summary)
        return {
            'ok': True,
            'hardware': hw,
            'sample_source': sample_source,
            'sample_count': len(samples),
            'rows': rows,
            'recommended': recommended,
            'summary': summary,
        }
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()


def main() -> int:
    ap = argparse.ArgumentParser(description='本机自动改卷 Batch/并发性能压测（零 API 费用）')
    ap.add_argument('--input', type=Path, default=None, help='可选：指定样卷目录')
    ap.add_argument('--samples', type=int, default=4, help='抽测页数（默认 4）')
    args = ap.parse_args()
    res = run_benchmark(ROOT, custom_dir=args.input, sample_count=args.samples, progress_cb=lambda m: print(m, flush=True))
    print(json.dumps(res, ensure_ascii=False, indent=2), flush=True)
    return 0 if res.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
