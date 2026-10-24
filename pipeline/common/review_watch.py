"""Watch persisted human review; durable milestones and single continuation."""
import os
from pathlib import Path
import subprocess
import time
from .paths import ROOT, DATA
from .records import read_csv, read_json, write_json, append, now, lock
from . import events
from .cnn_data import digit_reviewed
from . import row_log


def open_edge(url):
    for folder in (os.environ.get('PROGRAMFILES(X86)', ''), os.environ.get('PROGRAMFILES', '')):
        exe = Path(folder) / 'Microsoft/Edge/Application/msedge.exe'
        if exe.is_file():
            subprocess.Popen([str(exe), url], creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            return {'requested': True, 'url': url, 'visible_verified': False}
    return {'requested': False, 'url': url, 'error': '未找到Edge'}


def status(task, subset):
    rows = read_csv(subset)
    if not rows:
        raise ValueError('审核名单为空')
    done, pending, special = [], [], []
    if task in ('m1','m2','m3','m4'):
        for row in rows:
            name = row['new_name']
            st = events.load_state(task, name)
            if st['special']:
                special.append(name)
            elif st['reviewed']:
                done.append(name)
            else:
                pending.append(name)
    elif task == 'choice':
        base = DATA / 'choice'
        for row in rows:
            file = '%s_%03d.png' % (row['stem'], int(row['row']))
            st = row_log.choice_state(row_log.read(base, file[:-4]))
            if st['special']:
                special.append(file)
            elif st['reviewed']:
                done.append(file)
            else:
                pending.append(file)
    else:
        for row in rows:
            base = DATA / row.get('dataset','digit')
            if digit_reviewed(base,row): done.append(row['file'])
            else: pending.append(row['file'])
    return {'time':now(),'total':len(rows),'done':len(done),'pending':pending,'special':special,
            'percent':round(100*len(done)/len(rows),1), 'ready':not pending and not special}


def watch(batch_dir, interval=10, once=False):
    batch_dir = Path(batch_dir)
    with lock(batch_dir/'.watch.lock'):
        while True:
            config = read_json(batch_dir/'batch.json')
            task = config.get('review_task',config['task'])
            subset = config['review_subset']
            result = status(task, subset)
            state = read_json(batch_dir/'watch.json', {'milestones':[]})
            for value in (25,50,75,100):
                if result['percent'] >= value and value not in state['milestones']:
                    append(batch_dir/'events.jsonl',{'e':'review_progress','percent':value,'task':task})
                    state['milestones'].append(value)
            write_json(batch_dir/'watch.json',{**state,**result})
            if result['ready']:
                command = config.get('on_review_complete')
                if command and not state.get('continuation_started'):
                    if not isinstance(command,list) or not all(isinstance(v,str) for v in command):
                        raise ValueError('接续命令必须是参数数组')
                    # Mark before launching: a crash is reported for reconciliation, never blindly retried.
                    state.update(continuation_started=now(),continuation_command=command)
                    write_json(batch_dir/'watch.json',{**state,**result})
                    with (batch_dir/'continuation.log').open('a',encoding='utf-8') as log:
                        process = subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,
                                                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    state['continuation_pid'] = process.pid
                    write_json(batch_dir/'watch.json',{**state,**result})
                    append(batch_dir/'events.jsonl',{'e':'continuation_started','pid':process.pid})
                return result
            if once:
                return result
            time.sleep(max(2,interval))
