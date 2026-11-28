# -*- coding: utf-8 -*-
"""补跑识别批次中的失败图块（命中成功缓存绝不重复扣费；JSON/结构失败自动降级单题重试）。

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
from engine.diagnostics import (  # noqa: E402
    diagnose_exception,
    append_error_log,
    is_json_or_struct_error,
    EXIT_API_CONFIG_ERROR,
)
from engine.recognizer import apply_responses  # noqa: E402
from engine.run_log import RunLogger, now_iso  # noqa: E402
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


def _backup_results(out_dir: Path, results_path: Path, keep_history: int = 5) -> Path:
    """同时更新 results.json.bak 并保留带时间戳的最近 N 份备份，防止二轮补跑覆盖首轮快照。"""
    bak_path = out_dir / 'results.json.bak'
    shutil.copy2(results_path, bak_path)
    try:
        bdir = out_dir / 'backups'
        bdir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime('%Y%m%d_%H%M%S')
        hist_path = bdir / f'results_{ts}.json'
        shutil.copy2(results_path, hist_path)
        old = sorted(bdir.glob('results_*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in old[max(1, int(keep_history)):]:
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:
        pass
    return bak_path


def _load_all_api_records(out_dir: Path) -> list[dict]:
    api_dir = out_dir / 'api'
    if not api_dir.is_dir():
        return []
    recs = []
    for p in sorted(api_dir.glob('*.json')):
        try:
            obj = json.loads(p.read_text(encoding='utf-8'))
            if isinstance(obj, dict):
                recs.append(obj)
        except (OSError, ValueError):
            continue
    return recs


def _should_single_tile(item: dict) -> bool:
    if item.get('status') == '结构异常':
        return True
    issues_txt = ' '.join(str(x) for x in (item.get('issues') or []))
    return is_json_or_struct_error(issues_txt)


def retry_failed_grids(
    out_dir: Path,
    concurrency: int = 4,
    progress_cb=None,
    *,
    max_tiles_per_grid: int | None = None,
    client_factory=None,
    actor: str = 'system',
) -> dict:
    """只重跑失败的图块或重排失败的单个题块；JSON/结构异常降级为单题重试，绝不重发已成功的题块。"""
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

    if client_factory is None:
        cfg = load_vlm_config(ROOT)
        if not cfg.get('api_key'):
            return {
                'ok': False,
                'exit_code': EXIT_API_CONFIG_ERROR,
                'error': '未配置有效的大模型 API Key，请先在工作台右上角「⚙ 大模型 API」完成配置后再补跑。',
            }

    results_path = out_dir / 'results.json'
    bak_path = _backup_results(out_dir, results_path)

    client = client_factory(out_dir) if client_factory is not None else VlmClient(out_dir)
    run_log = RunLogger(
        out_dir,
        kind='retry',
        source='retry_failed_tiles',
        actor=actor,
        meta_extra={
            'failed_items_input': len(failed_items),
            'concurrency': int(concurrency),
            'model': client.model,
            'provider': client.provider,
        },
    )
    run_log.emit('run.start', f'启动失败图块补跑，共 {len(failed_items)} 个待补跑题块')

    pages = data.get('pages') or []
    grids = data.get('grids') or []

    # 按失败原因分流：JSON/结构类异常强制 1 题 1 图单跑，网络/待转录按小拼图（默认最多 4 题）打包
    stamp = time.strftime('%H%M%S')
    small_limit = max(1, int(max_tiles_per_grid)) if max_tiles_per_grid is not None else 4
    single_limit = 1 if max_tiles_per_grid is None else max(1, min(1, int(max_tiles_per_grid)))
    packers = {
        'fill_single': SquarePacker(out_dir, prefix=f'retry_s_{stamp}_', max_items=single_limit),
        'identity_single': SquarePacker(out_dir, prefix=f'retry_is_{stamp}_', max_items=single_limit),
        'fill': SquarePacker(out_dir, prefix=f'retry_f_{stamp}_', max_items=small_limit),
        'identity': SquarePacker(out_dir, prefix=f'retry_i_{stamp}_', max_items=small_limit),
    }
    retry_grids = []
    retried_ids: set[str] = set()
    single_count = 0
    for it in failed_items:
        crop_rel = it.get('crop') or ''
        crop_path = out_dir / crop_rel
        if not crop_path.is_file():
            continue
        kind = it.get('kind') if it.get('kind') in ('fill', 'identity') else 'fill'
        use_single = _should_single_tile(it)
        pk_key = f'{kind}_single' if use_single else kind
        if use_single:
            single_count += 1
        tile = {k: it.get(k) for k in ('id', 'kind', 'page_id', 'number', 'crop')}
        try:
            with Image.open(crop_path) as im_raw:
                im_rgb = im_raw.convert('RGB')
        except OSError:
            continue
        retried_ids.add(str(it['id']))
        finished = packers[pk_key].add(tile, im_rgb)
        if finished:
            retry_grids.append(finished)
    for pk in packers.values():
        tail = pk.finish()
        if tail:
            retry_grids.append(tail)

    if not retry_grids:
        err_msg = '失败题块对应的裁剪图片（crops/*.png）已丢失，无法单独补跑，请重新运行整批识别。'
        run_log.finish('error', message=err_msg)
        return {'ok': False, 'error': err_msg}

    mode_note = f'（含 {single_count} 题按单题模式隔离重试）' if single_count else ''
    if progress_cb:
        progress_cb(
            f'已打包 {len(retried_ids)} 个失败题块为 {len(retry_grids)} 个补跑拼图{mode_note}（已成功题块不重复扣费），开始调用 {client.provider_label}...'
        )
    run_log.emit(
        'retry.packed',
        f'打包完成：{len(retried_ids)} 题 -> {len(retry_grids)} 图{mode_note}',
        detail={'retried_tiles': len(retried_ids), 'retry_grids': len(retry_grids), 'single_mode_tiles': single_count},
    )

    t0 = time.monotonic()
    responses_map = {}
    grid_by_id = {g['id']: g for g in retry_grids}
    with ThreadPoolExecutor(max_workers=max(1, min(8, int(concurrency)))) as pool:
        futs = {pool.submit(client.call, g, True, run_log.run_id): g['id'] for g in retry_grids}
        done_n = 0
        for fut in as_completed(futs):
            gid = futs[fut]
            g_obj = grid_by_id[gid]
            done_n += 1
            try:
                rec = fut.result()
            except Exception as exc:
                diag = diagnose_exception(exc, default_module=f'{client.provider_label} 补跑转录')
                append_error_log(
                    out_dir,
                    diag['module'],
                    exc,
                    diag,
                    context={'grid': gid, 'run_id': run_log.run_id, 'tiles': [t['id'] for t in g_obj.get('items', [])]},
                )
                rec = {'grid': gid, 'ok': False, 'error': diag['title'] + '：' + diag['reason'], 'items': {}, 'errors': {}}
            if not rec.get('ok'):
                err_raw = str(rec.get('error') or '补跑调用失败')
                diag = diagnose_exception(err_raw, default_module=f'{client.provider_label} 补跑转录')
                append_error_log(
                    out_dir,
                    diag['module'],
                    err_raw,
                    diag,
                    context={'grid': gid, 'run_id': run_log.run_id, 'tiles': [t['id'] for t in g_obj.get('items', [])]},
                )
                run_log.emit(
                    'api.retry_fail',
                    f'补跑拼图 {gid} 失败：{err_raw[:160]}',
                    level='ERROR',
                    status='fail',
                    grid_id=gid,
                    duration_ms=round(float(rec.get('seconds') or 0) * 1000, 1),
                    error_code=str(rec.get('error_code') or ''),
                    detail={'error': err_raw[:240], 'tile_count': len(g_obj.get('items', []))},
                )
            else:
                tile_errs = rec.get('errors') or {}
                dup_w = rec.get('dup_warnings') or {}
                run_log.emit(
                    'api.retry_ok',
                    f'补跑拼图 {gid} 成功（{len(rec.get("items") or {})} 题）',
                    level='WARN' if (tile_errs or dup_w) else 'INFO',
                    status='ok',
                    grid_id=gid,
                    duration_ms=round(float(rec.get('seconds') or 0) * 1000, 1),
                    detail={'tile_errors': tile_errs, 'dup_warning_tiles': list(dup_w.keys())},
                )
                if tile_errs:
                    for tid, terr in tile_errs.items():
                        diag = diagnose_exception(terr, default_module=f'{client.provider_label} 补跑校验')
                        append_error_log(out_dir, diag['module'], terr, diag, context={'grid': gid, 'item': tid, 'run_id': run_log.run_id})
            responses_map[gid] = rec
            if progress_cb:
                progress_cb(
                    f"补跑进度 {done_n}/{len(retry_grids)}：拼图 {gid} "
                    f"{'成功' if rec.get('ok') else '仍失败（' + str(rec.get('error') or '')[:80] + '）'}"
                )

    ordered_res = [responses_map[g['id']] for g in retry_grids]
    apply_responses(pages, retry_grids, ordered_res, provider_label=client.provider_label)

    # 标记已被本轮补跑完全取代的历史失败拼图，保持统计口径严谨一致
    all_api_recs = _load_all_api_records(out_dir)
    latest_ok_by_grid: dict[str, bool] = {}
    for rec in all_api_recs:
        gid_r = str(rec.get('grid') or '')
        if gid_r:
            latest_ok_by_grid[gid_r] = bool(rec.get('ok'))
    for gid_r, rec in responses_map.items():
        latest_ok_by_grid[gid_r] = bool(rec.get('ok'))

    for g in grids:
        if g.get('superseded'):
            continue
        g_tile_ids = {str(t.get('id') or '') for t in (g.get('items') or []) if t.get('id')}
        if not g_tile_ids:
            continue
        prev_ok = latest_ok_by_grid.get(str(g.get('id') or ''))
        # 若该历史拼图所有题块均已进入本轮补跑，或该历史拼图本身调用失败且有题块被补跑取代，则标记 superseded
        if g_tile_ids.issubset(retried_ids) or (prev_ok is False and bool(g_tile_ids & retried_ids)):
            g['superseded'] = True
            g['superseded_by'] = run_log.run_id

    for g in retry_grids:
        g['run_id'] = run_log.run_id
    grids.extend(retry_grids)

    # 基于有效拼图重算 api_done / api_success / api_failures，严格保证 api_done == api_success + api_failures
    active_grids = [g for g in grids if not g.get('superseded')]
    superseded_grids = len(grids) - len(active_grids)
    active_ok = 0
    active_fail = 0
    for g in active_grids:
        gid_g = str(g.get('id') or '')
        if latest_ok_by_grid.get(gid_g, True):
            active_ok += 1
        else:
            active_fail += 1

    # 汇总全量 api/*.json 的真实计费 Token（包含首轮 + 全部补跑）
    prompt_tokens = sum(int((r.get('usage') or {}).get('prompt_tokens') or 0) for r in all_api_recs)
    completion_tokens = sum(int((r.get('usage') or {}).get('completion_tokens') or 0) for r in all_api_recs)
    cached_tokens = sum(
        int(((r.get('usage') or {}).get('prompt_tokens_details') or {}).get('cached_tokens') or 0)
        for r in all_api_recs
    )
    total_attempts = sum(len(r.get('attempts') or []) for r in all_api_recs)

    # 若单测用 mock transport 未写 api/*.json 以外的历史 token，兜底不回退
    stats = dict(data.get('stats') or {})
    if all_api_recs:
        stats['prompt_tokens'] = prompt_tokens
        stats['completion_tokens'] = completion_tokens
        stats['cached_tokens'] = cached_tokens
        stats['attempts'] = total_attempts
    else:
        stats['prompt_tokens'] = int(stats.get('prompt_tokens') or 0) + sum(
            int((r.get('usage') or {}).get('prompt_tokens') or 0) for r in ordered_res
        )
        stats['completion_tokens'] = int(stats.get('completion_tokens') or 0) + sum(
            int((r.get('usage') or {}).get('completion_tokens') or 0) for r in ordered_res
        )

    remaining_failed_items = sum(
        1
        for p in pages
        for it in p.get('items', [])
        if it.get('kind') in ('fill', 'identity') and it.get('status') in ('调用失败', '结构异常', '等待视觉转录', '等待豆包')
    )
    page_failures = sum(bool(p.get('error')) for p in pages)
    structural_failures = sum(it.get('status') == '结构异常' for p in pages for it in p.get('items', []))
    items_with_trans = sum('transcription' in it for p in pages for it in p.get('items', []))
    ok_grids_this_round = sum(1 for r in ordered_res if r.get('ok'))

    stats.update({
        'pages': len(pages),
        'page_failures': page_failures,
        'grids': len(active_grids),
        'grids_total_history': len(grids),
        'superseded_grids': superseded_grids,
        'api_done': active_ok + active_fail,
        'api_success': active_ok,
        'api_failures': active_fail,
        'structural_failures': structural_failures,
        'items_with_transcription': items_with_trans,
        'last_retry_seconds': round(time.monotonic() - t0, 2),
        'last_retry_grids': len(retry_grids),
        'last_retry_at': now_iso(),
        'last_retry_run_id': run_log.run_id,
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
    data['last_run_id'] = run_log.run_id
    data['updated_at'] = now_iso()
    save_json(results_path, data)

    msg = (
        f'补跑完成：共重跑 {len(retry_grids)} 个拼图（成功 {ok_grids_this_round} 个），'
        f'剩余未完成题块 {remaining_failed_items} 个，当前批次状态：{status}。'
    )
    run_log.finish('ok' if remaining_failed_items == 0 else 'warn', stats=stats, message=msg)
    return {
        'ok': True,
        'run_id': run_log.run_id,
        'retried_grids': len(retry_grids),
        'succeeded_grids': ok_grids_this_round,
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
    ap.add_argument('--max-tiles-per-grid', type=int, default=None, help='每个补跑拼图最多容纳题块数（结构异常自动降级为 1）')
    args = ap.parse_args()
    res = retry_failed_grids(
        args.dir,
        concurrency=args.concurrency,
        max_tiles_per_grid=args.max_tiles_per_grid,
        progress_cb=lambda m: print(m, flush=True),
    )
    print(json.dumps(res, ensure_ascii=False, indent=2), flush=True)
    return 0 if res.get('ok') else int(res.get('exit_code') or 1)


if __name__ == '__main__':
    raise SystemExit(main())
