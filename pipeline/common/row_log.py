# -*- coding: utf-8 -*-
"""每张样本一个中文日志：data/<库>/log/<样本名>.log，一行一个事件。

digit 和 choice 用这里；检测框（m1-m4）用 events.py。
日志是唯一事实源，审核状态每次从事件重算。
"""
import json
from pathlib import Path

from .records import read_events, now


def log_path(base, name):
    return Path(base) / 'log' / (name + '.log')


def read(base, name):
    p = log_path(base, name)
    return read_events(p) if p.exists() else []


def append(base, name, event):
    ev = {'时间': event.get('时间') or now()}
    ev.update({k: v for k, v in event.items() if k != '时间'})
    p = log_path(base, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists() or p.stat().st_size == 0
    with p.open('a', encoding='utf-8-sig' if new else 'utf-8', newline='\n') as f:
        f.write(json.dumps(ev, ensure_ascii=False) + '\n')
    return ev


def event_name(ev):
    return ev.get('事件') or ev.get('e') or ''


def choice_state(events):
    """一行选择题的状态：答案、是否已审、特殊处理。"""
    st = {'answer': '', 'pred_answer': '', 'reviewed': False, 'special': False,
          'note': '', 'decision': '', 'weight': 0.0, 'reason': '', 'status': '',
          'review_count': 0, 'reviewed_at': ''}
    for e in events:
        t = event_name(e)
        if t in ('预标注', 'pred'):
            a = e.get('预测答案')
            if a is None:
                a = e.get('answer', '')
            st.update(pred_answer=a, answer=a, reviewed=False, special=False,
                      note='', decision='', weight=0.0, reason='', status='')
        elif t in ('审核', 'review'):
            st.update(answer=e.get('答案', e.get('answer', st['answer'])),
                      reviewed=True, special=False, note='', decision='',
                      weight=0.0, reason='', status='')
            st['review_count'] = int(e.get('第几轮', e.get('n', st['review_count'] + 1)))
            st['reviewed_at'] = e.get('时间', '')
        elif t in ('特殊', 'special'):
            st.update(answer=e.get('答案', st['answer']), reviewed=True, special=True,
                      note=e.get('类型') or e.get('note') or '特殊',
                      decision=e.get('处理', ''),
                      weight=float(e.get('权重', 0) or 0),
                      reason=e.get('原因', ''), status=e.get('状态', ''))
            st['reviewed_at'] = e.get('时间', '')
        elif t in ('撤销审核', 'unreview'):
            st.update(reviewed=False, special=False, note='', decision='',
                      weight=0.0, reason='', status='', answer=st['pred_answer'])
    return st


def digit_state(events):
    """一个数字样本的状态：标签、是否人工确认、旧记录依据。"""
    st = {'label': None, 'pred_label': None, 'reviewed': False, 'special': False,
          'basis': '', 'split': '', 'note': '', 'review_count': 0, 'reviewed_at': ''}
    for e in events:
        t = event_name(e)
        if t in ('预标注', 'pred'):
            st['pred_label'] = e.get('标签', e.get('label'))
            st['label'] = st['pred_label']
            st['reviewed'] = False
        elif t in ('审核', 'review'):
            st['label'] = int(e.get('标签', e.get('label', st['label'])))
            st.update(reviewed=True, special=False)
            st['review_count'] = int(e.get('第几轮', e.get('n', st['review_count'] + 1)))
            st['reviewed_at'] = e.get('时间', '')
        elif t in ('旧记录', 'legacy'):
            st['label'] = int(e.get('标签', e.get('label', st['label'])))
            st['basis'] = e.get('依据', e.get('basis', ''))
            st['split'] = e.get('分区', e.get('split', ''))
            st['reviewed'] = st['basis'] != '推断'
            st['reviewed_at'] = e.get('时间', '')
        elif t in ('特殊', 'special'):
            st['label'] = int(e.get('标签', st['label']))
            st.update(special=True, note=e.get('类型') or e.get('note') or '特殊')
        elif t in ('撤销审核', 'unreview'):
            st['reviewed'] = False
    return st
