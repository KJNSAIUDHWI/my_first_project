"""全涂黑库三路独立复查。保留人工历史，不自动接入标签或训练。"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
import json
from pathlib import Path
import queue
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cnn import choice_grid_prelabel as grid
from common.records import read_json, write_json, now, lock


def run(libraries, output):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    with lock(output/'.run.lock'):
        selection_path = output/'selection.json'
        if selection_path.exists():
            selection = read_json(selection_path)
            if [str(Path(x).resolve()) for x in libraries] != [r['base'] for r in selection['libraries']]:
                raise ValueError('library_selection_changed')
        else:
            entries = []
            for number, base in enumerate(libraries):
                directory = output/('library_%d' % number); directory.mkdir(exist_ok=True)
                rows = grid.freeze(Path(base), directory, include_reviewed=True)
                entries.append({'id':number,'base':str(Path(base).resolve()),'rows':rows})
            selection = {'created':now(),'libraries':entries,'batch_size':grid.BATCH_SIZE,'routes':list(grid.ROUTES),'mode':'three_independent_predictions_per_image; no label writes'}
            write_json(selection_path, selection)
        size = selection['batch_size']; batches = output/'batches'; batches.mkdir(exist_ok=True)
        tasks = {route:queue.Queue() for route in grid.ROUTES}; mutex = threading.Lock()
        counts = Counter(); completed = {}; failed = {}; per_batch = {}; total = sum(len(lib['rows']) for lib in selection['libraries']); start = time.monotonic()
        def status(phase='running'):
            write_json(output/'status.json',{'updated':now(),'phase':phase,'total_images':total,
                'images_with_any_result':sum(v['n'] for v in per_batch.values() if v['routes']),
                'images_with_all_three':sum(v['n'] for v in per_batch.values() if len(v['routes'])==3),
                'route_images':dict(counts),'failed_requests':len(failed),'completed_requests':len(completed),
                'elapsed_seconds':time.monotonic()-start,'complete':phase=='completed','label_writes':0})
        for lib in selection['libraries']:
            for index in range(0,len(lib['rows']),size):
                chunk=lib['rows'][index:index+size];batch_key=(lib['id'],index);per_batch[batch_key]={'n':len(chunk),'routes':set()}
                for route in grid.ROUTES:
                    path=batches/('%d_%s_%06d.json'%(lib['id'],route,index));key=(lib['id'],route,index)
                    if path.exists():
                        result=read_json(path)
                        if result.get('ok') and [r['file'] for r in result['items']]==[r['file'] for r in chunk]:
                            completed[key]=path;per_batch[batch_key]['routes'].add(route);counts[route]+=len(chunk);continue
                    tasks[route].put((lib,index,chunk,path,key))
        status()
        def worker(route):
            while True:
                try:lib,index,chunk,path,key=tasks[route].get_nowait()
                except queue.Empty:return
                result=grid.recognize(Path(lib['base']),chunk,route)
                with mutex:
                    if result['ok']:
                        write_json(path,result);completed[key]=path;per_batch[(lib['id'],index)]['routes'].add(route);counts[route]+=len(chunk)
                    else:
                        target=output/('failed_%d_%s_%06d.json'%(lib['id'],route,index));write_json(target,result);failed[key]=str(target)
                    status();print('requests=%d all3_images=%d/%d failures=%d'%(len(completed),sum(v['n'] for v in per_batch.values() if len(v['routes'])==3),total,len(failed)),flush=True)
        with ThreadPoolExecutor(max_workers=sum(c['workers'] for c in grid.ROUTES.values())) as pool:
            futures=[pool.submit(worker,route) for route,c in grid.ROUTES.items() for _ in range(c['workers'])]
            for future in futures:future.result()
        if failed:status('incomplete_resumable');return
        summary_rows=[]
        for lib in selection['libraries']:
            votes={}
            for index in range(0,len(lib['rows']),size):
                for route in grid.ROUTES:
                    for item in read_json(completed[(lib['id'],route,index)])['items']:
                        votes.setdefault(item['file'],{})[route]=item
            for row in lib['rows']:
                values=votes[row['file']];answers=[values[r]['answer'] for r in grid.ROUTES]
                unanimous=len(set(answers))==1 and answers[0] is not None
                flagged=any(v[k] for v in values.values() for k in ('uncertain','crop_issue','special'))
                ref=row['reference'];answer=answers[0] if unanimous else None
                summary_rows.append({'library':lib['id'],'base':lib['base'],'file':row['file'],'previous_answer':ref['answer'],
                     'previous_reviewed':ref['reviewed'],'previous_special':ref['special'],'votes':values,'unanimous':unanimous,
                     'consensus_answer':answer,'flagged':flagged,'needs_review':not unanimous or flagged or answer!=ref['answer']})
        write_json(output/'results.json',{'created':now(),'items':summary_rows,'label_writes':0})
        with (output/'复查结果.csv').open('w',encoding='utf-8-sig',newline='') as f:
            fields=['library','file','previous_answer','previous_reviewed','previous_special','mimo','deepseek','gemini','unanimous','consensus_answer','flagged','needs_review']
            writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
            for row in summary_rows:
                writer.writerow({**{k:row[k] for k in fields if k in row},**{r:row['votes'][r]['answer'] for r in grid.ROUTES}})
        write_json(output/'completion.json',{'created':now(),'total':len(summary_rows),'unanimous':sum(r['unanimous'] for r in summary_rows),
                 'needs_review':sum(r['needs_review'] for r in summary_rows),'label_writes':0,'reviewed_automatically':False})
        status('completed')


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--data',action='append',required=True);ap.add_argument('--output',required=True)
    args=ap.parse_args();run(args.data,args.output)
