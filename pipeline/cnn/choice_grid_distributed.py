"""三路分工复查：复用旧结果，疑难交叉复查；不写审核或训练标签。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import queue
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cnn import choice_grid_prelabel as grid
from common.records import read_json, write_json, now, lock


def needs_cross(rows, results):
    for i, row in enumerate(rows):
        votes = [r['items'][i] for r in results.values()]
        if len({v['answer'] for v in votes}) > 1:
            return True
        if any(v['answer'] is None or v['answer'] != row['reference']['answer'] or
               any(v[k] for k in ('uncertain', 'crop_issue', 'special')) for v in votes):
            return True
    return False


def run(output, deepseek_model):
    grid.ROUTES['deepseek'] = {**grid.ROUTES['deepseek'], 'model': deepseek_model}
    output = Path(output)
    with lock(output/'.run.lock'):
        selection = read_json(output/'selection.json')
        batches = output/'batches'
        size = selection['batch_size']
        jobs = []
        mutex = threading.Lock()
        started = time.monotonic()
        failures = []
        calls = 0
        for lib in selection['libraries']:
            for index in range(0, len(lib['rows']), size):
                rows = lib['rows'][index:index+size]
                results = {}
                for route in grid.ROUTES:
                    path = batches/('%d_%s_%06d.json' % (lib['id'], route, index))
                    if path.exists():
                        result = read_json(path)
                        if result.get('ok') and [v['file'] for v in result['items']] == [r['file'] for r in rows]:
                            results[route] = result
                jobs.append({'lib':lib, 'index':index, 'rows':rows, 'results':results})

        def status(phase):
            counts = {r:sum(len(j['rows']) for j in jobs if r in j['results']) for r in grid.ROUTES}
            write_json(output/'status.json', {'updated':now(), 'phase':phase,
                'mode':'distributed_first_pass_then_risk_crosscheck',
                'total_images':sum(len(j['rows']) for j in jobs),
                'images_with_any_result':sum(len(j['rows']) for j in jobs if j['results']),
                'images_with_all_three':sum(len(j['rows']) for j in jobs if len(j['results']) == 3),
                'route_images':counts, 'new_requests':calls, 'failed_requests':len(failures),
                'elapsed_seconds':time.monotonic()-started, 'complete':phase=='completed', 'label_writes':0})

        def execute(job, route, phase):
            nonlocal calls
            result = grid.recognize(Path(job['lib']['base']), job['rows'], route)
            with mutex:
                calls += 1
                if result['ok']:
                    path = batches/('%d_%s_%06d.json' % (job['lib']['id'], route, job['index']))
                    write_json(path, result)
                    job['results'][route] = result
                else:
                    failures.append({'library':job['lib']['id'], 'index':job['index'], 'route':route, 'result':result})
                    write_json(output/'distributed_failures.json', failures)
                status(phase)
                print('%s requests=%d failures=%d' % (phase, calls, len(failures)), flush=True)

        pending = queue.Queue()
        for job in jobs:
            if not job['results']:
                pending.put(job)
        write_json(output/'distributed_run.json', {'created':now(), 'routes':grid.ROUTES,
            'reused_images':sum(len(j['rows']) for j in jobs if j['results']),
            'pending_batches':pending.qsize(), 'label_writes':0})
        status('distributed_first_pass')

        def first_worker(route):
            while True:
                try:
                    job = pending.get_nowait()
                except queue.Empty:
                    return
                execute(job, route, 'distributed_first_pass')

        with ThreadPoolExecutor(max_workers=sum(c['workers'] for c in grid.ROUTES.values())) as pool:
            futures = [pool.submit(first_worker, r) for r,c in grid.ROUTES.items() for _ in range(c['workers'])]
            for future in futures:
                future.result()

        # Only absent or risky batches need another independent route; never overwrite an existing vote.
        queues = {r:queue.Queue() for r in grid.ROUTES}
        for job in jobs:
            results = job['results']
            if not results or (len(results) < 2 and needs_cross(job['rows'], results)):
                route = next(r for r in ('gemini', 'mimo', 'deepseek') if r not in results)
                queues[route].put(job)
        status('risk_crosscheck')

        def cross_worker(route):
            while True:
                try:
                    job = queues[route].get_nowait()
                except queue.Empty:
                    return
                execute(job, route, 'risk_crosscheck')

        with ThreadPoolExecutor(max_workers=sum(c['workers'] for c in grid.ROUTES.values())) as pool:
            futures = [pool.submit(cross_worker, r) for r,c in grid.ROUTES.items() for _ in range(c['workers'])]
            for future in futures:
                future.result()

        rows_out = []
        for job in jobs:
            for i,row in enumerate(job['rows']):
                votes = {r:result['items'][i] for r,result in job['results'].items()}
                answers = {v['answer'] for v in votes.values()}
                rows_out.append({'library':job['lib']['id'], 'file':row['file'], 'reference':row['reference'],
                    'votes':votes, 'models':{r:v.get('model') for r,v in job['results'].items()},
                    'disagreement':len(answers)>1, 'human_review_required':True})
        write_json(output/'distributed_results.json', {'created':now(), 'items':rows_out, 'label_writes':0})
        missing = sum(not row['votes'] for row in rows_out)
        write_json(output/'distributed_completion.json', {'created':now(), 'total':len(rows_out),
            'missing_images':missing, 'disagreements':sum(r['disagreement'] for r in rows_out),
            'label_writes':0, 'reviewed_automatically':False})
        status('incomplete_resumable' if missing or failures else 'completed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--deepseek-model', default='deepseek-v4.1-flash-1017')
    args = parser.parse_args()
    run(args.output, args.deepseek_model)
