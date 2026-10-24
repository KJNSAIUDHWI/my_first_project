# -*- coding: utf-8 -*-
"""一次迭代的入口：冻结快照 → 训练 → 完整性检查 → 永久留档；部署另行授权。

用法：
  py3136 tools\\迭代.py snapshot --task digit --purpose 补多选区
  py3136 tools\\迭代.py train    --task digit                （接上一次 snapshot 的 run）
  py3136 tools\\迭代.py status

不给 --run 时按 <模块>_<YYYYMMDD_HHMMSS>_<用途> 生成；snapshot 把 run 记进
train/.last_run.json，train 不传 --run 就接上一次。

底层入口拒绝直接 --promote，普通训练应经由本工具，
由 run_training.supervise 统一做退出码、指标有限性、权重可加载检查和永久留档。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'pipeline'))
from common import cnn_data, yolo_snapshot              # noqa: E402
from common.model_registry import current, publish, model_record_path               # noqa: E402
from common.paths import TRAIN                          # noqa: E402
from common.records import now, read_json, write_json    # noqa: E402
from common.run_training import supervise                # noqa: E402

PY = sys.executable
CNN_TASKS = ('digit', 'choice')
YOLO_TASKS = ('m1', 'm2')
LAST_RUN = TRAIN / '.last_run.json'

# m1/m2 的定档配方（来自 2026-10-23 两次成功 run 的 args.yaml），
# 不要用 train_base.py 的脚本默认值：m1 默认是 SGD lr0=0.01，直接跑会 NaN。
YOLO_RECIPES = {
    'm1': ['--optimizer', 'AdamW', '--lrf', '0.01', '--warmup', '3',
           '--warmup-bias-lr', '0.0001', '--amp', '--scale', '0', '--translate', '0'],
    'm2': ['--optimizer', 'AdamW', '--lrf', '0.01', '--warmup', '3',
           '--warmup-bias-lr', '0.0001', '--amp', '--scale', '0', '--translate', '0',
           '--fliplr', '0'],
}


def _run_name(task, purpose):
    return '%s_%s_%s' % (task, datetime.now().strftime('%Y%m%d_%H%M%S'), purpose)


def _purpose_of(run):
    parts = Path(run).name.split('_', 3)
    return parts[3] if len(parts) == 4 else '迭代完成'


def cmd_snapshot(args):
    task = args.task
    name = args.run or _run_name(task, args.purpose)
    run_dir = (TRAIN / name).resolve()
    if run_dir.parent != TRAIN.resolve():
        raise ValueError('run必须在train下一级')
    out = run_dir / 'dataset_snapshot'
    if not args.check_only and any((run_dir / p).exists() for p in
                                   ('launch.json','results.csv','completion_check.json','candidate.json')):
        raise FileExistsError('已有训练现场，不能为旧run重建快照；请另建run：' + str(run_dir))
    if out.exists() and not args.check_only:
        raise SystemExit('快照已存在，换一个 --run 或先确认它可以复用：%s' % out)
    if task in CNN_TASKS:
        if args.check_only:
            rows, inferred = cnn_data.collect(task, args.allow_inferred, persist_cache=False)
            print('检查通过：%d 条，推断标签排除 %d 条（未复制文件）' % (len(rows), len(inferred)))
            return None
        rows = cnn_data.freeze(task, out, args.allow_inferred)
        excluded = len(read_json(out / 'selection.json', {}).get('inferred_excluded') or [])
        print('快照完成 %s：%d 条，排除推断标签 %d 条' % (out, len(rows), excluded))
    elif task in YOLO_TASKS:
        if args.stage is None:
            raise SystemExit('%s 需要 --stage（累计范围）；--subset只约束本批审核' % task)
        audit = yolo_snapshot.freeze(task, args.subset, out,
                                     args.decisions if args.decisions else None,
                                     check_only=args.check_only, stage=args.stage,
                                     skip_invalid=args.skip_invalid, defer_list=args.defer_list)
        print('快照%s：train/val=%s，明确排除 %d、超出 stage %d、坏标签 %d%s' % (
            '检查通过' if args.check_only else '完成 ' + str(out),
            json.dumps(audit['counts'], ensure_ascii=False),
            len(audit['excluded']), audit['skipped_stage'],
            len(audit['skipped_invalid']),
            '' if not audit['clips'] else '，轻微裁剪 %d 框' % len(audit['clips'])))
        for bad in audit['skipped_invalid'][:10]:
            print('   跳过坏标签：%s' % bad['error'])
        if args.check_only:
            return None
    else:
        raise SystemExit('未知 task：%s' % task)
    write_json(LAST_RUN, {'run': name, 'task': task, 'snapshot': str(out), 'time': now()})
    print('run = %s' % name)
    return name


def cmd_train(args):
    if getattr(args, 'arg', None):
        raise ValueError('--arg 已停用：请使用明确的训练参数，不能透传覆盖快照、起点或run')
    name = args.run or read_json(LAST_RUN, {}).get('run')
    if not name:
        raise SystemExit('没有可接的 run，先执行 snapshot 或用 --run 指定')
    task = args.task
    run_dir = (TRAIN / name).resolve()
    if run_dir.parent != TRAIN.resolve():
        raise ValueError('run必须在train下一级')
    snap = run_dir / 'dataset_snapshot'
    if not snap.exists():
        raise SystemExit('缺少快照：%s（先跑 snapshot）' % snap)
    manifest = read_json(snap / 'selection.json', {}) if task in CNN_TASKS else read_json(run_dir / 'snapshot_audit.json', {})
    if manifest.get('task') != task:
        raise ValueError('快照任务与训练任务不一致，或缺少当前入口审计')
    if task in YOLO_TASKS:
        yolo_snapshot.validate_training_input(task, run_dir, snap/'data.yaml')
    initial = None if args.scratch and task in CNN_TASKS else current(task)
    if task in CNN_TASKS:
        cmd = [PY, 'pipeline/cnn/train_%s.py' % task, '--name', run_dir.name,
               '--data', str(snap)]
        if args.scratch:
            cmd.append('--scratch')
        elif initial is not None:
            cmd += ['--initial', str(initial)]
        else:
            cmd.append('--scratch')
        if args.epochs:
            cmd += ['--epochs', str(args.epochs)]
        if args.batch:
            cmd += ['--batch', str(args.batch)]
        if args.lr:
            cmd += ['--lr', str(args.lr)]
    else:
        cmd = [PY, 'pipeline/%s/train_base.py' % task, '--name', run_dir.name,
               '--data', str(snap / 'data.yaml')]
        if initial is not None:
            cmd += ['--model', str(initial)]
        cmd += YOLO_RECIPES[task]
        cmd += ['--epochs', str(args.epochs or 80), '--patience', '15']
        if args.lr:
            cmd += ['--lr0', str(args.lr)]
        else:
            cmd += ['--lr0', '0.0001']
        if args.batch:
            cmd += ['--batch', str(args.batch)]
    print('将执行：%s' % ' '.join(cmd))
    result = supervise(task, run_dir, cmd, initial or (snap / 'selection.json'), _purpose_of(name),
                       deploy=False)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def cmd_deploy(args):
    run = (TRAIN / args.run).resolve()
    if run.parent != TRAIN.resolve():
        raise ValueError('run必须在train下一级')
    if not run.is_dir():
        raise ValueError('指定的 run 不存在：%s' % run)
    result = read_json(run / 'completion_check.json', {})
    if not result:
        raise ValueError('该 run 没有完成检查记录（completion_check.json）：'
                         '旧格式 run 或未完成的训练；不自动迁移、不补造 candidate')
    candidate = result.get('candidate') or {}
    if not candidate.get('path'):
        raise ValueError('完成检查缺少新格式 candidate 记录（旧格式），不能部署；'
                         '需先明确验证和迁移方案，不补造记录')
    if not result.get('success'):
        raise ValueError('完成检查未通过（success!=true），不可部署')
    if candidate.get('task') != args.task:
        raise ValueError('候选任务不符：candidate.task=%r，请求 task=%r'
                         % (candidate.get('task'), args.task))
    if Path(candidate.get('run', '')).resolve() != run:
        raise ValueError('候选来源run与本次部署run不一致')
    source = model_record_path(candidate['path'])
    from common.records import sha
    if not source.exists():
        raise ValueError('候选权重文件缺失：%s' % source)
    if sha(source) != candidate['sha256']:
        raise ValueError('候选权重与完成检查中的哈希不一致（指纹不符）：%s' % source)
    row = publish(args.task, source, run, _purpose_of(run.name))
    result['deployment'] = row
    write_json(run / 'completion_check.json', result)
    print(json.dumps(row, ensure_ascii=False, indent=2))
    return row


def cmd_status(args):
    if args.run:
        run_dir = (TRAIN / args.run).resolve()
        if run_dir.parent != TRAIN.resolve():
            raise ValueError('run必须在train下一级')
        if not run_dir.is_dir():
            raise SystemExit('指定的 run 不存在：%s（train/ 下没有该目录）' % args.run)
    else:
        name = read_json(LAST_RUN, {}).get('run')
        if not name:
            print('未找到统一入口最近 run 指针（train/.last_run.json）。'
                  '可用 --run 显式指定某次运行；当前项目状态见 docs/01-接力记录.txt。')
            return None
        run_dir = (TRAIN / name).resolve()
        if not run_dir.is_dir():
            raise SystemExit('最近指针指向的 run 不存在：%s（指针可能过期，用 --run 显式指定）' % name)
    completion = read_json(run_dir / 'completion_check.json', {})
    candidate = completion.get('candidate') or {}
    legacy = bool(completion) and 'success' not in completion
    deploy_note = _deploy_note(run_dir, completion, candidate, legacy)
    info = {
        'run': str(run_dir),
        'launch': read_json(run_dir / 'launch.json', {}),
        'heartbeat': read_json(run_dir / 'heartbeat.json', {}),
        'exit': read_json(run_dir / 'exit_status.json', {}),
        'completion': completion,
        'diagnosis': read_json(run_dir / 'diagnosis.json', {}),
        'numerical_failures': (run_dir / 'numerical_failure.jsonl').exists(),
        'results_csv': (run_dir / 'results.csv').exists(),
        'snapshot': (run_dir / 'dataset_snapshot' / 'selection.json').exists()
                    or (run_dir / 'dataset_snapshot' / 'data.yaml').exists(),
        'legacy_completion': legacy,
        'candidate_archived': bool(candidate.get('path')),
        'deploy_note': deploy_note,
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return info


def _task_of_run(run_dir):
    name = run_dir.name
    for task in CNN_TASKS + YOLO_TASKS:
        if name.startswith(task + '_'):
            return task
    return ''


def _deploy_note(run_dir, completion, candidate, legacy):
    """说明该 run 的部署结构是否可被 deploy 命令接受；不代表用户已授权部署。"""
    if not completion:
        return '无完成检查记录：旧格式或未完成，deploy 将拒绝'
    if legacy:
        return '旧格式完成记录（缺 success/candidate）：deploy 将拒绝，需先明确验证与迁移方案'
    if not completion.get('success'):
        return '完成检查未通过：不可部署'
    task = candidate.get('task') or _task_of_run(run_dir)
    if not candidate.get('path'):
        return '缺候选留档路径：不可部署'
    from common.records import sha
    try:
        source = model_record_path(candidate['path'])
    except ValueError as e:
        return '候选路径不合法：' + str(e)
    if Path(candidate.get('run', '')).resolve() != run_dir.resolve():
        return '候选来源run不符：不可部署'
    if not source.exists() or sha(source) != candidate.get('sha256'):
        return '候选权重缺失或指纹不符：不可部署'
    current_data = read_json(ROOT / 'models' / 'current.json', {})
    deployed = current_data.get(task, {})
    if deployed.get('run') == str(run_dir) and deployed.get('sha256') == candidate.get('sha256'):
        return '结构与候选均有效；该候选已登记为当前部署（%s）' % task
    return '结构与候选均有效：可经 deploy 命令部署（须用户明确同意，不代表已授权）'


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('snapshot', help='冻结一次训练用的数据快照')
    s.add_argument('--task', required=True, choices=CNN_TASKS + YOLO_TASKS)
    s.add_argument('--run', help='run 名（默认自动生成）')
    s.add_argument('--purpose', default='迭代', help='写进 run 名的用途')
    s.add_argument('--allow-inferred', action='store_true', help='CNN：纳入推断标签（默认排除）')
    s.add_argument('--subset', help='YOLO：本批必须审完的名单 CSV（可选）')
    s.add_argument('--stage', type=int, choices=(1, 2, 3, 4),
                   help='YOLO：累计纳入到第几阶段的已审训练图')
    s.add_argument('--defer-list', type=Path, help='YOLO：用户明确暂缓的训练名单CSV')
    s.add_argument('--decisions', help='YOLO：特殊样本决定 JSON')
    s.add_argument('--skip-invalid', action='store_true',
                   help='YOLO：跳过坏标签的图并写入审计（默认直接报错）')
    s.add_argument('--check-only', action='store_true', help='只检查名单和标签，不复制文件')
    s.set_defaults(func=cmd_snapshot)

    t = sub.add_parser('train', help='按快照训练、校验并永久留档；不部署')
    t.add_argument('--task', required=True, choices=CNN_TASKS + YOLO_TASKS)
    t.add_argument('--run', help='run 名（默认接上一次 snapshot）')
    t.add_argument('--epochs', type=int)
    t.add_argument('--batch', type=int)
    t.add_argument('--lr', type=float)
    t.add_argument('--scratch', action='store_true', help='CNN：随机初始化重训')
    t.add_argument('--no-deploy', action='store_true', help='兼容旧命令；训练始终不部署')
    t.add_argument('--arg', action='append', help='已停用；使用 --epochs/--batch/--lr 等明确参数')
    t.set_defaults(func=cmd_train)

    d = sub.add_parser('deploy', help='仅在用户明确同意后切换指定run的候选权重')
    d.add_argument('--task', required=True, choices=CNN_TASKS + ('m1','m2'))
    d.add_argument('--run', required=True)
    d.set_defaults(func=cmd_deploy)

    st = sub.add_parser('status', help='查看最近一次 run 的状态')
    st.add_argument('--run')
    st.set_defaults(func=cmd_status)

    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
