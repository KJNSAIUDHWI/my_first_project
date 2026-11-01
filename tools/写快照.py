# -*- coding: utf-8 -*-
"""结构3接力：当前状态覆盖保存；显式阶段事件入索引，交接快照按内容留档。"""
import argparse
import hashlib
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
from common.records import atomic_text, lock, now
ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'docs/01-接力记录.txt'
HISTORY = ROOT / 'docs/06-历史.md'
STORE = ROOT / 'docs/history'
EVENTS = STORE / 'events.jsonl'
SECTIONS = ('当前状态', '本轮限制', '待办与阻塞', '证据入口')
MISSING = 'missing'

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def validate_body(body):
    lines = body.strip().splitlines()
    if not lines or lines[0] != '接力结构：3':
        raise ValueError('正文必须使用接力结构：3；旧编号规则已退休')
    heads = [line[:-1] for line in lines[1:] if line.endswith('：') and not line.startswith('- ')]
    if heads != list(SECTIONS):
        raise ValueError('依次填写：' + '、'.join(SECTIONS))
    section = None
    contents = {s: [] for s in SECTIONS}
    for line in lines[1:]:
        if not line.strip():
            continue
        if line[:-1] in SECTIONS and line.endswith('：'):
            section = line[:-1]
        elif section and (line == '无' or (line.startswith('- ') and line[2:].strip())):
            contents[section].append(line)
        else:
            raise ValueError('未知正文或空条目：' + line)
    if any(not v or ('无' in v and len(v) != 1) for v in contents.values()):
        raise ValueError('每节写条目或单独写无')
    return body.strip() + '\n'

def parse(raw):
    text = raw.decode('utf-8-sig')
    if text.startswith('会话：'):
        header, body = text.split('\n\n', 1)
        session, updated = header.splitlines()
        return {'struct': 3, 'session': session.removeprefix('会话：'),
                'updated': updated.removeprefix('更新：'), 'body': validate_body(body)}
    if '接力结构：2' in text and '<!-- handoff:end -->' in text:
        return {'struct': 2, 'session': '', 'body': text}
    raise ValueError('接力格式损坏或不支持')

def events():
    if not EVENTS.exists():
        return []
    rows = [json.loads(line) for line in EVENTS.read_text(encoding='utf-8').splitlines() if line.strip()]
    for row in rows:
        if row.get('snapshot'):
            p = (ROOT / row['snapshot']).resolve()
            if not p.is_relative_to(STORE.resolve()) or not p.is_file():
                raise ValueError('交接快照不存在或越界')
            if digest(p.read_bytes()) != row['snapshot_sha256']:
                raise ValueError('交接快照哈希不符')
    return rows

def render_history(rows):
    text = '# 历史索引\n\n只记录阶段变化与证据，不复制累计接力。日常接手读取01；仅追溯时进入以下记录。\n\n'
    text += '## 机制迁移前\n\n- [旧接力去重记录](history/20261030-旧接力去重记录.md)：不同原文各保留一次。\n- [退休操作细节](history/20261030-旧操作细节.md)：原工具参数和专题过程，仅按需追溯。\n- [退休技术文档](history/旧技术文档.md)：原旧文档存档。\n- [结构2最后状态](history/legacy-03cc86df4442e014f846531d3c5a52950cf9ceac937f99e93b01bbfe1cf03458.txt)：迁移核对依据，已退休。\n\n## 阶段记录\n\n'
    for row in reversed(rows):
        text += '- ' + row['time'] + '：' + row['summary']
        if row.get('snapshot'):
            text += ' [交接快照](' + Path(row['snapshot']).relative_to('docs').as_posix() + ')'
        text += '\n'
    return text

def inspect():
    out = {'exists': TARGET.exists(), 'sha256': digest(TARGET.read_bytes()) if TARGET.exists() else MISSING}
    try:
        rec = parse(TARGET.read_bytes()) if TARGET.exists() else {}
        out.update(parse_ok=True, struct=rec.get('struct'), session=rec.get('session'))
    except (ValueError, UnicodeError) as e:
        out.update(parse_ok=False, problem=str(e))
    try:
        rows = events()
        out.update(history_ok=True, archived=any(x.get('snapshot_sha256') == out['sha256'] for x in rows))
    except (ValueError, OSError) as e:
        out.update(history_ok=False, history_problem=str(e), archived=False)
    return out

def submit(args):
    body = validate_body(Path(args.note_file).read_text(encoding='utf-8-sig'))
    if not args.session or '\n' in args.session or '\r' in args.session:
        raise ValueError('会话ID不能为空或包含换行')
    if args.handoff and not args.event:
        raise ValueError('交接须用--event填写本阶段变化摘要')
    if args.event and (len(args.event) > 400 or '\n' in args.event or '\r' in args.event):
        raise ValueError('阶段摘要限单行400字；具体记录引用实际产物')
    def prepare():
        raw = TARGET.read_bytes() if TARGET.exists() else None
        if (digest(raw) if raw is not None else MISSING) != args.expected_sha256:
            raise ValueError('基准哈希不符；重读01并合并，不能只换哈希强写')
        old = parse(raw) if raw is not None else None
        if old and old['struct'] == 2 and not args.migrate_legacy:
            raise ValueError('首次结构2转3须显式--migrate-legacy')
        if args.migrate_legacy and (not old or old['struct'] != 2):
            raise ValueError('只能迁移结构2')
        rows = events()
        return raw, old, rows
    if args.dry_run:
        prepare()
        return {'dry_run': True, 'ok': True}
    with lock(ROOT / 'tools/.handoff.lock'):
        raw, old, rows = prepare()
        if old and old['struct'] == 2:
            STORE.mkdir(parents=True, exist_ok=True)
            legacy = STORE / ('legacy-' + digest(raw) + '.txt')
            if not legacy.exists():
                atomic_text(legacy, raw.decode('utf-8-sig'))
        same = old and old['struct'] == 3 and old['session'] == args.session and old['body'] == body
        text = raw.decode('utf-8-sig') if same else '会话：' + args.session + '\n更新：' + now() + '\n\n' + body
        atomic_text(TARGET, text)
        final = digest(TARGET.read_bytes())
        if args.event:
            key = digest((final + args.event + str(args.handoff)).encode('utf-8'))
            if not any(x['id'] == key for x in rows):
                row = {'id': key, 'time': now(), 'summary': args.event}
                if args.handoff:
                    snap = STORE / 'snapshots' / (final + '.txt')
                    snap.parent.mkdir(parents=True, exist_ok=True)
                    if not snap.exists():
                        atomic_text(snap, text)
                    row.update(snapshot=snap.relative_to(ROOT).as_posix(), snapshot_sha256=final)
                rows.append(row)
                atomic_text(EVENTS, ''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in rows))
            atomic_text(HISTORY, render_history(rows))
        back = inspect()
        if not back['parse_ok'] or not back['history_ok'] or back['sha256'] != final or (args.handoff and not back['archived']):
            raise ValueError('读回/归档不符；01可能已保存，重读后以同正文重试补档')
        return {'final_sha256': final, 'handoff_archived': back['archived'], 'idempotent': bool(same)}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inspect', action='store_true')
    p.add_argument('--session'); p.add_argument('--note-file'); p.add_argument('--expected-sha256')
    p.add_argument('--dry-run', action='store_true'); p.add_argument('--handoff', action='store_true')
    p.add_argument('--migrate-legacy', action='store_true'); p.add_argument('--event')
    args = p.parse_args()
    try:
        if args.inspect:
            result = inspect()
        else:
            if not args.note_file or args.expected_sha256 is None:
                raise ValueError('须提供note-file、session与expected-sha256')
            result = submit(args)
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError) as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0
if __name__ == '__main__':
    sys.exit(main())
