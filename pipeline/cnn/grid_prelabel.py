"""三路数字拼图预标注：稳定性测试、共用任务队列、逐批留档与续跑。"""
import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
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
import urllib.error

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cnn import mimo_digit
from common import row_log
from common.records import read_csv,read_json,write_json,append,now,lock

ROUTES={
 'mimo':{'model':'mimo-v2.6-flash','env':'MIMO_API_KEY','url':'https://api.xiaomimimo.com/anthropic','workers':2},
 'deepseek':{'model':'deepseek-v4.1-flash','env':'HAPPY_DEEPSEEK_KEY','url':'https://api.happy-pet.fun/v1/chat/completions','workers':4},
 'gemini':{'model':'gemini-3.6-flash','env':'HAPPY_GEMINI_KEY','url':'https://api.happy-pet.fun/v1/chat/completions','workers':4}}

PROMPT=('Read the printed question number in each cell of this grid. Blue numbers are cell IDs, not question numbers. '
 'Ignore punctuation, adjacent brackets, option letters and black answer marks. Never infer a numerical sequence. '
 'Return ONLY a JSON array, one object per cell, with exactly these fields: '
 '{"id":1,"label":12,"uncertain":false,"crop_issue":true}. '
 'label is an integer 1..60 for a readable number, 0 for definitely non-numeric, null for ambiguous or unreadable. '
 'uncertain=true if not clearly readable. crop_issue=true for clipped digits or contamination from neighboring characters. '
 'Return every cell ID exactly once. No explanation.')


def request_grid(route,image):
    config=ROUTES[route];key=os.environ.get(config['env'],'').strip()
    if not key:raise ValueError('缺少环境变量：'+config['env'])
    if route=='mimo':return mimo_digit.predict(image,config['model'],key,config['url'])
    buf=io.BytesIO();image.save(buf,format='PNG')
    body={'model':config['model'],'max_tokens':8192 if route=='deepseek' else 4096,'temperature':0,'messages':[{'role':'user','content':[
        {'type':'text','text':PROMPT}, {'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()}}]}]}
    req=urllib.request.Request(config['url'],json.dumps(body).encode(),
        {'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=180) as response:
            raw=response.read()
        try:result=json.loads(raw)
        except json.JSONDecodeError:
            # 部分中转响应未转义content中的换行；恢复外层后仍严格检查每格结果。
            result=json.loads(raw,strict=False)
    except urllib.error.HTTPError as error:
        raise RuntimeError('HTTP_%d'%error.code) from None
    choices=result.get('choices') or []
    if not choices or choices[0].get('finish_reason')!='stop':raise ValueError('输出为空或截断')
    text=choices[0].get('message',{}).get('content') or ''
    if not text.strip():raise ValueError('空输出')
    # 不读取或留存reasoning_content。
    unwrapped=route=='gemini' and ('\n' in text or '\r' in text)
    if unwrapped:
        # 此中转会在固定字符宽度处插换行，甚至拆开JSON键；题号结构无文本值。
        text=text.replace('\r','').replace('\n','')
    return text,{**result.get('usage',{}),'returned_model':result.get('model',''),'transport_line_unwrap':unwrapped}


def recognize(base, rows, route, attempts=4):
    image=mimo_digit.sheet(base,rows)
    errors=[];start=time.monotonic()
    for attempt in range(attempts):
        try:
            text,usage=request_grid(route,image)
            parsed=mimo_digit.parse_reply(text,len(rows))
            items=[{'file':r['file'],**{k:v for k,v in p.items() if k!='id'}} for r,p in zip(rows,parsed)]
            return {'route':route,'model':ROUTES[route]['model'],'items':items,'usage':usage,
                    'seconds':time.monotonic()-start,'errors':errors,'ok':True}
        except Exception as error:
            # 只记错误种类和预设HTTP状态，绝不记录请求、密钥或推理内容。
            message=str(error) if isinstance(error,(ValueError,RuntimeError)) else type(error).__name__
            errors.append(message[:160])
            if attempt+1<attempts:time.sleep(2**(attempt+1))
    return {'route':route,'items':[],'files':[r['file'] for r in rows],
            'seconds':time.monotonic()-start,'errors':errors,'ok':False}


def stability(base, selection, reference, output, repeats=3):
    base,output=Path(base),Path(output)
    if output.exists():raise FileExistsError(output)
    output.mkdir(parents=True)
    by={r['file']:r for r in read_csv(base/'labels.csv')}
    selected=[by[r['file']] for r in read_csv(selection)]
    expected={r['file']:r['label'] for r in read_json(reference)['items']}
    if any(r['file'] not in expected for r in selected):raise ValueError('缺少测试参照')
    jobs=[]
    for route in ROUTES:
        if not os.environ.get(ROUTES[route]['env']):raise ValueError('缺少'+ROUTES[route]['env'])
        for repeat in range(repeats):
            rows=selected.copy();random.Random(20261026+repeat).shuffle(rows)
            for i in range(0,len(rows),24):jobs.append((route,repeat,i,rows[i:i+24]))
    jobs.sort(key=lambda job:(job[1],job[2],job[0]))
    begin=time.monotonic();results=[];counts=Counter();mutex=threading.Lock()
    semaphores={route:threading.Semaphore(c['workers']) for route,c in ROUTES.items()}
    def one(job):
        route,repeat,index,rows=job
        with semaphores[route]:
            started=time.monotonic()-begin
            r=recognize(base,rows,route)
            r.update(started_seconds=started,ended_seconds=time.monotonic()-begin)
        r.update(repeat=repeat,index=index)
        if r['ok']:
            r['differences']=[dict(item,expected=expected[item['file']]) for item in r['items'] if item['label']!=expected[item['file']]]
        write_json(output/('%s_%d_%04d.json'%(route,repeat,index)),r)
        with mutex:
            counts[route]+=1;print('稳定性 %s %d/%d'%(route,counts[route],repeats*((len(selected)+23)//24)),flush=True)
        return r
    with ThreadPoolExecutor(max_workers=sum(c['workers'] for c in ROUTES.values())) as pool:
        for r in pool.map(one,jobs):results.append(r)
    summary={'created':now(),'wall_seconds':time.monotonic()-begin,'samples_per_repeat':len(selected),'repeats':repeats,'routes':{}}
    for route in ROUTES:
        records=[r for r in results if r['route']==route]
        summary['routes'][route]={'requests':len(records),'success':sum(r['ok'] for r in records),
          'first_attempt_success':sum(r['ok'] and not r['errors'] for r in records),
          'failed':sum(not r['ok'] for r in records),'differences':sum(len(r.get('differences',[])) for r in records),
          'wrong_nonnull':sum(sum(d['label'] is not None for d in r.get('differences',[])) for r in records),
          'uncertain_null':sum(sum(d['label'] is None for d in r.get('differences',[])) for r in records),
          'sum_request_seconds':sum(r['seconds'] for r in records),'cells':sum(len(r['items']) for r in records),
          'workers':ROUTES[route]['workers'],
          'observed_cells_per_second':sum(len(r['items']) for r in records)/max(.001,max(r['ended_seconds'] for r in records)-min(r['started_seconds'] for r in records))}
    summary['passed']=all(r['failed']==0 and r['wrong_nonnull']==0 and r['uncertain_null']<=.05*r['cells'] for r in summary['routes'].values())
    write_json(output/'summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False),flush=True)
    return summary


def full(base, output):
    base,output=Path(base),Path(output)
    output.mkdir(parents=True,exist_ok=True)
    results_dir=output/'batches';results_dir.mkdir(exist_ok=True)
    rows=read_csv(base/'labels.csv');files=[r['file'] for r in rows]
    if len(set(files))!=len(files):raise ValueError('数据清单重复')
    manifest=output/'selection.json'
    if manifest.exists():
        if read_json(manifest)['files']!=files:raise ValueError('续跑清单已变化')
    else:write_json(manifest,{'base':str(base.resolve()),'files':files,'created':now(),'batch_size':24})
    for config in ROUTES.values():
        if not os.environ.get(config['env']):raise ValueError('缺少环境变量：'+config['env'])
    tasks=queue.Queue();done={};failed={};counts=Counter();mutex=threading.Lock();start=time.monotonic()
    for i in range(0,len(rows),24):
        chunk=rows[i:i+24];saved=results_dir/('%06d.json'%i)
        if saved.exists():
            r=read_json(saved)
            if [item['file'] for item in r['items']]!=[row['file'] for row in chunk]:raise ValueError('已存批次清单不符')
            done[i]=r
        else:tasks.put((i,chunk))
    def status(complete=False):
        write_json(output/'status.json',{'created':now(),'total':len(rows),'done':sum(len(r['items']) for r in done.values()),
          'batch_done':len(done),'failed_batches':len(failed),'routes':dict(counts),
          'elapsed_seconds':time.monotonic()-start,'complete':complete})
    route_limits={route:threading.Semaphore(c['workers']) for route,c in ROUTES.items()}
    def call(route,chunk):
        with route_limits[route]:return recognize(base,chunk,route)
    def worker(route):
        while True:
            try:index,chunk=tasks.get_nowait()
            except queue.Empty:return
            result=call(route,chunk)
            if not result['ok']:
                # 只给失败批次换一条独立路线兜底；保留首轮错误证据。
                primary=result
                alternate='mimo' if route!='mimo' else 'gemini'
                result=call(alternate,chunk)
                result['primary_failure']=primary
            with mutex:
                if result['ok']:
                    write_json(results_dir/('%06d.json'%index),result);done[index]=result;counts[result['route']]+=len(chunk)
                else:
                    write_json(output/('failed_%06d.json'%index),result);failed[index]=result
                status();print('全量预标注 %d/%d，失败批次%d'%(sum(len(r['items']) for r in done.values()),len(rows),len(failed)),flush=True)
            tasks.task_done()
    status()
    with ThreadPoolExecutor(max_workers=sum(c['workers'] for c in ROUTES.values())) as pool:
        futures=[pool.submit(worker,route) for route,c in ROUTES.items() for _ in range(c['workers'])]
        for future in futures:future.result()
    if failed:raise RuntimeError('仍有失败批次，已保存现场，可续跑；未写正式审核')
    items=[item for index in sorted(done) for item in done[index]['items']]
    if [r['file'] for r in items]!=files:raise ValueError('合并清单缺失或重复')
    # 最后接入未审建议。用户已审的样本不覆盖，其余保持未审。
    skipped=[]
    for index in sorted(done):
        result=done[index]
        for item in result['items']:
            name=Path(item['file']).stem;state=row_log.digit_state(row_log.read(base,name))
            if state['reviewed']:skipped.append(item['file']);continue
            # 续跑时不重复追加已接入的同一条建议。
            events=row_log.read(base,name)
            if events and events[-1].get('预标注批次')==str(output.resolve()):continue
            label=state['label'] if item['label'] is None else item['label']
            row_log.append(base,name,{'事件':'预标注','标签':label,'模型':result['model'],'接口来源':result['route'],
              '疑难':item['uncertain'] or item['label'] is None,'裁图异常':state['crop_issue'] or item['crop_issue'],
              '模型读数':item['label'],'预标注批次':str(output.resolve()),'说明':'三路编号拼图预标注；待人工审核，绝不自动确认'})
    write_json(output/'completion.json',{'completed_at':now(),'samples':len(items),'skipped_reviewed':skipped,
       'routes':dict(Counter(r['route'] for r in done.values())),
       'uncertain':sum(r['uncertain'] or r['label'] is None for r in items),
       'crop_issue':sum(r['crop_issue'] for r in items),'reviewed_automatically':False})
    status(True)
    print('全量预标注完成，仍待人工审核。',flush=True)


def main():
    ap=argparse.ArgumentParser(description=__doc__);sub=ap.add_subparsers(dest='command',required=True)
    test=sub.add_parser('test');test.add_argument('--data',required=True);test.add_argument('--selection',required=True)
    test.add_argument('--reference',required=True);test.add_argument('--output',required=True);test.add_argument('--repeats',type=int,default=3)
    run=sub.add_parser('full');run.add_argument('--data',required=True);run.add_argument('--output',required=True)
    args=ap.parse_args()
    if args.command=='test':stability(args.data,args.selection,args.reference,args.output,args.repeats)
    else:
        with lock(Path(args.output).parent/('.'+Path(args.output).name+'.lock')):full(args.data,args.output)


if __name__=='__main__':main()
