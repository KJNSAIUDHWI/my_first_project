# -*- coding: utf-8 -*-
"""结构3接力回归：只写D:/temp带codex时间目录下的独立假项目。"""
import argparse
import importlib.util
import json
import re
import shutil
from pathlib import Path
from types import SimpleNamespace
ROOT = Path(__file__).resolve().parents[1]

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--workdir', required=True)
    args = ap.parse_args()
    work = Path(args.workdir).resolve()
    rel = work.relative_to(Path('D:/temp').resolve())
    if not rel.parts or not re.fullmatch(r'\d{4}-\d{2}-\d{2}-\d{2}-\d{2}_codex_.+', rel.parts[0]) or work == ROOT or ROOT in work.parents:
        raise ValueError('须使用独立的D:/temp/*_codex_*目录')
    fixture = work / 'fixture'
    if fixture.exists():
        raise ValueError('验收目录已存在；使用新目录，不覆盖现场')
    for rel in ('tools/写快照.py', 'pipeline/common/records.py'):
        dest = fixture / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    spec = importlib.util.spec_from_file_location('snapshot_test', fixture / 'tools/写快照.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    note = work / 'note.txt'
    body = '接力结构：3\n\n当前状态：\n- 测试状态\n\n本轮限制：\n- 接口停止\n\n待办与阻塞：\n- 待比较\n\n证据入口：\n- train/example\n'
    note.write_text(body, encoding='utf-8')
    results = []
    def check(label, condition):
        assert condition, label
        results.append(label)
    def call(**kw):
        options = dict(session='test', note_file=str(note), expected_sha256=m.inspect()['sha256'], dry_run=False, handoff=False, migrate_legacy=False, event=None)
        options.update(kw)
        return m.submit(SimpleNamespace(**options))
    def rejects(label, **kw):
        before = m.TARGET.read_bytes() if m.TARGET.exists() else None
        try:
            call(**kw)
        except (ValueError, OSError):
            check(label, (m.TARGET.read_bytes() if m.TARGET.exists() else None) == before)
        else:
            raise AssertionError(label)
    call(dry_run=True)
    check('dry_run_no_files', not m.TARGET.exists() and not m.EVENTS.exists())
    call()
    check('ordinary_write_no_archive', m.inspect()['parse_ok'] and not m.EVENTS.exists())
    rejects('stale_hash_rejected', expected_sha256='bad')
    rejects('handoff_needs_summary', handoff=True)
    rejects('empty_session_rejected', session='')
    call(handoff=True, event='阶段结果；证据train/example')
    first = m.TARGET.read_bytes(); count = len(m.events())
    check('handoff_verified', m.inspect()['archived'])
    call(handoff=True, event='阶段结果；证据train/example')
    check('retry_idempotent', m.TARGET.read_bytes() == first and len(m.events()) == count)
    note.write_text(body.replace('- 测试状态', '- 新状态'), encoding='utf-8')
    call(session='next')
    check('new_session_no_full_archive', len(m.events()) == count and not m.inspect()['archived'])
    note.write_text(body.replace('待办与阻塞：', '错误节：'), encoding='utf-8')
    rejects('malformed_body_rejected')
    note.write_text(body, encoding='utf-8')
    # Fail the index write after 01 and event were saved, then retry to repair it.
    original = m.atomic_text
    def fail_index(path, text):
        if path == m.HISTORY:
            raise OSError('injected index failure')
        return original(path, text)
    m.atomic_text = fail_index
    try:
        call(handoff=True, event='失败后补档')
    except OSError:
        pass
    else:
        raise AssertionError('failure injection')
    m.atomic_text = original
    n = len(m.events()); call(handoff=True, event='失败后补档')
    check('partial_failure_repaired_without_duplicate', m.inspect()['archived'] and len(m.events()) == n and '失败后补档' in m.HISTORY.read_text(encoding='utf-8'))
    row = m.events()[-1]; snap = fixture / row['snapshot']; raw = snap.read_bytes()
    snap.write_text('corrupt', encoding='utf-8')
    check('corrupt_snapshot_detected', not m.inspect()['history_ok'])
    rejects('corrupt_history_blocks_write')
    snap.write_bytes(raw)
    # Migration preserves the entire old file and switches to the current schema.
    legacy = '旧页眉\n接力结构：2\n旧正文\n<!-- handoff:end -->\n'
    m.TARGET.write_text(legacy, encoding='utf-8')
    rejects('migration_requires_flag')
    call(migrate_legacy=True, event='迁移')
    check('legacy_preserved', any(p.read_text(encoding='utf-8') == legacy for p in m.STORE.glob('legacy-*.txt')) and m.inspect()['struct'] == 3)
    (work / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': len(results), 'checks': results}, ensure_ascii=False))

if __name__ == '__main__':
    main()
