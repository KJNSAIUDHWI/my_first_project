"""只读复核人工数字标签；先测试48/64格，再全量分流、疑点交叉复核及特殊上下文复核。"""
import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import random
import sys
import threading
import time
import urllib.request

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cnn import grid_prelabel, mimo_digit
from common import row_log, events
from common.cnn_data import digit_image
from common.records import read_csv, write_json, now, lock

ROOT = Path(__file__).resolve().parents[2]
PROMPT = ('Independently read each printed question number. Blue IDs identify cells, not answers. '
          'Ignore brackets and adjacent answer strokes. Never infer a sequence or copy nearby cells. '
          'Return ONLY JSON: [[id,label,uncertain,crop_issue],...], every ID exactly once. '
          'label: integer 1..60, 0 for definitely nonnumeric, null if unreadable. '
          'uncertain and crop_issue must be JSON booleans. Do not guess clipped digits.')


def request(route, images, prompt):
    cfg = grid_prelabel.ROUTES[route]
    key = os.environ[cfg['env']]
    content = [{'type': 'text', 'text': prompt}]
    for title, image in images:
        content.append({'type': 'text', 'text': title})
        buf = io.BytesIO(); image.save(buf, format='PNG')
        data = base64.b64encode(buf.getvalue()).decode()
        if route == 'mimo':
            content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': data}})
        else:
            content.append({'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + data}})
    body = {'model': cfg['model'], 'max_tokens': 12288, 'messages': [{'role': 'user', 'content': content}]}
    headers = {'Content-Type': 'application/json'}
    if route == 'mimo':
        body['thinking'] = {'type': 'disabled'}
        headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
        url = cfg['url'].rstrip('/') + '/v1/messages'
    else:
        body['temperature'] = 0
        headers['Authorization'] = 'Bearer ' + key
        url = cfg['url']
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=180) as response:
        result = json.loads(response.read(), strict=False)
    if route == 'mimo':
        if result.get('stop_reason') != 'end_turn': raise ValueError('truncated')
        text = ''.join(c.get('text', '') for c in result.get('content', []) if c['type'] == 'text')
    else:
        choice = result['choices'][0]
        if choice.get('finish_reason') != 'stop': raise ValueError('truncated')
        text = choice['message']['content']
    text = text.strip()
    if text.startswith('```'): text = text.split('\n', 1)[1].rsplit('```', 1)[0]
    try: parsed = json.loads(text)
    except json.JSONDecodeError:
        # 已知Gemini中转会在固定列宽插入未转义换行；仅数值数组允许此修复。
        if route != 'gemini' or prompt != PROMPT: raise
        parsed = json.loads(text.replace('\n', '').replace('\r', ''))
    return parsed, result.get('usage', {})


def recognize(base, rows, route):
    image = mimo_digit.sheet(base, rows)
    errors = []; start = time.monotonic()
    for attempt in range(3):
        try:
            parsed, usage = request(route, [('Numbered grid', image)], PROMPT)
            objects = [{'id': r[0], 'label': r[1], 'uncertain': r[2], 'crop_issue': r[3]}
                       for r in parsed if isinstance(r, list) and len(r) == 4]
            checked = mimo_digit.parse_reply(json.dumps(objects), len(rows))
            return {'ok': True, 'route': route, 'seconds': time.monotonic()-start, 'errors': errors,
                    'usage': usage, 'items': [{'file': r['file'], **p} for r, p in zip(rows, checked)]}
        except Exception as exc:
            errors.append(type(exc).__name__)
            if attempt < 2: time.sleep(2 ** attempt)
    return {'ok': False, 'route': route, 'errors': errors, 'items': [], 'seconds': time.monotonic()-start}


def suspicious(item, human):
    return item['label'] != human['label'] or item['uncertain'] or item['crop_issue']


def run(base, output):
    output.mkdir(parents=True, exist_ok=True)
    for cfg in grid_prelabel.ROUTES.values():
        if not os.environ.get(cfg['env']): raise ValueError('Missing environment: '+cfg['env'])
    manifest = output/'selection.json'
    if manifest.exists():
        frozen = json.loads(manifest.read_text(encoding='utf-8')); rows = frozen['rows']
    else:
        rows = []
        for row in read_csv(base/'labels.csv'):
            state = row_log.digit_state(row_log.read(base, Path(row['file']).stem))
            if not state['reviewed']: raise ValueError('Still unreviewed: '+row['file'])
            rows.append({**row, 'human': {k: state[k] for k in ('label', 'special', 'note', 'reviewed_at')}})
        frozen = {'created': now(), 'base': str(base.resolve()), 'rows': rows}
        write_json(manifest, frozen)
    human = {r['file']: r['human'] for r in rows}
    normal = [r for r in rows if not r['human']['special']]
    random.Random(202610261900).shuffle(normal)
    begin = time.monotonic(); mutex = threading.Lock()
    def status(phase, **kwargs):
        write_json(output/'status.json', {'created': now(), 'phase': phase, 'total': len(rows),
                   'elapsed_seconds': time.monotonic()-begin, **kwargs})
    def saved_call(path, chunk, route, pilot=False):
        if path.exists():
            old = json.loads(path.read_text(encoding='utf-8'))
            if pilot and not old['ok']: return old
            if old['ok'] and [i['file'] for i in old['items']] == [r['file'] for r in chunk]: return old
            if not old['ok'] and path.parent.name == 'primary':
                route = 'gemini' if old['route'] != 'gemini' else 'deepseek'
        result = recognize(base, chunk, route)
        if not pilot and not result['ok']:
            failure = result
            parts = [recognize(base, chunk[i:i+24], route) for i in range(0, len(chunk), 24)]
            if all(p['ok'] for p in parts):
                result = {'ok': True, 'route': route, 'items': [i for p in parts for i in p['items']],
                          'fallback_parts': parts, 'primary_failure': failure}
            elif path.parent.name == 'primary':
                alternate = 'gemini' if route != 'gemini' else 'deepseek'
                result = recognize(base, chunk, alternate)
                result['primary_failure'] = failure
        write_json(path, result); return result
    pilot = normal[:96]
    status('testing_48_64')
    tests = {}; jobs = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for size in (48, 64):
            for route in grid_prelabel.ROUTES:
                for i in range(0, len(pilot), size):
                    path = output/'pilot'/('%s_%d_%d.json' % (route, size, i))
                    jobs.append((size, route, pool.submit(saved_call, path, pilot[i:i+size], route, True)))
        for size, route, future in jobs:
            tests.setdefault((size, route), []).append(future.result())
    report = {}
    for route in grid_prelabel.ROUTES:
        maps = {s: {i['file']: i['label'] for r in tests[s, route] for i in r['items']} for s in (48, 64)}
        report[route] = {'sizes': {str(s): {'requests': len(tests[s, route]),
                       'success': sum(r['ok'] for r in tests[s, route]),
                       'retry_requests': sum(bool(r['errors']) for r in tests[s, route]),
                       'request_seconds': sum(r['seconds'] for r in tests[s, route]),
                       'human_disagreements': sum(i['label'] != human[i['file']]['label'] for r in tests[s, route] for i in r['items'])}
                       for s in (48, 64)},
                       'size_label_differences': sum(maps[48].get(r['file']) != maps[64].get(r['file']) for r in pilot)}
    complete_48 = all(r['ok'] for route in grid_prelabel.ROUTES for r in tests[48, route])
    complete = all(r['ok'] for rs in tests.values() for r in rs)
    stable = complete and all(v['size_label_differences'] <= 1 for v in report.values())
    size = 64 if stable else 48
    write_json(output/'pilot_summary.json', {'routes': report, 'complete': complete, 'passed_48': complete_48,
               'stable_64': stable, 'chosen_size': size, 'ready_for_full': stable or complete_48})
    if not (stable or complete_48):
        status('blocked_pilot_failure'); return
    tasks = queue.Queue(); results = {}; failures = {}
    for i in range(0, len(normal), size): tasks.put((i, normal[i:i+size]))
    def worker(route):
        while True:
            try: i, chunk = tasks.get_nowait()
            except queue.Empty: return
            result = saved_call(output/'primary'/('%06d.json'%i), chunk, route)
            with mutex:
                if result['ok']: results[i] = result
                else: failures[i] = result
                status('primary', done=sum(len(r['items']) for r in results.values()), failed_batches=len(failures), grid_size=size)
                print('Primary', sum(len(r['items']) for r in results.values()), '/', len(normal), flush=True)
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(worker, route) for route, cfg in grid_prelabel.ROUTES.items() for _ in range(cfg['workers'])]
        for future in futures: future.result()
    if failures:
        status('blocked_primary_failure', failed_batches=len(failures)); return
    suspects = []; by_route = {r: [] for r in grid_prelabel.ROUTES}; votes = {}
    by_file = {r['file']: r for r in normal}
    for batch in results.values():
        for item in batch['items']:
            votes[item['file']] = {batch['route']: item}
            if suspicious(item, human[item['file']]):
                suspects.append(item['file'])
                for route in by_route:
                    if route != batch['route']: by_route[route].append(by_file[item['file']])
    status('crosscheck', primary_done=len(normal), suspects=len(suspects))
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = []
        for route, selected in by_route.items():
            for i in range(0, len(selected), size):
                jobs.append(pool.submit(saved_call, output/'crosscheck'/('%s_%06d.json'%(route, i)), selected[i:i+size], route))
        for future in as_completed(jobs):
            result = future.result()
            if not result['ok']: failures[len(failures)] = result
            for item in result['items']: votes[item['file']][result['route']] = item
    write_json(output/'suspects.json', [{'file': f, 'human': human[f], 'votes': votes[f]} for f in suspects])
    status('special_context', suspects=len(suspects))
    specials = [r for r in rows if r['human']['special']]
    crops = {r['文件名']: r for r in read_csv(ROOT/'data/m2/crop清单.csv')}
    special_results = []
    for row in specials:
        stem = row['stem']; crop = ROOT/'data/m2/images'/(stem+'.jpg')
        meta = crops.get(stem+'.jpg', {})
        parent = meta.get('源图') or stem.split('.选择题区')[0]
        page = ROOT/'data/m1/images'/(parent+'.jpg')
        state = events.load_state('m1', parent)
        context = {'file': row['file'], 'human_note': row['human']['note'], 'parent': parent,
                   'm1_state': state, 'crop_metadata': meta, 'images': [str(digit_image(base, row)), str(crop), str(page)]}
        # 小图位置由M2归一化框给出，避免整区中误认另一题号。
        context['digit_box_in_region'] = json.loads(row['box'])
        write_json(output/'special_context'/(Path(row['file']).stem+'.json'), context)
        images = []
        for title, path in zip(('Digit crop', 'M1 cropped region', 'Original full page'), (digit_image(base, row), crop, page)):
            with Image.open(path) as im:
                image = im.convert('RGB'); image.thumbnail((2600, 2600)); images.append((title, image))
        prompt = ('Inspect this human-marked special case using three images: digit crop, M1 region, original full page. '
                  'The target digit box in region is normalized center x,y,width,height: '+row['box']+'. '
                  'Human special note: '+row['human']['note']+'. '
                  'M1 class is '+meta.get('区域', '选择题区')+'; M1 special status: '+str(state.get('special', False))+'. '
                  'Return ONLY JSON object with label (0..60 or null), crop_problem (string), '
                  'm1_problem (string), recommendation (string). Explain in Chinese. '
                  'Distinguish actual visible digits from context inference. Recommend retaining special if ambiguous. '
                  'For synthetic augmentation requests, assess feasibility only; do not manufacture new truth.')
        for route in grid_prelabel.ROUTES:
            target = output/'special_votes'/(Path(row['file']).stem+'_'+route+'.json')
            if target.exists():
                result = json.loads(target.read_text(encoding='utf-8'))
                if result.get('ok'): special_results.append(result); continue
            result = {'file': row['file'], 'route': route, 'ok': False}
            for attempt in range(3):
                try:
                    parsed, usage = request(route, images, prompt)
                    if not isinstance(parsed, dict) or not all(k in parsed for k in ('label','crop_problem','m1_problem','recommendation')): raise ValueError('special_schema')
                    if parsed['label'] is not None and (type(parsed['label']) != int or not 0 <= parsed['label'] <= 60): raise ValueError('special_label')
                    result.update(ok=True, result=parsed, usage=usage); break
                except Exception as exc: result['error'] = type(exc).__name__
            write_json(target, result); special_results.append(result)
    summary = {'completed_at': now(), 'complete': not failures and all(r['ok'] for r in special_results),
               'normal': len(normal), 'special': len(specials), 'suspects': len(suspects),
               'crosscheck_failed_batches': len(failures), 'special_success': sum(r['ok'] for r in special_results),
               'special_requests': len(special_results), 'grid_size': size,
               'primary_routes': dict(Counter(b['route'] for b in results.values())),
               'seconds': time.monotonic()-begin, 'human_labels_modified': False}
    write_json(output/'completion.json', summary); status('complete' if summary['complete'] else 'incomplete', **summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data', required=True); ap.add_argument('--output', required=True)
    args = ap.parse_args()
    with lock(Path(args.output).parent/('.'+Path(args.output).name+'.lock')):
        run(Path(args.data), Path(args.output))
