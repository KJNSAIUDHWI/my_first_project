"""Durable supervisor for YOLO/CNN runs; no implicit training at import."""

import argparse

import csv

import json

import math

import os

from pathlib import Path

import subprocess

import sys

import time

import traceback

from .paths import ROOT, TRAIN

from .records import write_json, read_json, sha, now, lock

from .model_registry import archive_weight

from .diagnostics import diagnose





def supervise(task, run, command, initial, purpose, deploy=False):

    if deploy:

        raise ValueError('训练不能部署；用户明确同意后使用独立deploy操作')

    run = Path(run).resolve()

    if run.parent != TRAIN.resolve():

        raise ValueError('run必须在train下一级')

    run.mkdir(parents=True, exist_ok=True)

    with lock(TRAIN / '.training.lock'):

        if (run / 'launch.json').exists() or (run / 'results.csv').exists():

            raise FileExistsError('运行已经启动过；请新建run或明确恢复，不覆盖：' + str(run))

        import importlib.metadata

        env = {p: importlib.metadata.version(p) for p in ('torch', 'ultralytics', 'numpy', 'Pillow')}

        launch = {'task': task, 'started': now(), 'supervisor_pid': os.getpid(),

                  'command': command, 'initial_model': str(initial), 'initial_model_sha256': sha(initial),

                  'environment': env, 'python': sys.version, 'purpose': purpose, 'deploy': deploy}

        # Preserve the complete small code tree, not just hashes that cannot restore it.

        import shutil

        for base in ('pipeline',):

            shutil.copytree(ROOT / base, run / 'code_snapshot' / base,

                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))

        write_json(run / 'launch.json', launch)

        write_json(TRAIN / 'active.json', {'run': str(run), 'task': task})

        start = time.monotonic()

        code = 1

        try:

            with (run / 'train.log').open('x', encoding='utf-8') as log:

                proc = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,

                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

                launch['train_pid'] = proc.pid

                write_json(run / 'launch.json', launch)

                while proc.poll() is None:

                    try:

                        write_json(run / 'heartbeat.json', {'time': now(), 'pid': proc.pid, 'status': 'running'})

                    except Exception:

                        pass

                    time.sleep(2)

                code = proc.returncode

        except BaseException:

            write_json(run / 'supervisor_error.json', {'time': now(), 'traceback': traceback.format_exc()})

            raise

        finally:

            write_json(run / 'exit_status.json', {'exit_code': code, 'finished': now(),

                                                  'seconds': round(time.monotonic()-start, 2)})

            write_json(run / 'heartbeat.json', {'time': now(), 'status': 'exited', 'exit_code': code})

        result = {'exit_code': code, 'success': False}

        try:

            if code != 0:

                raise RuntimeError('训练异常退出')

            if (run / 'numerical_failure.jsonl').exists():

                raise RuntimeError('存在致命数值故障')

            with (run / 'results.csv').open(encoding='utf-8-sig', newline='') as f:

                rows = list(csv.DictReader(f))

            if not rows or any(not math.isfinite(float(v)) for r in rows for v in r.values()):

                raise ValueError('指标为空或存在非有限值')

            from .model_registry import inspect_weight

            weight = run / ('inference.pt' if task in ('digit', 'choice') else 'weights/best.pt')

            checked = inspect_weight(weight, task)

            result.update(success=True, completed_epochs=len(rows), weight=checked, last_metrics=rows[-1])

            result['candidate'] = archive_weight(task, weight, run, purpose)

            result['deployment'] = None

        except Exception as e:

            result.update(success=False, error=str(e))

        write_json(run / 'completion_check.json', result)

        diagnose(run, save=True)

        if not result['success']:

            raise RuntimeError(result['error'])

        return result





def main():

    ap = argparse.ArgumentParser()

    ap.add_argument('--task', required=True, choices=('m1','m2','m3','digit','choice'))

    ap.add_argument('--run', required=True, type=Path)

    ap.add_argument('--initial', required=True, type=Path)

    ap.add_argument('--purpose', default='迭代完成')

    ap.add_argument('--no-deploy', action='store_true')

    ap.add_argument('command', nargs=argparse.REMAINDER)

    a = ap.parse_args()

    cmd = a.command[1:] if a.command[:1] == ['--'] else a.command

    if not cmd:

        ap.error('missing child command')

    print(json.dumps(supervise(a.task,a.run,cmd,a.initial,a.purpose,False),ensure_ascii=False))





if __name__ == '__main__':

    main()
