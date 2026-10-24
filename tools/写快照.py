# -*- coding: utf-8 -*-
"""接力记录工具（结构2）：01 只保留当前会话一条结构化记录。

docs/01-接力记录.txt 由本工具生成，不要手改。用法（参数由 agent 准备）：

  写快照.py --inspect
  写快照.py --session S --note-file F --expected-sha256 H --dry-run
  写快照.py --session S --note-file F --expected-sha256 H
  写快照.py --session S --note-file F --expected-sha256 H --handoff
  写快照.py --session S --note-file F --expected-sha256 H --migrate-legacy

--inspect / --dry-run 只读，不建锁、不写任何文件。
正式写入必须带 --expected-sha256（当前 01 原字节哈希；文件不存在用 missing），
进入 OS 锁后重算比对，防止覆盖别人并发写入的内容。
--handoff 是交接口令的提交模式：保存后当场把最终 01 归档进 06，读回验证。
--migrate-legacy 仅用于把无结构条目的旧 01 首次迁移成结构2，旧正文完整归档一次。
"""
import argparse
import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
from common.records import atomic_text, lock, now  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'docs/01-接力记录.txt'
HISTORY = ROOT / 'docs/06-历史.md'
LOCK_PATH = ROOT / 'tools/.handoff.lock'
HISTORY_MARK = '## 一、接力日志'
HISTORY_END = '## 二、旧文档存档'
MARK_RE = re.compile(r'^─+ 会话 (.+?) · 创建 (.+?) · 更新 (.+?) ─+$')
END_MARK = '<!-- handoff:end -->'
STRUCT_VER = '接力结构：2'
SECTIONS = ('当前状态', '有效决定与限制', '未决问题', '本轮必要记录', '证据', '关闭或替代')
REQUIRED_SECTIONS = SECTIONS[:5]
CLOSED_SECTION = SECTIONS[5]
PREFIX_OF = {'当前状态': 'S', '有效决定与限制': 'D', '未决问题': 'Q'}
PREAMBLE = [
    '当前接力：先读本条，了解进度、限制和用户决定。写新接力前保留仍有效的状态。',
    '本条不是任务书；下一步按用户指令执行。旧记录见 docs/06-历史.md。',
    '本文件由 tools/写快照.py 生成；写入方法见 docs/05-迭代操作.md。',
]
MISSING = 'missing'
ID_RE = re.compile(r'^\[([SDQ])(\d{3,})\]\s*(.*)$')
# 关闭说明中的替代写法：「替代D002」或「→D002」；替代目标必须存在于本次有效条目
SUB_RE = re.compile(r'(?:替代|→)\s*([SDQ]\d{3,})')

STAGE_INPUT = '阶段1（输入校验）'
STAGE_HASH = '阶段2（基准哈希校验）'
STAGE_ARCHIVE_OLD = '阶段3（旧版归档）'
STAGE_WRITE = '阶段4（写入01）'
STAGE_HANDOFF = '阶段5（交接归档）'
STAGE_VERIFY = '阶段6（读回校验）'


class HandoffError(Exception):
    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage


def file_sha(path):
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def norm_body(body):
    return '\n'.join(line.rstrip() for line in body.strip().split('\n'))


def rec_key(r):
    return (r['session'], r['created'], r['updated'], norm_body(r['body']))


# ---------- 解析 ----------

def scan_records(lines, source):
    """按 MARK/END_MARK 切记录；结构错误直接抛 ValueError。"""
    records, cur = [], None
    for idx, line in enumerate(lines, 1):
        m = MARK_RE.fullmatch(line)
        if m:
            if cur is not None:
                raise ValueError('%s 第%d行：连续记录头（嵌套记录）' % (source, idx))
            cur = {'session': m.group(1), 'created': m.group(2),
                   'updated': m.group(3), 'body_lines': []}
            continue
        if line == END_MARK:
            if cur is None:
                raise ValueError('%s 第%d行：缺少记录头的结束标记' % (source, idx))
            cur['body'] = '\n'.join(cur.pop('body_lines')).strip('\n')
            records.append(cur)
            cur = None
            continue
        if cur is not None:
            cur['body_lines'].append(line)
    if cur is not None:
        raise ValueError('%s：记录未闭合，缺少 %s' % (source, END_MARK))
    return records


def parse_sections(body):
    """解析结构2正文，返回 (items, closed, order)。items/closed: 编号->文本。

    条目正文去空白后必须非空；关闭条目必须有非空说明；必填节没条目必须写「无」。
    """
    lines = body.split('\n')
    if lines[0].strip() != STRUCT_VER:
        raise ValueError('正文第一行必须是「%s」' % STRUCT_VER)
    items, closed, order = {}, {}, []
    sec_state = {}   # 节名 -> None(空)/'none'(写了无)/'items'(有条目)
    sec, sec_done = None, False
    for idx, raw in enumerate(lines[1:], 2):
        line = raw.strip()
        if line == '':
            if sec is not None:
                sec_done = True
            continue
        head = next((n for n in SECTIONS if line == n + '：'), None)
        if head:
            if sec is not None and not sec_done:
                raise ValueError('第%d行：节与节之间必须空行分隔（%s）' % (idx, head))
            if head in order:
                raise ValueError('第%d行：重复节「%s」' % (idx, head))
            order.append(head)
            sec, sec_done = head, False
            sec_state[head] = None
            continue
        if sec is None:
            raise ValueError('第%d行：结构行后、第一节前的未知正文：%s' % (idx, line))
        if sec_done:
            raise ValueError('第%d行：节已结束后又出现内容：%s' % (idx, line))
        if line == '无':
            if sec_state.get(sec) == 'items':
                raise ValueError('第%d行：「无」与条目混用：%s' % (idx, sec))
            sec_state[sec] = 'none'
            sec_done = True
            continue
        if not line.startswith('- '):
            raise ValueError('第%d行：条目必须以「- 」开头：%s' % (idx, line))
        entry = line[2:].strip()
        if not entry:
            raise ValueError('第%d行：条目不能为空（没条目就写「无」）' % idx)
        m = ID_RE.match(entry)
        if sec in PREFIX_OF:
            if not m:
                raise ValueError('第%d行：%s 的条目必须带 [S/D/Q编号]：%s' % (idx, sec, line))
            sid = m.group(1) + m.group(2)
            if sid[0] != PREFIX_OF[sec]:
                raise ValueError('第%d行：编号 %s 不属于「%s」' % (idx, sid, sec))
            if sid in items or sid in closed:
                raise ValueError('第%d行：重复编号 %s' % (idx, sid))
            if not m.group(3).strip():
                raise ValueError('第%d行：条目 %s 的正文不能为空' % (idx, sid))
            items[sid] = m.group(3).strip()
            sec_state[sec] = 'items'
        elif sec == CLOSED_SECTION:
            if not m:
                raise ValueError('第%d行：关闭节条目必须以被关闭编号开头：%s' % (idx, line))
            sid = m.group(1) + m.group(2)
            if sid in items or sid in closed:
                raise ValueError('第%d行：重复编号 %s' % (idx, sid))
            if not m.group(3).strip():
                raise ValueError('第%d行：关闭 %s 必须带非空说明（为何关闭/被谁替代）' % (idx, sid))
            closed[sid] = m.group(3).strip()
            sec_state[sec] = 'items'
        else:
            # 本轮必要记录 / 证据：任意非空文本条目
            sec_state[sec] = 'items'
    missing = [n for n in REQUIRED_SECTIONS if n not in order]
    if missing:
        raise ValueError('缺少必需节：%s' % '、'.join(missing))
    if order[:len(REQUIRED_SECTIONS)] != list(REQUIRED_SECTIONS):
        raise ValueError('节顺序必须是：%s' % '、'.join(REQUIRED_SECTIONS))
    extra = order[len(REQUIRED_SECTIONS):]
    if extra and extra != [CLOSED_SECTION]:
        raise ValueError('关闭或替代节只能出现在最后')
    bare = [n for n in REQUIRED_SECTIONS if sec_state.get(n) is None]
    if bare:
        raise ValueError('必填节没有条目时必须明确写「无」，不能留空节：%s' % '、'.join(bare))
    return items, closed, order


def classify(rec):
    body = rec['body']
    first = body.split('\n', 1)[0].strip() if body else ''
    if first == STRUCT_VER:
        rec['struct'] = 2
        rec['items'], rec['closed'], _ = parse_sections(body)
    else:
        if first.startswith('接力结构') or re.search(r'(?m)^- \[[SDQ]\d{3,}\]', body):
            raise ValueError('接力结构头损坏或版本不支持，不能按旧格式迁移')
        rec['struct'] = 1
        rec['items'], rec['closed'] = {}, {}
    return rec


def parse_01_bytes(raw):
    """严格解析 01 文件字节：最多一条记录，页眉、边界、正文全部校验。"""
    if not raw.strip():
        return []
    text = raw.decode('utf-8').replace('\r\n', '\n')
    lines = text.split('\n')
    if lines[:len(PREAMBLE)] != PREAMBLE:
        raise ValueError('01：未知页眉（正文必须由本工具生成，先迁移再手工排查）')
    rest = lines[len(PREAMBLE):]
    while rest and not rest[0].strip():
        rest.pop(0)
    if not rest:
        raise ValueError('01：非空文件但解析不到记录')
    if not MARK_RE.fullmatch(rest[0]):
        raise ValueError('01：页眉与记录之间存在未知正文：%s' % rest[0])
    while rest and not rest[-1].strip():
        rest.pop()
    if rest[-1] != END_MARK:
        raise ValueError('01：缺少结束标记或结束标记之后存在正文')
    records = scan_records(rest, '01')
    if len(records) != 1:
        raise ValueError('01：必须恰好一条记录，实际 %d 条' % len(records))
    return [classify(records[0])]


def history_region(text):
    """返回 06 日志区的文本切片边界（cut 到结束边界行首）。"""
    lines = text.replace('\r\n', '\n').split('\n')
    start = next((i for i, l in enumerate(lines) if l.strip() == HISTORY_MARK), None)
    if start is None:
        raise ValueError('06：缺少「%s」区' % HISTORY_MARK)
    end = next((i for i, l in enumerate(lines[start + 1:], start + 1)
                if l.strip() == HISTORY_END), None)
    if end is None:
        raise ValueError('06：缺少「%s」边界' % HISTORY_END)
    region = lines[start + 1:end]
    while region and not region[-1].strip():
        region.pop()
    records = scan_records(region, '06接力日志区')
    for r in records:
        # 旧正文首行不是结构2，classify 会保留为 struct=1；一旦标称结构2，
        # 就必须完整解析，损坏时拒绝读取，不能清空编号库存后继续。
        classify(r)
    in_rec = False
    for line in region:
        if MARK_RE.fullmatch(line):
            in_rec = True
        elif line == END_MARK:
            in_rec = False
        elif not in_rec and line.strip():
            raise ValueError('06接力日志区：记录外未知正文：%s' % line)
    return records


def load_history():
    try:
        text = HISTORY.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as e:
        raise ValueError('06读取失败（%s）：%s' % (HISTORY, e)) from e
    return history_region(text)


def archived_keys():
    return {rec_key(r) for r in load_history()}


# ---------- 迁移检查 ----------

def merge_plan(old_rec, new_items, new_closed, same_session):
    """旧有效条目 -> 新记录的保留/关闭计划与违规列表。"""
    errors = []
    old_items, old_closed = old_rec['items'], old_rec['closed']
    old_ids = set(old_items) | set(old_closed)
    old_valid = set(old_items)
    kept, updated, closed_now = [], [], []
    for sid in sorted(old_valid):
        if sid in new_items:
            if sid[0] == 'D' and old_items[sid].strip() != new_items[sid].strip():
                errors.append('D原编号改义（%s）：决定变化应新增编号并关闭旧条' % sid)
            (updated if norm_body(old_items[sid]) != norm_body(new_items[sid]) else kept).append(sid)
        elif sid in new_closed:
            closed_now.append(sid)
        else:
            errors.append('有效条目遗漏（%s）：保留或在「关闭或替代」说明依据' % sid)
    for sid in sorted(old_closed):
        if same_session and sid not in new_closed:
            errors.append('同会话丢失关闭说明（%s）' % sid)
    for sid in sorted(set(new_items) & set(old_closed)):
        errors.append('复用已关闭编号（%s）：新事实应使用新编号' % sid)
    fresh = sorted(set(new_items) - old_ids)
    for sid in sorted(set(new_closed) - old_ids):
        errors.append('关闭了不存在的编号（%s）' % sid)
    return {'kept': kept, 'updated': updated, 'closed': closed_now, 'new': fresh,
            'errors': errors}


# ---------- 渲染 ----------

def render_doc(rec):
    return '\n'.join(PREAMBLE) + '\n\n\n' + render_block([rec])


def render_block(records):
    parts = []
    for r in records:
        head = ' 会话 %s · 创建 %s · 更新 %s ' % (r['session'], r['created'], r['updated'])
        width = max(72, len(head) + 8)
        pad = width - len(head)
        parts.append('─' * (pad // 2) + head + '─' * (pad - pad // 2))
        parts.append(r['body'])
        parts.append(END_MARK)
    return '\n\n'.join(parts)


def archive(records):
    """把 records 插入 06 日志区顶部，按 (session,created,updated,body) 去重。"""
    if not HISTORY.exists():
        raise FileNotFoundError('缺少 %s，先把它建好再归档' % HISTORY)
    text = HISTORY.read_text(encoding='utf-8')
    lines = text.replace('\r\n', '\n').split('\n')
    start = next((i for i, l in enumerate(lines) if l.strip() == HISTORY_MARK), None)
    if start is None:
        raise ValueError('06：缺少「%s」区' % HISTORY_MARK)
    cut = start + 1  # 标题行的下一行行首
    end = next((i for i, l in enumerate(lines[cut:], cut) if l.strip() == HISTORY_END), None)
    if end is None:
        raise ValueError('06：缺少「%s」边界' % HISTORY_END)
    existing = {rec_key(r) for r in history_region(text)}
    pending = [r for r in records if rec_key(r) not in existing]
    if pending:
        block = render_block(pending)
        new_text = '\n'.join(lines[:cut]) + '\n\n' + block + '\n\n' + '\n'.join(lines[cut:])
        atomic_text(HISTORY, new_text)
    return pending


# ---------- 只读视图 ----------

def inspect():
    out = {'file': str(TARGET), 'exists': TARGET.exists()}
    if TARGET.exists():
        raw = TARGET.read_bytes()
        out['sha256'] = hashlib.sha256(raw).hexdigest()
        try:
            records = parse_01_bytes(raw)
            out['parse_ok'] = True
            if records:
                r = records[0]
                out.update({'struct': r['struct'], 'session': r['session'],
                            'created': r['created'], 'updated': r['updated'],
                            'valid_ids': sorted(r['items']),
                            'closed_ids': sorted(r['closed'])})
            else:
                out.update({'struct': 'empty', 'session': '', 'valid_ids': [],
                            'closed_ids': []})
        except ValueError as e:
            out['parse_ok'] = False
            out['problem'] = str(e)
    else:
        out['sha256'] = MISSING
    try:
        keys = archived_keys()
        if TARGET.exists():
            raw = TARGET.read_bytes()
            records = parse_01_bytes(raw) if out.get('parse_ok', True) else []
            out['archived'] = bool(records) and rec_key(records[0]) in keys
        out['history_ok'] = True
    except (OSError, UnicodeError, ValueError) as e:
        out['history_ok'] = False
        out['history_problem'] = str(e)
        out['archived'] = None
    return out


# ---------- 写入流程 ----------

def common_checks(session, note, old, migrate=False):
    """dry-run 与正式提交共用的只读校验：结构、迁移条件、保留/关闭/替代、历史编号。

    两条路径必须得到同一结论；哈希与并发保护由调用方各自负责
    （正式提交在锁内比对，dry-run 在外比对）。
    返回 {'ok','errors','plan','idempotent'}；正文结构非法时抛 ValueError。
    """
    errors = []
    if old and old['struct'] == 1 and not migrate:
        return {'ok': False,
                'errors': ['当前01为旧格式（无结构条目），正式提交须用 --migrate-legacy'],
                'plan': None, 'idempotent': False}
    if migrate and old and old['struct'] == 2:
        errors.append('01已是结构2，--migrate-legacy 不能再用于跳过校验')
    new_items, new_closed, _ = parse_sections(note.strip())
    if old and old['struct'] == 2:
        plan = merge_plan(old, new_items, new_closed, old['session'] == session)
        errors.extend(plan['errors'])
    else:
        plan = {'kept': [], 'updated': [], 'closed': [], 'new': sorted(new_items),
                'errors': []}
    # 替代关系只约束"本次新发生的关闭"：目标必须是本次新增编号，
    # 原有有效编号不能顶替（决定改变=新增编号+关闭旧条）。
    # 原样保留的关闭说明（同会话刷新、重复handoff、跨会话沿用）不重判，
    # 否则已完成的历史替代会因目标"现在已存在"被误拒。
    fresh = set(plan['new'])
    old_closed_map = old['closed'] if old and old['struct'] == 2 else {}
    for sid in sorted(new_closed):
        text = new_closed[sid]
        m = SUB_RE.search(text)
        if not m:
            continue
        if old_closed_map.get(sid) and norm_body(old_closed_map[sid]) == norm_body(text):
            continue
        target = m.group(1)
        if target == sid:
            errors.append('替代目标不能是被关闭编号自身（%s）' % sid)
        elif target not in new_items:
            errors.append('替代目标 %s 不在本次有效条目中（替代写法必须指向本次新增的编号）'
                          % target)
        elif target not in fresh:
            errors.append('替代目标 %s 不是本次新增编号（替代关系必须指向本次新增的编号，'
                          '原有有效编号不能充当替代目标）：%s' % (target, sid))
    used_ever = set()
    for r in load_history():
        used_ever |= set(r.get('items', {})) | set(r.get('closed', {}))
    if old:
        used_ever |= set(old['items']) | set(old['closed'])
    clashes = sorted(set(new_items) & used_ever - (set(old['items']) if old else set()))
    if clashes:
        errors.append('新增编号与历史编号冲突（编号必须全局唯一且稳定）：%s'
                      % '、'.join(clashes))
    idempotent = bool(
        old and old['session'] == session
        and norm_body(old['body']) == norm_body(note.strip()))
    return {'ok': not errors, 'errors': errors, 'plan': plan, 'idempotent': idempotent}


def validate_note(session, note):
    if not session or not session.strip():
        raise HandoffError(STAGE_INPUT, 'session 不能为空')
    if '\n' in session or '\r' in session or ' · 创建 ' in session or ' · 更新 ' in session:
        raise HandoffError(STAGE_INPUT, 'session 含非法字符')
    if not note or not note.strip():
        raise HandoffError(STAGE_INPUT, '摘要不能为空')
    if END_MARK in note or any(MARK_RE.fullmatch(l) for l in note.split('\n')):
        raise HandoffError(STAGE_INPUT, '摘要不能包含记录分隔符或 %s' % END_MARK)
    try:
        parse_sections(note.strip())
    except ValueError as e:
        raise HandoffError(STAGE_INPUT, '摘要不符合结构2：%s' % e)


def run_write(session, note, expected, handoff=False, migrate=False):
    """完整写入流程；失败抛 HandoffError（带阶段），保证 01/06 不处于半写状态。"""
    with lock(LOCK_PATH):
        raw = TARGET.read_bytes() if TARGET.exists() else None
        actual = hashlib.sha256(raw).hexdigest() if raw else MISSING
        if expected != actual:
            raise HandoffError(STAGE_HASH,
                               '基准哈希不符：当前01=%s，请求=%s。请重新读取01、'
                               '合并最新内容后再提交' % (actual, expected))
        try:
            records = parse_01_bytes(raw) if raw else []
        except ValueError as e:
            raise HandoffError(STAGE_INPUT, '当前01不可解析：%s' % e)
        old = records[0] if records else None
        try:
            verdict = common_checks(session, note, old, migrate)
        except ValueError as e:
            raise HandoffError(STAGE_INPUT, str(e))
        if not verdict['ok']:
            raise HandoffError(STAGE_INPUT, '校验未通过：\n- ' + '\n- '.join(verdict['errors']))
        idempotent = verdict['idempotent']
        archived_old = 0
        if old and (migrate or old['session'] != session):
            try:
                archived_old = len(archive([old]))
            except (OSError, UnicodeError, ValueError) as e:
                raise HandoffError(STAGE_ARCHIVE_OLD,
                                   '01保持旧版可读，旧会话归档失败：%s。修复06后用原01哈希重试'
                                   % e) from e
        if idempotent:
            final = old
        else:
            final = {'session': session,
                     'created': old['created'] if old and old['session'] == session else now(),
                     'updated': now(), 'body': note.strip()}
            try:
                atomic_text(TARGET, render_doc(final))
            except OSError as e:
                raise HandoffError(STAGE_WRITE, '01写入失败（旧版仍可读）：%s' % e)
        if handoff:
            try:
                archive([final])
            except (OSError, ValueError) as e:
                raise HandoffError(STAGE_HANDOFF,
                                   '01已保存，交接归档未完成：%s。重试同正文会复用原updated并补档' % e)
        try:
            back = parse_01_bytes(TARGET.read_bytes())
        except (OSError, ValueError) as e:
            raise HandoffError(STAGE_VERIFY, '读回01失败：%s' % e)
        if not back or rec_key(back[0]) != rec_key(final):
            raise HandoffError(STAGE_VERIFY, '读回01与写入版本不一致')
        if handoff and rec_key(final) not in archived_keys():
            raise HandoffError(STAGE_VERIFY, '06中未见最终版本归档')
        return {'session': session, 'created': final['created'], 'updated': final['updated'],
                'idempotent': idempotent, 'archived_old': archived_old,
                'handoff_archived': bool(handoff), 'plan': verdict['plan'],
                'final_sha256': file_sha(TARGET)}


def run_dry(session, note, expected, migrate=False):
    out = {'session': session, 'baseline': {}}
    raw = TARGET.read_bytes() if TARGET.exists() else None
    actual = hashlib.sha256(raw).hexdigest() if raw else MISSING
    out['baseline']['current_sha256'] = actual
    out['baseline']['matches'] = expected == actual
    if not out['baseline']['matches']:
        out['ok'] = False
        out['error'] = '基准哈希不符：当前01=%s，请求=%s' % (actual, expected)
        return out
    try:
        records = parse_01_bytes(raw) if raw else []
    except ValueError as e:
        out.update({'ok': False, 'error': '当前01不可解析：%s' % e})
        return out
    old = records[0] if records else None
    # 与正式提交共用 common_checks：结构、保留/关闭/替代、历史编号冲突同一份规则
    try:
        verdict = common_checks(session, note, old, migrate)
    except ValueError as e:
        out.update({'ok': False, 'error': str(e)})
        return out
    out.update({'ok': verdict['ok'], 'idempotent': verdict['idempotent'],
                'plan': verdict['plan']})
    if not verdict['ok']:
        out['error'] = '\n- '.join(verdict['errors'])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--session', help='稳定的会话 ID（同一会话反复使用同一个）')
    group = ap.add_mutually_exclusive_group()
    group.add_argument('--note-file', type=Path, help='摘要文件（UTF-8）')
    group.add_argument('--note', help='摘要文本')
    ap.add_argument('--expected-sha256',
                    help='当前01原字节SHA-256；正式写入必填，文件不存在用 missing')
    ap.add_argument('--inspect', action='store_true', help='只读输出01结构状态')
    ap.add_argument('--dry-run', action='store_true', help='预检，不写任何文件')
    ap.add_argument('--handoff', action='store_true', help='交接：保存后当场归档最终01')
    ap.add_argument('--migrate-legacy', action='store_true',
                    help='仅首次迁移旧格式01；旧正文完整归档一次')
    args = ap.parse_args(argv)

    try:
        if args.inspect:
            if args.note is not None or args.note_file or args.dry_run \
                    or args.handoff or args.migrate_legacy:
                raise HandoffError(STAGE_INPUT, '--inspect 单独使用')
            print(__import__('json').dumps(inspect(), ensure_ascii=False, indent=1))
            return 0
        note = (args.note_file.read_text(encoding='utf-8-sig') if args.note_file
                else args.note)
        if note is not None and isinstance(note, str):
            note = note.replace('\r\n', '\n')
        if not args.session or note is None:
            raise HandoffError(STAGE_INPUT, '写入/预检需要 --session 和 --note/--note-file')
        if not args.expected_sha256 or not args.expected_sha256.strip():
            raise HandoffError(STAGE_INPUT, '正式写入必须带 --expected-sha256（可先用 --inspect 获取）')
        validate_note(args.session, note)
        if args.dry_run:
            out = run_dry(args.session, note, args.expected_sha256.strip(),
                          migrate=args.migrate_legacy)
            print(__import__('json').dumps(out, ensure_ascii=False, indent=1))
            return 0 if out.get('ok') else 1
        result = run_write(args.session, note, args.expected_sha256.strip(),
                           handoff=args.handoff, migrate=args.migrate_legacy)
        result['file'] = str(TARGET)
        print(__import__('json').dumps(result, ensure_ascii=False, indent=1))
        return 0
    except HandoffError as e:
        print('%s %s' % (e.stage, e), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
