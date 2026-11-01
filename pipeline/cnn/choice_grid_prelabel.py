"""涂黑三路编号拼图预标注：先小批重复测试，稳定后分流全量；只写待审建议。"""
import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import queue
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cnn.grid_prelabel import ROUTES
from common import row_log
from common.records import read_csv, read_json, write_json, now, lock

BATCH_SIZE = 12

PROMPT = ('Read the filled answer options in EACH independent cell. Blue numbers are cell IDs, never answers. '
          'Do not use the question number or infer patterns. A-F are option letters; multiple answers are allowed. '
          'Identify option labels from the printed letters and layout within the same row. A filled letter may be obscured; use neighboring printed letters only when its label is unambiguous. Never assume every row has the same option count. A selected option may be shaded/scribbled while its letter remains visible; compare darkness to unmarked options in the SAME cell. Do not require a solid black rectangle. Empty brackets and normal printed letters alone are not filled. '
          'Return ONLY a JSON array [{"id":1,"answer":"AC","uncertain":false,"crop_issue":false,"special":false}]. '
          'answer is sorted unique uppercase A-F, "" for clearly unmarked, null if unreadable. '
          'special=true for crossed-out/erased/changed answers, ambiguous scribbles or unresolvable marks; uncertain=true whenever unsure. '
          'crop_issue=true for clipped/missing options, multiple answer rows in one cell, or neighboring-row contamination. '
          'Never guess a clipped or erased answer. Return every ID exactly once. No explanation.')


def sheet(base, rows):
    width, height, columns = 440, 130, 4
    canvas = Image.new('RGB', (width * columns, height * ((len(rows) + columns - 1) // columns)), '#e8e8e8')
    draw = ImageDraw.Draw(canvas); font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 20)
    for i, row in enumerate(rows):
        x, y = i % columns * width, i // columns * height
        draw.rectangle((x+3, y+3, x+width-4, y+height-4), fill='white')
        draw.text((x+8, y+6), str(i+1), font=font, fill='blue')
        with Image.open(Path(base)/'images'/row['file']) as source:
            image = source.convert('RGB'); scale = min((width-20)/image.width, (height-44)/image.height)
            image = image.resize((max(1,round(image.width*scale)),max(1,round(image.height*scale))),Image.Resampling.LANCZOS)
            canvas.paste(image, (x+(width-image.width)//2, y+36+(height-44-image.height)//2))
    return canvas


def parse_reply(text, count):
    text = text.strip()
    if text.startswith('```'): text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
    try: items = json.loads(text)
    except json.JSONDecodeError:
        # Fixed-schema letter/boolean output has no free text; known relay line-wrap can split keys or answers.
        items = json.loads(text.replace('\r', '').replace('\n', ''))
    if not isinstance(items, list) or len(items) != count: raise ValueError('wrong_count')
    by = {}
    for item in items:
        if not isinstance(item, dict): raise ValueError('bad_item')
        ident, answer = item.get('id'), item.get('answer')
        if type(ident) is not int or not 1 <= ident <= count or ident in by: raise ValueError('bad_id')
        if answer is not None and (not isinstance(answer, str) or any(c not in 'ABCDEF' for c in answer) or answer != ''.join(sorted(set(answer)))):
            raise ValueError('bad_answer')
        if any(type(item.get(k)) is not bool for k in ('uncertain', 'crop_issue', 'special')): raise ValueError('missing_flags')
        by[ident] = {k: item[k] for k in ('id', 'answer', 'uncertain', 'crop_issue', 'special')}
    return [by[i] for i in range(1, count+1)]


def request(route, image):
    cfg = ROUTES[route]; key = os.environ.get(cfg['env'], '').strip()
    if not key: raise ValueError('missing_' + cfg['env'])
    buf = io.BytesIO(); image.save(buf, format='PNG'); data = base64.b64encode(buf.getvalue()).decode()
    headers = {'Content-Type': 'application/json'}
    if route == 'mimo':
        content = [{'type':'image', 'source':{'type':'base64','media_type':'image/png','data':data}}, {'type':'text','text':PROMPT}]
        headers.update({'x-api-key':key, 'anthropic-version':'2023-06-01'})
        body = {'model':cfg['model'], 'max_tokens':8192, 'thinking':{'type':'disabled'}, 'messages':[{'role':'user','content':content}]}
        url = cfg['url'].rstrip('/') + '/v1/messages'
    else:
        content = [{'type':'text','text':PROMPT}, {'type':'image_url','image_url':{'url':'data:image/png;base64,'+data}}]
        body = {'model':cfg['model'], 'max_tokens':8192, 'temperature':0, 'messages':[{'role':'user','content':content}]}
        if route == 'deepseek': body['thinking'] = {'type':'disabled'}
        headers['Authorization'] = 'Bearer ' + key; url = cfg['url']
    try:
        with urllib.request.urlopen(urllib.request.Request(url, json.dumps(body).encode(), headers), timeout=180) as response:
            result = json.loads(response.read(), strict=False)
    except urllib.error.HTTPError as error: raise RuntimeError('HTTP_%d' % error.code) from None
    if route == 'mimo':
        if result.get('stop_reason') != 'end_turn': raise ValueError('truncated')
        text = ''.join(c.get('text', '') for c in result.get('content', []) if c.get('type') == 'text')
    else:
        choice = (result.get('choices') or [{}])[0]
        if choice.get('finish_reason') != 'stop': raise ValueError('truncated')
        text = choice.get('message', {}).get('content') or ''
    return text, {**result.get('usage', {}), 'returned_model':result.get('model', '')}


def recognize(base, rows, route):
    errors = []; start = time.monotonic(); image = sheet(base, rows)
    for attempt in range(3):
        try:
            text, usage = request(route, image); parsed = parse_reply(text, len(rows))
            return {'ok':True, 'route':route, 'model':ROUTES[route]['model'], 'usage':usage, 'errors':errors,
                    'seconds':time.monotonic()-start, 'items':[{'file':r['file'], **p} for r,p in zip(rows, parsed)]}
        except Exception as error:
            # Never persist request headers, response bodies, credentials, or reasoning text.
            errors.append(str(error) if isinstance(error, (ValueError, RuntimeError)) else type(error).__name__)
            if attempt < 2: time.sleep(2**attempt)
    if len(rows) > 12:
        parts = [recognize(base, rows[:len(rows)//2], route), recognize(base, rows[len(rows)//2:], route)]
        if all(p['ok'] for p in parts):
            return {'ok':True, 'route':route, 'model':ROUTES[route]['model'], 'split_retry':True, 'parts':parts,
                    'items':[r for p in parts for r in p['items']], 'errors':errors, 'seconds':time.monotonic()-start}
    return {'ok':False, 'route':route, 'items':[], 'errors':errors, 'seconds':time.monotonic()-start}


def freeze(base, output, include_reviewed=False):
    manifest = output/'selection.json'
    if manifest.exists(): return read_json(manifest)['rows']
    rows = []
    for row in read_csv(base/'manifest.csv'):
        file = '%s_%03d.png' % (row['stem'], int(row['row']))
        state = row_log.choice_state(row_log.read(base, Path(file).stem))
        if not include_reviewed and (state['reviewed'] or state['special']): continue
        if not (base/'images'/file).is_file(): raise FileNotFoundError(file)
        rows.append({**row, 'file':file, 'reference':state})
    if not rows or len({r['file'] for r in rows}) != len(rows): raise ValueError('empty_or_duplicate')
    random.Random(202610262154).shuffle(rows)
    write_json(manifest, {'created':now(), 'base':str(base.resolve()), 'rows':rows, 'batch_size':BATCH_SIZE, 'include_reviewed':include_reviewed})
    return rows


def pilot(base, rows, output):
    # Include uncommon prediction groups for stress testing, but hide all predictions in request images.
    selected = []; buckets = {}
    for row in rows:
        state = row_log.choice_state(row_log.read(base, Path(row['file']).stem))
        buckets.setdefault(state['pred_answer'], []).append(row)
    for answer in sorted(buckets, key=lambda a:len(buckets[a])):
        selected.extend(buckets[answer][:min(3, len(buckets[answer]))])
    seen = {r['file'] for r in selected}
    selected.extend(r for r in rows if r['file'] not in seen)
    selected = selected[:48]
    write_json(output/'pilot_selection.json', {'rows':selected})
    jobs = []
    for repeat in range(2):
        order = selected.copy(); random.Random(20261026+repeat).shuffle(order)
        for index in range(0, len(order), BATCH_SIZE):
            chunk = order[index:index+BATCH_SIZE]
            sheet(base, chunk).save(output/('pilot_grid_%d_%02d.png' % (repeat,index)))
            for route in ROUTES: jobs.append((route,repeat,index,chunk))
    def one(job):
        route, repeat, index, chunk = job; path = output/('pilot_%s_%d_%02d.json' % (route, repeat, index))
        if path.exists():
            result = read_json(path)
            if result.get('ok'): return route,repeat,result
        result = recognize(base, chunk, route); write_json(path,result)
        print('pilot %s repeat%d batch%d ok=%s' % (route,repeat,index,result['ok']), flush=True)
        return route,repeat,result
    with ThreadPoolExecutor(max_workers=6) as pool: results = list(pool.map(one, jobs))
    votes = {}; failures = 0
    for route, repeat, result in results:
        failures += not result['ok']
        for item in result['items']: votes.setdefault(item['file'], []).append(item)
    missing = [r['file'] for r in selected if len(votes.get(r['file'],[]))!=6]
    unstable = [file for file,v in votes.items() if len({r['answer'] for r in v})!=1]
    high_risk = [file for file,v in votes.items() if any(r['special'] or r['crop_issue'] or r['uncertain'] or r['answer'] is None for r in v)]
    summary = {'created':now(), 'samples':len(selected), 'requests':len(results), 'failed':failures,
               'answer_disagreements':unstable, 'incomplete_votes':missing, 'flagged':high_risk,
               'passed':failures==0 and len(votes)==len(selected) and len(unstable)<=len(selected)*.1,
               'scope':'structural/repeat/cross-route agreement only; not human accuracy'}
    write_json(output/'pilot_summary.json',summary); return summary


def apply(base, output, rows, results):
    by = {item['file']:(item,result) for result in results for item in result['items']}
    if set(by) != {r['file'] for r in rows}: raise ValueError('result_manifest_mismatch')
    applied = skipped = 0
    for row in rows:
        item,result = by[row['file']]; name = Path(row['file']).stem
        events = row_log.read(base,name); state = row_log.choice_state(events)
        if state['reviewed'] or state['special']: skipped += 1; continue
        if events and events[-1].get('预标注批次') == str(output.resolve()): continue
        # Ambiguous model output does not become blank or override the existing suggestion.
        answer = state['pred_answer'] if item['answer'] is None else item['answer']
        row_log.append(base,name,{'事件':'预标注','预测答案':answer,'模型读数':item['answer'],
                      '模型':result['model'],'接口来源':result['route'], '疑难':item['uncertain'] or item['answer'] is None,
                      '裁图异常':item['crop_issue'], '特殊建议':item['special'], '预标注批次':str(output.resolve()),
                      '说明':'三路编号拼图建议，仍待人工审核；特殊建议不等于人工特殊决定'})
        applied += 1
    return applied, skipped


def run(base, output, pilot_only=False, include_reviewed=False):
    base,output = Path(base),Path(output); output.mkdir(parents=True,exist_ok=True)
    for cfg in ROUTES.values():
        if not os.environ.get(cfg['env']): raise ValueError('missing_'+cfg['env'])
    with lock(output/'.run.lock'):
        rows = freeze(base,output,include_reviewed)
        write_json(output/'status.json',{'phase':'pilot','total':len(rows),'done':0,'updated':now(),'complete':False})
        summary = pilot(base,rows,output)
        if not summary['passed'] or pilot_only:
            write_json(output/'status.json',{'phase':'pilot_passed' if summary['passed'] else 'pilot_needs_review','total':len(rows),'done':0,'updated':now(),'complete':False})
            return
        batches = output/'batches';batches.mkdir(exist_ok=True);tasks=queue.Queue();done={};failed={};mutex=threading.Lock();start=time.monotonic()
        for index in range(0,len(rows),BATCH_SIZE):
            chunk=rows[index:index+BATCH_SIZE];path=batches/('%06d.json'%index)
            if path.exists():
                saved=read_json(path)
                if saved.get('ok') and [r['file'] for r in saved['items']]==[r['file'] for r in chunk]:done[index]=saved;continue
            tasks.put((index,chunk))
        def status(phase='full',complete=False):
            write_json(output/'status.json',{'phase':phase,'total':len(rows),'done':sum(len(r['items']) for r in done.values()),'failed_batches':len(failed),'routes':dict(Counter(r['route'] for r in done.values())),'updated':now(),'seconds':time.monotonic()-start,'complete':complete})
        def worker(route):
            while True:
                try:index,chunk=tasks.get_nowait()
                except queue.Empty:return
                result=recognize(base,chunk,route)
                if not result['ok']:
                    primary=result; alternate='gemini' if route=='mimo' else 'mimo';result=recognize(base,chunk,alternate);result['primary_failure']=primary
                with mutex:
                    if result['ok']:write_json(batches/('%06d.json'%index),result);done[index]=result
                    else:write_json(output/('failed_%06d.json'%index),result);failed[index]=result
                    status();print('full %d/%d failed=%d'%(sum(len(r['items']) for r in done.values()),len(rows),len(failed)),flush=True)
        status()
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures=[pool.submit(worker,route) for route in ROUTES for _ in range(2)]
            for future in futures:future.result()
        if failed: status('failed_resumable');return
        write_json(output/'prediction_completion.json',{'completed_at':now(),'samples':len(rows),'applied':False,'reviewed_automatically':False})
        status('ready_to_apply',True)


if __name__ == '__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--data',required=True);ap.add_argument('--output',required=True);ap.add_argument('--pilot-only',action='store_true');ap.add_argument('--include-reviewed',action='store_true',help='独立复查包含已审/特殊；不改人工记录')
    args=ap.parse_args();run(args.data,args.output,args.pilot_only,args.include_reviewed)
