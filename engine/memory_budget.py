# -*- coding: utf-8 -*-
"""改卷模式内存暂存 Crop 预检压测与资源分配器。

在启动真实改卷识别前自动执行约 0.2~0.5 秒的微压测：
1. 读取本机真实物理内存（总内存 / 可用内存 / 当前进程 RSS）；
2. 抽样当前批次 1~2 张真实试卷原图，实测单页裁切 + 1500x1500 拼图内存占用、PNG 编码体积与显式释放后的回收效果；
3. 根据可用内存与批次规模，自动给出合理的内存并发窗口（max_inflight_grids / yolo_batch / vlm_workers），
   并在可用内存吃紧时自动安全回退到传统硬盘缓存，绝不 OOM 崩溃。
"""
from __future__ import annotations

import gc
import io
import os
import time
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageOps


def _read_system_memory_mb() -> tuple[float, float, float]:
    """返回 (total_mb, available_mb, process_rss_mb)。"""
    try:
        import psutil
        vm = psutil.virtual_memory()
        rss = psutil.Process(os.getpid()).memory_info().rss
        return (
            round(vm.total / (1024 * 1024), 1),
            round(vm.available / (1024 * 1024), 1),
            round(rss / (1024 * 1024), 1),
        )
    except Exception:
        pass
    # Windows stdlib 兜底
    if os.name == 'nt':
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ('dwLength', ctypes.c_ulong),
                    ('dwMemoryLoad', ctypes.c_ulong),
                    ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return (
                    round(stat.ullTotalPhys / (1024 * 1024), 1),
                    round(stat.ullAvailPhys / (1024 * 1024), 1),
                    0.0,
                )
        except Exception:
            pass
    return 8192.0, 4096.0, 0.0


def run_memory_crop_preflight(
    image_paths: Sequence[Path | str],
    *,
    grid_size: int = 1500,
    requested_concurrency: int = 4,
    requested_yolo_batch: int = 0,
    min_free_mb: float = 1200.0,
) -> dict:
    """运行内存暂存 Crop 预检与微压测，返回推荐分配方案。"""
    t0 = time.perf_counter()
    paths = [Path(p) for p in image_paths if Path(p).is_file()]
    page_count = len(paths)
    total_mb, avail_mb, rss_before_mb = _read_system_memory_mb()

    # 抽样首张与中段最多 2 张真实原图进行微压测
    sample_paths: list[Path] = []
    if paths:
        sample_paths.append(paths[0])
        if len(paths) > 2:
            sample_paths.append(paths[len(paths) // 2])

    sample_dims: list[tuple[int, int]] = []
    sample_page_raw_mbs: list[float] = []
    sample_crop_set_mbs: list[float] = []
    sample_grid_png_mbs: list[float] = []

    for sp in sample_paths:
        im = None
        canvas = None
        crops: list[Image.Image] = []
        try:
            im = ImageOps.exif_transpose(Image.open(sp)).convert('RGB')
            w, h = im.size
            sample_dims.append((w, h))
            raw_mb = (w * h * 3) / (1024 * 1024)
            sample_page_raw_mbs.append(raw_mb)

            # 模拟真实单页裁切（约 4 个大题/姓名框，覆盖约 35% 页面面积）
            tile_h = max(32, h // 5)
            tile_w = max(32, int(w * 0.8))
            crop_bytes = 0
            for k in range(4):
                y1 = min(h - tile_h, k * tile_h)
                c = im.crop((0, y1, min(w, tile_w), min(h, y1 + tile_h)))
                crops.append(c)
                crop_bytes += c.width * c.height * 3
            sample_crop_set_mbs.append(crop_bytes / (1024 * 1024))

            # 模拟拼入 1500x1500 内存画布并编码 PNG 字节流
            canvas = Image.new('RGB', (grid_size, grid_size), 'white')
            y_cur = 12
            for c in crops[:2]:
                sc = min(1.0, (grid_size - 24) / max(1, c.width), (grid_size // 2 - 24) / max(1, c.height))
                rz = c.resize((max(1, int(c.width * sc)), max(1, int(c.height * sc))), Image.Resampling.BILINEAR)
                canvas.paste(rz, (12, y_cur))
                y_cur += rz.height + 16
                rz.close()
            buf = io.BytesIO()
            canvas.save(buf, format='PNG', compress_level=3)
            sample_grid_png_mbs.append(len(buf.getvalue()) / (1024 * 1024))
            buf.close()
        except Exception:
            continue
        finally:
            for c in crops:
                try:
                    c.close()
                except Exception:
                    pass
            if canvas is not None:
                try:
                    canvas.close()
                except Exception:
                    pass
            if im is not None:
                try:
                    im.close()
                except Exception:
                    pass

    gc.collect()
    _, avail_after_mb, rss_after_mb = _read_system_memory_mb()

    avg_page_raw_mb = (sum(sample_page_raw_mbs) / len(sample_page_raw_mbs)) if sample_page_raw_mbs else 18.0
    avg_crop_mb = (sum(sample_crop_set_mbs) / len(sample_crop_set_mbs)) if sample_crop_set_mbs else 6.5
    avg_grid_png_mb = (sum(sample_grid_png_mbs) / len(sample_grid_png_mbs)) if sample_grid_png_mbs else 1.4
    canvas_raw_mb = (grid_size * grid_size * 3) / (1024 * 1024)  # ~6.44 MB for 1500x1500

    # 估算本批次采用内存暂存后，相比落盘免写的零碎文件与磁盘写入量
    # 每页约 25 选择 crop + 25 CNN crop + 4 填空/姓名 crop + ~0.6 张拼图/JSON
    est_grids_total = max(1, int(round(page_count * 0.75))) if page_count else 0
    est_saved_files = page_count * 54 + est_grids_total * 2
    est_saved_disk_mb = round(page_count * (avg_crop_mb * 0.35 + 0.5) + est_grids_total * avg_grid_png_mb, 1)

    # 基于可用物理内存计算安全预算（不超过当前可用内存的 28% 且封顶 1536 MB）
    eff_avail = min(avail_mb, avail_after_mb) if avail_after_mb > 0 else avail_mb
    safe_budget_mb = round(min(eff_avail * 0.28, 1536.0), 1)
    conc = max(1, min(16, int(requested_concurrency or 4)))
    yolo_b = max(1, min(8, int(requested_yolo_batch or (4 if eff_avail >= 4096 else 2))))

    # 在途有界窗口峰值 = (yolo_b + 2) 页原图/裁切缓冲 + 2 个活跃打包画布 + max_inflight_grids 张已编码拼图字节流
    # 因为每张 tile 拼入画布后即刻 close，每张拼图 API 返回后即刻释放，峰值与总页数无关！
    per_inflight_grid_mb = avg_grid_png_mb * 2.4 + 2.0  # PNG 字节 + base64 临时串开销
    base_pipeline_mb = (yolo_b + 2) * (avg_page_raw_mb + avg_crop_mb) + 2.0 * canvas_raw_mb
    remaining_for_grids = max(0.0, safe_budget_mb - base_pipeline_mb)
    max_grids_by_ram = int(remaining_for_grids // max(1.0, per_inflight_grid_mb))
    target_inflight = max(conc + 1, min(conc * 2 + 2, 16))
    max_inflight_grids = max(2, min(target_inflight, max_grids_by_ram)) if max_grids_by_ram >= 2 else 2

    est_peak_mb = round(base_pipeline_mb + max_inflight_grids * per_inflight_grid_mb, 1)

    if eff_avail < min_free_mb:
        mode = 'disk_fallback'
        reason = f'当前可用物理内存仅 {eff_avail:.0f} MB（低于安全阈值 {min_free_mb:.0f} MB），已自动切回硬盘缓存防内存溢出'
        rec_yolo_batch = 1
        rec_concurrency = min(conc, 2)
    elif est_peak_mb > safe_budget_mb:
        # 先自动降并发窗口再尝试放进内存
        rec_yolo_batch = 1
        rec_concurrency = min(conc, 2)
        max_inflight_grids = 2
        est_peak_mb = round((rec_yolo_batch + 1) * (avg_page_raw_mb + avg_crop_mb) + 2.0 * canvas_raw_mb + 2 * per_inflight_grid_mb, 1)
        if est_peak_mb <= safe_budget_mb:
            mode = 'memory'
            reason = f'内存偏紧，已自动收敛窗口至并发 {rec_concurrency} / 在途拼图 {max_inflight_grids} 张以安全运行内存暂存'
        else:
            mode = 'disk_fallback'
            reason = f'预估峰值 {est_peak_mb:.0f} MB 超过安全预算 {safe_budget_mb:.0f} MB，已自动降级为硬盘缓存'
    else:
        mode = 'memory'
        rec_yolo_batch = yolo_b
        rec_concurrency = conc
        reason = (
            f'内存充裕（可用 {eff_avail / 1024:.1f} GB），启用内存暂存：预计稳态峰值仅 ~{est_peak_mb:.0f} MB，'
            f'识别完成立即释放，预计免写约 {est_saved_files} 个零碎文件（省 ~{est_saved_disk_mb:.1f} MB 磁盘 IO）'
        )

    return {
        'ok': True,
        'recommended_mode': mode,
        'use_memory_crops': mode == 'memory',
        'reason': reason,
        'page_count': page_count,
        'sampled_pages': len(sample_dims),
        'sample_dimensions': [f'{w}x{h}' for w, h in sample_dims],
        'total_ram_mb': total_mb,
        'available_ram_mb': eff_avail,
        'process_rss_before_mb': rss_before_mb,
        'process_rss_after_mb': rss_after_mb,
        'safe_budget_mb': safe_budget_mb,
        'avg_page_raw_mb': round(avg_page_raw_mb, 2),
        'avg_page_crop_mb': round(avg_crop_mb, 2),
        'avg_grid_png_mb': round(avg_grid_png_mb, 2),
        'est_peak_mb': est_peak_mb,
        'est_saved_files': est_saved_files,
        'est_saved_disk_mb': est_saved_disk_mb,
        'max_inflight_grids': max_inflight_grids,
        'recommended_yolo_batch': rec_yolo_batch,
        'recommended_concurrency': rec_concurrency,
        'benchmark_ms': round((time.perf_counter() - t0) * 1000, 1),
    }
