"""Evidence-based diagnosis, without silently changing samples or hyperparameters."""
from pathlib import Path
from .records import read_events, read_json, now, write_json


def diagnose(run, save=False):
    run = Path(run)
    failures = read_events(run / 'numerical_failure.jsonl')
    overflows = read_events(run / 'amp_overflow.jsonl')
    exit_state = read_json(run / 'exit_status.json', {})
    completion = read_json(run / 'completion_check.json', {})
    manual = read_json(run / 'manual_stop.json', {})
    first = failures[0] if failures else None
    logs = ''
    for name in ('train.log', 'train.err.log'):
        p = run / name
        if p.exists():
            with p.open('rb') as f:
                f.seek(max(0, p.stat().st_size - 32000))
                logs += f.read().decode('utf-8', 'replace')
    category, suggestion = 'none', '未发现明确故障；完成状态以退出码和完整性检查为准。'
    if first:
        where = str(first.get('where', ''))
        category = 'data' if where.startswith('input.') else 'numerical'
        suggestion = ('检查记录中的具体样本及冻结标签；复查failure_batch.pt中的变换后输入。'
                      if category == 'data' else
                      '先复查异常阶段及变换后批次，再对比学习率、warmup和AMP；不要将批次成员直接认定为坏数据，不从损坏last续训。')
    elif 'out of memory' in logs.lower():
        category, suggestion = 'oom', '显存不足：确认其他GPU进程；建议减小batch，新建运行重试，保留当前配方作为对照。'
    elif (manual.get('success') is True and manual.get('weight', {}).get('finite') is True
          and manual.get('termination') == 'user_requested_stop_after_saved_epoch'
          and manual.get('completed_epochs') == manual.get('requested_epochs')
          and manual.get('completed_epochs', 0) > 0):
        category, suggestion = 'manual_stop', f"按用户要求完成{manual['completed_epochs']}轮后停止；最佳权重已单独验收留档，原进程退出码保留。"
    elif exit_state.get('exit_code') not in (None, 0):
        category, suggestion = 'process', '训练异常退出：查看traceback及启动参数，保留部署权重，定位后新建重试。'
    elif completion.get('error'):
        category, suggestion = 'completion', '训练退出但收尾检查失败；修复检查中记录的问题后再发布，不冒充已部署。'
    result = {'time': now(), 'run': str(run), 'category': category, 'first_failure': first,
              'amp_overflow_count': len(overflows), 'exit': exit_state, 'completion': completion,
              'suggestion': suggestion, 'samples_are_suspects_not_proven_bad': True,
              'traceback_tail': logs[-6000:] if category != 'none' else ''}
    if save:
        write_json(run / 'diagnosis.json', result)
    return result
