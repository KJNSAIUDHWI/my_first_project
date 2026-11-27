# -*- coding: utf-8 -*-
"""补跑识别批次中的失败图块（命中成功缓存绝不重复扣费；补跑前自动备份 results.json.bak）。

用法：
    python scripts/retry_failed_tiles.py --dir batches/<批次名>/recognized [--concurrency 4]
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import shutil
import sys
import time

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.vlm_client import VlmClient, SquarePacker, save_json, load_vlm_config  # noqa: E402
from engine.diagnostics import diagnose_exception, append_error_log, EXIT_API_CONFIG_ERROR  # noqa: E402
from engine.recognizer import apply_responses  # noqa: E402
from PIL import Image  # noqa: E402


def find_failed_targets(out_dir: Path) -> dict:
    """扫描 recognized/ 目录，统计需要补跑的失败图块和未转录题块。"""
    results_path = out_dir / 'results.json'
    if not results_path.is_file():
        return {'ok': False, 'error': f'未找到识别结果文件：{results_path}'}
    data = json.loads(results_path.read_text(encoding='utf-8'))
    pages = data.get('pages') or []
    grids = data.get('grids') or []
    failed_items = []
    for p in pages:
        for it in p.get('items') or []:
            if it.get('kind') in ('fill', 'identity') and (
                it.get('status') in ('调用失败', '结构异常', '等待视觉转录', '等待豆包') or 'transcription' not in it
            ):
                failed_items.append(it)
    # 找出关联了失败题块的原图块
    failed_ids = {it['id'] for it in failed_items}
    failed_grids = [g for g in grids if any(t.get('id') in failed_ids for t in g.get('items') or [])]
    return {
        'ok': True,
        'results': data,
        'failed_items': failed_items,
        'failed_grids': failed_grids,
        'failed_item_count': len(failed_items),
        'failed_grid_count': len(failed_grids),
    }


def retry_failed_grids(out_dir: Path, concurrency: int = 4, progress_cb=None) -> dict:
    """只重跑失败的图块或重排失败的单个题块，绝不重发已成功的题块。"""
    out_dir = Path(out_dir).resolve()
    scan = find_failed_targets(out_dir)
    if not scan.get('ok'):
        return scan
    data = scan['results']
    failed_items = scan['failed_items']
    if not failed_items:
        return {
            'ok': True,
            'retried_grids': 0,
            'succeeded_grids': 0,
            'remaining_failures': 0,
            'message': '当前批次没有失败或未转录的图块，无需补跑。',
        }

    cfg = load_vlm_config(ROOT)
    if not cfg.get('api_key'):
        return {
            'ok': False,
            'exit_code': EXIT_API_CONFIG_ERROR,
            'error': '未配置有效的大模型 API Key，请先在工作台右上角「⚙ 大模型 API」完成配置后再补跑。',
        }

    # 备份原 results.json
    results_path = out_dir / 'results.json'
    bak_path = out_dir / 'results.json.bak'
    shutil.copy2(results_path, bak_path)

    client = VlmClient(out_dir)
    pages = data.get('pages') or []
    grids = data.get('grids') or []

    # 仅将失败/未转录的题块按类别（fill / identity）分开重新打包，避免混排与重复付费
    stamp = time.strftime('%H%M%S')
    packers = {
        'fill': SquarePacker(out_dir, prefix=f'retry_f_{stamp}_'),
        'identity': SquarePacker(out_dir, prefix=f'retry_i_{stamp}_'),
    }
    retry_grids = []
    for it in failed_items:
        crop_rel = it.get('crop') or ''
        crop_path = out_dir / crop_rel
        if not crop_path.is_file():
            continue
        kind = it.get('kind') if it.get('kind') in packers else 'fill'
        tile = {k: it.get(k) for k in ('id', 'kind', 'page_id', 'number', 'crop')}
        try:
            with Image.open(crop_path) as im_raw:
                im_rgb = im_raw.convert('RGB')
        except OSError:
            continue
        finished = packers[kind].add(tile, im_rgb)
        if finished:
            retry_grids.append(finished)
    for pk in packers.values():
        tail = pk.finish()
        if tail:
            retry_grids.append(tail)

    if not retry_grids:
        return {
            'ok': False,
            'error': '失败题块对应的裁剪图片（crops/*.png）已丢失，无法单独补跑，请重新运行整批识别。',
        }

    if progress_cb:
        progress_cb(f'已打包 {len(failed_items)} 个失败题块为 {len(retry_grids)} 个补跑拼图（已成功题块不重复扣费），开始调用 {client.provider_label}...')

    t0 = time.monotonic()
    responses_map = {}
    with ThreadPoolExecutor(max_workers=max(1, min(8, int(concurrency)))) as pool:
        futs = {pool.submit(client.call, g, True): g['id'] for g in retry_grids}
        done_n = 0
        for fut in as_completed(futs):
            gid = futs[fut]
            done_n += 1
            try:
                rec = fut.result()
            except Exception as exc:
                diag = diagnose_exception(exc, default_module='补跑失败图块')
                append_error_log(out_dir, diag['module'], exc, diag, context={'grid': gid})
                rec = {'grid': gid, 'ok': False, 'error': diag['title'] + '：' + diag['reason'], 'items': {}, 'errors': {}}
            responses_map[gid] = rec
            if progress_cb:
                progress_cb(f"补跑进度 {done_n}/{len(retry_grids)}：拼图 {gid} {'成功' if rec.get('ok') else '仍失败（' + str(rec.get('error') or '')[:80] + '）'}")

    ordered_res = [responses_map[g['id']] for g in retry_grids]
    apply_responses(pages, retry_grids, ordered_res, provider_label=client.provider_label)
    grids.extend(retry_grids)

    # 重算统计信息与健康状态
    remaining_failed_items = sum(
        1
        for p in pages
        for it in p.get('items', [])
        if it.get('kind') in ('fill', 'identity') and it.get('status') in ('调用失败', '结构异常', '等待视觉转录', '等待豆包')
    )
    page_failures = sum(bool(p.get('error')) for p in pages)
    structural_failures = sum(it.get('status') == '结构异常' for p in pages for it in p.get('items', []))
    items_with_trans = sum('transcription' in it for p in pages for it in p.get('items', []))
    stats = dict(data.get('stats') or {})
    prev_ok = int(stats.get('api_success') or 0)
    ok_grids = sum(1 for r in ordered_res if r.get('ok'))
    retry_fail_grids = sum(1 for r in ordered_res if not r.get('ok'))
    total_api_ok = min(len(grids), prev_ok + ok_grids)
    stats.update({
        'pages': len(pages),
        'page_failures': page_failures,
        'grids': len(grids),
        'api_done': total_api_ok + retry_fail_grids,
        'api_success': total_api_ok,
        'api_failures': retry_fail_grids,
        'structural_failures': structural_failures,
        'items_with_transcription': items_with_trans,
        'last_retry_seconds': round(time.monotonic() - t0, 2),
        'last_retry_grids': len(retry_grids),
    })
    if page_failures == len(pages) and len(pages) > 0:
        status = '识别失败：全部页面本地检测失败'
    elif page_failures > 0:
        status = '部分页面本地失败，待补跑'
    elif remaining_failed_items > 0:
        status = '有未完成调用，待补跑'
    else:
        status = '待人工验收'

    data['pages'] = pages
    data['grids'] = grids
    data['stats'] = stats
    data['status'] = status
    data['model'] = client.model
    data['provider'] = client.provider
    data['provider_label'] = client.provider_label
    save_json(results_path, data)

    ok_grids = sum(1 for r in ordered_res if r.get('ok'))
    msg = (
        f'补跑完成：共重跑 {len(retry_grids)} 个拼图（成功 {ok_grids} 个），'
        f'剩余未完成题块 {remaining_failed_items} 个，当前批次状态：{status}。'
    )
    return {
        'ok': True,
        'retried_grids': len(retry_grids),
        'succeeded_grids': ok_grids,
        'remaining_failures': remaining_failed_items,
        'status': status,
        'stats': stats,
        'backup': str(bak_path),
        'message': msg,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description='仅补跑失败或结构异常的识别图块（不重复扣费）')
    ap.add_argument('--dir', type=Path, required=True, help='识别输出目录（包含 results.json）')
    ap.add_argument('--concurrency', type=int, default=4, choices=range(1, 9))
    args = ap.parse_args()
    res = retry_failed_grids(args.dir, concurrency=args.concurrency, progress_cb=lambda m: print(m, flush=True))
    print(json.dumps(res, ensure_ascii=False, indent=2), flush=True)
    return 0 if res.get('ok') else int(res.get('exit_code') or 1)


if __name__ == '__main__':
    raise SystemExit(main())
