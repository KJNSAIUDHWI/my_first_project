"""Regression checks for CNN review admission and review-service image binding."""
import csv
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_WORKDIR = None
sys.path.insert(0, str(PROJECT_ROOT / 'pipeline'))

from common import fixed_dataset, paths  # noqa: E402
from common import cnn_data  # noqa: E402
from common.choice_events import project  # noqa: E402
from common.records import sha  # noqa: E402


DIRECTORY_GUARD = None


def _codex_run_root(path):
    if DIRECTORY_GUARD is None:
        raise RuntimeError('目录guard尚未初始化')
    resolved = Path(path).resolve()
    temp_root = Path(DIRECTORY_GUARD.TEMP_ROOT).resolve()
    try:
        relative = resolved.relative_to(temp_root)
    except ValueError as exc:
        raise ValueError('项目副本和fixture都必须位于 D:/temp') from exc
    if not relative.parts or not DIRECTORY_GUARD.CODEX_DIR_RE.fullmatch(relative.parts[0]):
        raise ValueError('路径必须位于带日期的 D:/temp/*_codex_* 本轮目录内')
    return temp_root / relative.parts[0]


def _load_directory_guard():
    path = PROJECT_ROOT / 'tools' / '验证交接与准入.py'
    if not path.is_file():
        raise RuntimeError('副本缺少 tools/验证交接与准入.py 目录guard')
    spec = importlib.util.spec_from_file_location('cnn_admission_directory_guard', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_test_paths(workdir):
    global DIRECTORY_GUARD
    DIRECTORY_GUARD = _load_directory_guard()
    work = DIRECTORY_GUARD.validate_workdir(workdir)
    project = Path(paths.ROOT).resolve()
    if project != PROJECT_ROOT.resolve():
        raise ValueError('common.paths.ROOT 未指向当前测试副本')
    if _codex_run_root(project) != _codex_run_root(work):
        raise ValueError('PROJECT_ROOT 与 --workdir 必须位于本轮同一个 D:/temp 日期_codex_目录')
    if DIRECTORY_GUARD._overlaps(work, project):
        raise ValueError('--workdir 不得与 PROJECT_ROOT 副本重叠')
    work.mkdir(parents=True, exist_ok=True)
    return work


def _png(r, g, b):
    def chunk(name, payload):
        body = name + payload
        return struct.pack('>I', len(payload)) + body + struct.pack('>I', zlib.crc32(body) & 0xffffffff)

    raw = b'\x00' + bytes((r, g, b, 255))
    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>2I5B', 1, 1, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw))
            + chunk(b'IEND', b''))


def _write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_events(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events), encoding='utf-8')


def _load_service(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Request:
    def __init__(self, path, body):
        raw = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.path = path
        self.headers = {'Content-Length': str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.response = None

    def _send(self, code, body, *args, **kwargs):
        if isinstance(body, (bytes, bytearray)):
            body = body.decode('utf-8')
        self.response = json.loads(body) if isinstance(body, str) else body
        return self.response


class CNNAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(TEST_WORKDIR, '必须经 --workdir 设置经过guard验证的目录')
        self.assertEqual(_codex_run_root(paths.ROOT), _codex_run_root(TEST_WORKDIR),
                         'common.paths.ROOT 与 fixture 必须位于同一轮 D:/temp codex 副本')
        self._new_fixture_root(TEST_WORKDIR)
        self._patch_data_paths()
        self.digit_server = _load_service(PROJECT_ROOT / 'tools' / '数字审核' / 'serve.py',
                                          'cnn_test_digit_serve')
        self.choice_server = _load_service(PROJECT_ROOT / 'tools' / '涂黑审核' / 'serve.py',
                                           'cnn_test_choice_serve')

    def _new_fixture_root(self, parent=None):
        parent = Path(parent or TEST_WORKDIR or '')
        if not str(parent) or str(parent) == '.':
            raise RuntimeError('CNN fixtures 必须位于经过guard验证的 --workdir')
        parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='cnn-admission-', dir=parent))
        self.data = self.root / 'data'
        cnn_data.DATA = self.data
        fixed_dataset._PLAN_CACHE.clear()

    def _patch_data_paths(self):
        patch_data = mock.patch.object(cnn_data, 'DATA', self.data)
        patch_m_dir = mock.patch.object(fixed_dataset, 'm_dir', lambda name: self.data / name)
        patch_data.start()
        patch_m_dir.start()
        self.addCleanup(patch_data.stop)
        self.addCleanup(patch_m_dir.stop)

    def _plans(self, rows):
        plan_rows = [{'new_name': name, 'split': split, 'group': group, 'stage': '1'}
                     for name, split, group in rows]
        for name in ('m2', 'm1'):
            _write_csv(self.data / name / 'splits.csv', plan_rows,
                       ['new_name', 'split', 'group', 'stage'])
        fixed_dataset._PLAN_CACHE.clear()

    def _digit_data(self, stems=(('A', 'train'), ('B', 'val')), bind=True):
        self._plans([(stem, split, stem + '_group') for stem, split in stems])
        base = self.data / 'digit'
        labels, events = [], []
        for n, (stem, split) in enumerate(stems):
            file = stem.lower() + '.png'
            path = base / 'images' / split / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_png(20 + n, 80, 120))
            labels.append({'file': file, 'label': '2', 'split': split, 'stem': stem})
            ev = {'e': 'review', 'file': file, 'label': 2}
            if bind:
                ev['image_sha256'] = sha(path)
            events.append(ev)
        _write_csv(base / 'labels.csv', labels, ['file', 'label', 'split', 'stem'])
        _write_events(base / 'review.jsonl', events)
        return base

    def _choice_data(self, stems=(('A', 'train'), ('B', 'val')), event_kind='review', bind=True):
        self._plans([(stem, split, stem + '_group') for stem, split in stems])
        base = self.data / 'choice'
        manifest = []
        events_by_stem = {}
        for n, (stem, split) in enumerate(stems):
            manifest.append({'stem': stem + '.选择题区', 'row': 1, 'no': 1, 'n_opts': 4})
            file = stem + '.选择题区_001.png'
            path = base / 'images' / file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_png(30 + n, 110, 90))
            event = {'e': event_kind if stem == stems[-1][0] else 'review',
                     'row': 1, 'answer': 'A'}
            if event['e'] == 'special':
                event['note'] = '改过答案'
            if bind:
                event['image_sha256'] = sha(path)
            events_by_stem[stem + '.选择题区'] = [event]
        _write_csv(base / 'manifest.csv', manifest, ['stem', 'row', 'no', 'n_opts'])
        for stem, events in events_by_stem.items():
            _write_events(base / 'log' / (stem + '.jsonl'), events)
        return base

    def _digit_api(self, base):
        module = self.digit_server
        module.DATA = str(base)
        module.CSV_PATH = str(base / 'labels.csv')
        module.load()
        return module

    def _choice_api(self, base):
        module = self.choice_server
        module.DATA = str(base)
        module.IMAGES = str(base / 'images')
        module.LOGDIR = str(base / 'log')
        module.MANIFEST = str(base / 'manifest.csv')
        module.SUBSET = set()
        module.ONLY_UNREVIEWED = False
        module.load_manifest()
        module.rebuild()
        return module

    def test_digit_admission_and_frozen_bytes_use_direct_hashes(self):
        base = self._digit_data()
        rows, inferred = cnn_data.collect('digit')
        self.assertEqual(inferred, [])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r['image_sha256'] == sha(r['source']) for r in rows))
        output = self.root / 'snapshot'
        cnn_data.freeze('digit', output)
        self.assertEqual(len(cnn_data.load_frozen(output, 'digit')), 2)

        frozen = output / 'images' / 'digit__a.png'
        old = frozen.stat()
        frozen.write_bytes(_png(250, 80, 120))
        self.assertEqual(frozen.stat().st_size, old.st_size)
        os.utime(frozen, ns=(old.st_atime_ns, old.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, '快照图像已变化'):
            cnn_data.load_frozen(output, 'digit')

    def test_choice_missing_or_replaced_image_binding_is_rejected(self):
        base = self._choice_data(bind=False)
        with self.assertRaisesRegex(ValueError, '缺少图片绑定'):
            cnn_data.collect('choice')

        # Fresh fixture with valid bindings, then replace bytes while restoring size and mtime.
        self._new_fixture_root(os.environ.get('CNN_TEST_DIR') or None)
        self._plans([('A', 'train', 'A_group'), ('B', 'val', 'B_group')])
        base = self._choice_data(bind=True)
        path = base / 'images' / 'A.选择题区_001.png'
        prior = path.stat()
        path.write_bytes(_png(240, 110, 90))
        self.assertEqual(path.stat().st_size, prior.st_size)
        os.utime(path, ns=(prior.st_atime_ns, prior.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, '审核后图片已变化'):
            cnn_data.collect('choice')

    def test_choice_valid_exclude_and_defer_need_no_image_binding_but_freeze_tracks_them(self):
        base = self._choice_data()
        manifest_path = base / 'manifest.csv'
        with manifest_path.open('r', encoding='utf-8', newline='') as f:
            rows = list(csv.DictReader(f))
        for stem in ('C', 'D'):
            rows.append({'stem': stem + '.选择题区', 'row': 1, 'no': 1, 'n_opts': 4})
        _write_csv(manifest_path, rows, ['stem', 'row', 'no', 'n_opts'])

        decisions = {}
        for stem, action, status in (
                ('C', 'exclude', 'confirmed'), ('D', 'defer', 'pending')):
            sample = stem + '.选择题区_001.png'
            log = base / 'log' / (stem + '.选择题区.jsonl')
            _write_events(log, [{'e': 'special', 'row': 1, 'answer': 'A',
                                 'note': '改过答案'}])
            decisions[sample] = {
                'action': action,
                'reason': '用户决定排除' if action == 'exclude' else '等待后续确认',
                'basis': '无需纳入训练' if action == 'exclude' else '',
                'status': status,
                'log_sha256': sha(log),
            }
            # These non-admitted rows deliberately have neither image files nor hashes.
        decision_path = base / 'special_decisions.json'
        decision_path.write_text(json.dumps(decisions, ensure_ascii=False), encoding='utf-8')

        selected, _ = cnn_data.collect('choice')
        self.assertEqual({r['file'] for r in selected}, {
            'A.选择题区_001.png', 'B.选择题区_001.png'})

        output = self.root / 'excluded-evidence-snapshot'
        original_collect = cnn_data.collect

        def collect_then_change(*args, **kwargs):
            result = original_collect(*args, **kwargs)
            decisions['C.选择题区_001.png']['reason'] = '排除决定后来变化'
            decision_path.write_text(json.dumps(decisions, ensure_ascii=False), encoding='utf-8')
            return result

        with mock.patch.object(cnn_data, 'collect', side_effect=collect_then_change):
            with self.assertRaisesRegex(ValueError, '准入依据在审核后变化'):
                cnn_data.freeze('choice', output)
        self.assertFalse(output.exists(), 'changed excluded-row evidence must block snapshot creation')

    def test_digit_missing_binding_and_same_size_mtime_replacement_fail(self):
        base = self._digit_data(bind=False)
        with self.assertRaisesRegex(ValueError, '缺图片绑定'):
            cnn_data.collect('digit')

        self._new_fixture_root(os.environ.get('CNN_TEST_DIR') or None)
        self._plans([('A', 'train', 'A_group'), ('B', 'val', 'B_group')])
        base = self._digit_data(bind=True)
        path = base / 'images' / 'train' / 'a.png'
        prior = path.stat()
        path.write_bytes(_png(230, 80, 120))
        self.assertEqual(path.stat().st_size, prior.st_size)
        os.utime(path, ns=(prior.st_atime_ns, prior.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, '尚未确认或审核后变化'):
            cnn_data.collect('digit')

    def test_freeze_rejects_image_change_during_copy_and_cleans_partial_output(self):
        self._digit_data()
        output = self.root / 'half-snapshot'
        original_copy = cnn_data.shutil.copy2

        def copy_then_change(src, dst):
            result = original_copy(src, dst)
            if Path(src).name == 'a.png':
                stat = Path(src).stat()
                Path(src).write_bytes(_png(220, 80, 120))
                os.utime(src, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            return result

        with mock.patch.object(cnn_data.shutil, 'copy2', side_effect=copy_then_change):
            with self.assertRaisesRegex(ValueError, '复制期间源图片变化'):
                cnn_data.freeze('digit', output)
        self.assertFalse(output.exists(), 'failed freeze must remove its partial snapshot')

    def test_freeze_rejects_changed_review_or_label_evidence_after_admission(self):
        base = self._digit_data()
        original_collect = cnn_data.collect
        output = self.root / 'changed-evidence'

        def collect_then_change(task, allow_inferred=False, persist_cache=True):
            result = original_collect(task, allow_inferred, persist_cache)
            with (base / 'review.jsonl').open('a', encoding='utf-8') as f:
                f.write('\n')
            return result

        with mock.patch.object(cnn_data, 'collect', side_effect=collect_then_change):
            with self.assertRaisesRegex(ValueError, '准入依据在审核后变化'):
                cnn_data.freeze('digit', output)
        self.assertFalse(output.exists())

    def test_unknown_parent_does_not_fall_back_to_csv_split(self):
        self._plans([('B', 'val', 'B_group')])
        base = self.data / 'digit'
        path = base / 'images' / 'train' / 'unknown.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_png(30, 40, 50))
        _write_csv(base / 'labels.csv', [
            {'file': 'unknown.png', 'label': '2', 'split': 'train', 'stem': 'UNKNOWN'},
            {'file': 'b.png', 'label': '2', 'split': 'val', 'stem': 'B'},
        ], ['file', 'label', 'split', 'stem'])
        _write_events(base / 'review.jsonl', [
            {'e': 'review', 'file': 'unknown.png', 'label': 2, 'image_sha256': sha(path)},
        ])
        with self.assertRaisesRegex(ValueError, '缺少父页分区/身份：UNKNOWN'):
            cnn_data.collect('digit')

    def test_choice_project_preserves_only_valid_review_bindings(self):
        digest = 'a' * 64
        manifest = [{'stem': 'A', 'row': '1', 'no': '1', 'n_opts': '4'}]
        projected = project('A', manifest, [
            {'e': 'review', 'row': 1, 'answer': 'A', 'image_sha256': digest},
            {'e': 'save', 'row': 1, 'answer': 'A', 'note': ''},
        ])[0]
        self.assertTrue(projected['reviewed'])
        self.assertEqual(projected['image_sha256'], digest)

        for events in ([{'e': 'save', 'row': 1, 'answer': 'B', 'note': ''}],
                       [{'e': 'unreview', 'row': 1}],
                       [{'e': 'pred', 'rows': [{'row': 1, 'answer': 'B'}]}]):
            row = project('A', manifest, [
                {'e': 'review', 'row': 1, 'answer': 'A', 'image_sha256': digest}, *events
            ])[0]
            self.assertFalse(row['reviewed'])
            self.assertIsNone(row['image_sha256'])

        legacy = project('A', manifest, [{'e': 'review', 'row': 1, 'answer': 'A'}])[0]
        self.assertTrue(legacy['reviewed'])
        self.assertIsNone(legacy['image_sha256'])

    def test_choice_review_special_and_batch_apply_require_displayed_hashes(self):
        stems = [('A', 'train'), ('B', 'val'), ('C', 'train')]
        self._plans([(s, split, s + '_group') for s, split in stems])
        base = self._choice_data(stems=stems, event_kind='review')
        module = self._choice_api(base)

        # Direct ordinary confirmation binds exactly the bytes the browser displayed.
        a_path = base / 'images' / 'A.选择题区_001.png'
        a_hash = sha(a_path)
        req = _Request('/api/review', {'stem': 'A.选择题区', 'row': 1, 'answer': 'A',
                                      'note': '', 'displayed_sha256': a_hash})
        module.H.do_POST(req)
        self.assertEqual(req.response['row']['image_sha256'], a_hash)

        # A special confirmation follows the same binding path.
        b_path = base / 'images' / 'B.选择题区_001.png'
        b_hash = sha(b_path)
        req = _Request('/api/review', {'stem': 'B.选择题区', 'row': 1, 'answer': 'A',
                                      'note': '改过答案', 'displayed_sha256': b_hash})
        module.H.do_POST(req)
        b_log = base / 'log' / 'B.选择题区.jsonl'
        special_event = json.loads(b_log.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(special_event['e'], 'special')
        self.assertEqual(special_event['image_sha256'], b_hash)

        # Replacing the image after display, even with the same size and restored mtime, rejects review.
        old_stat = b_path.stat()
        b_path.write_bytes(_png(240, 110, 90))
        self.assertEqual(b_path.stat().st_size, old_stat.st_size)
        os.utime(b_path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        before = b_log.read_bytes()
        stale = _Request('/api/review', {'stem': 'B.选择题区', 'row': 1, 'answer': 'A',
                                        'note': '改过答案', 'displayed_sha256': b_hash})
        module.H.do_POST(stale)
        self.assertIn('已变化', stale.response['err'])
        self.assertEqual(b_log.read_bytes(), before)
        b_path.write_bytes(_png(31, 110, 90))
        os.utime(b_path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))

        # A changed draft cannot be applied without every affected row's displayed hash.
        c_path = base / 'images' / 'C.选择题区_001.png'
        c_hash = sha(c_path)
        save = _Request('/api/set', {'stem': 'C.选择题区', 'row': 1, 'answer': 'B', 'note': ''})
        module.H.do_POST(save)
        c_log = base / 'log' / 'C.选择题区.jsonl'
        before = c_log.read_bytes()
        missing = _Request('/api/apply', {'answer': '', 'displayed_sha256': {}})
        module.H.do_POST(missing)
        self.assertIn('未成功显示', missing.response['err'])
        self.assertEqual(c_log.read_bytes(), before)

        apply = _Request('/api/apply', {
            'answer': '', 'displayed_sha256': {'C.选择题区|1': c_hash}
        })
        module.H.do_POST(apply)
        self.assertEqual(apply.response['applied'], 1)
        event = json.loads(c_log.read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(event['image_sha256'], c_hash)

        decision = {'B.选择题区_001.png': {
            'action': 'include', 'reason': '用户确认 changed 样本', 'basis': '用户明确确认',
            'status': 'confirmed', 'weight': 2.0, 'log_sha256': sha(b_log),
        }}
        (base / 'special_decisions.json').write_text(json.dumps(decision, ensure_ascii=False),
                                                    encoding='utf-8')
        rows, _ = cnn_data.collect('choice')
        self.assertEqual({r['file']: r['image_sha256'] for r in rows}[
            'A.选择题区_001.png'], a_hash)
        self.assertEqual({r['file']: r['weight'] for r in rows}[
            'B.选择题区_001.png'], 2.0)

    def test_special_decision_status_and_weight_rules_remain_strict(self):
        stems = [('A', 'train'), ('B', 'val')]
        base = self._choice_data(stems=stems, event_kind='special')
        log = base / 'log' / 'B.选择题区.jsonl'
        path = base / 'special_decisions.json'
        sample = 'B.选择题区_001.png'

        def decide(status, reason='用户确认 changed 样本', basis='用户明确确认', weight=2.0,
                   decided_by='user'):
            row = {'action': 'include', 'reason': reason, 'basis': basis, 'status': status,
                   'decided_by': decided_by, 'weight': weight, 'log_sha256': sha(log)}
            path.write_text(json.dumps({sample: row}, ensure_ascii=False), encoding='utf-8')

        decide('pending', basis='user confirmed')
        with self.assertRaisesRegex(ValueError, '当前 pending'):
            cnn_data.collect('choice')

        decide('rule', basis='现行规则')
        with self.assertRaisesRegex(ValueError, '当前 rule'):
            cnn_data.collect('choice')

        decide('confirmed', basis='待用户确认')
        with self.assertRaisesRegex(ValueError, '待用户确认'):
            cnn_data.collect('choice')

        decide('confirmed', basis='用户已确认，无需待确认')
        selected, _ = cnn_data.collect('choice')
        self.assertEqual({r['file']: r['weight'] for r in selected}[sample], 2.0)

        decide('confirmed', reason='   ')
        with self.assertRaisesRegex(ValueError, '缺 action/reason'):
            cnn_data.collect('choice')

        decide('confirmed', basis='   ')
        with self.assertRaisesRegex(ValueError, '非空依据'):
            cnn_data.collect('choice')

        for weight in (float('inf'), float('nan'), 1.0):
            decide('confirmed', weight=weight)
            with self.assertRaises(ValueError):
                cnn_data.collect('choice')

        decide('confirmed', weight=2.0)
        selected, _ = cnn_data.collect('choice')
        self.assertEqual({r['file']: r['weight'] for r in selected}[sample], 2.0)

        # A confirmed include remains blocked when its note has no supported kind.
        events = json.loads(log.read_text(encoding='utf-8').splitlines()[-1])
        events['note'] = '特殊情况'
        _write_events(log, [events])
        decide('confirmed', weight=2.0)
        with self.assertRaisesRegex(ValueError, '未知特殊类型'):
            cnn_data.collect('choice')

    def test_digit_same_label_reconfirmation_and_write_failures_are_reported(self):
        base = self.data / 'digit'
        path = base / 'images' / 'train' / 'same.png'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_png(20, 80, 120))
        _write_csv(base / 'labels.csv', [
            {'file': 'same.png', 'label': '2', 'split': 'train', 'stem': 'A'}
        ], ['file', 'label', 'split', 'stem'])
        module = self._digit_api(base)
        digest = sha(path)

        confirm = _Request('/set', {'i': 0, 'value': 2, 'displayed_sha256': digest})
        module.H.do_POST(confirm)
        self.assertTrue(confirm.response['ok'])
        event = json.loads((base / 'review.jsonl').read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(event['image_sha256'], digest)
        self.assertEqual(event['label'], 2)

        # Stale browser bytes fail even when the replacement keeps its old size and mtime.
        stat = path.stat()
        path.write_bytes(_png(240, 80, 120))
        self.assertEqual(path.stat().st_size, stat.st_size)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        stale = _Request('/set', {'i': 0, 'value': 2, 'displayed_sha256': digest})
        module.H.do_POST(stale)
        self.assertFalse(stale.response['ok'])
        self.assertIn('已变化', stale.response['err'])

        # A same-label review-log append failure must never report ok=true.
        path.write_bytes(_png(20, 80, 120))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        (base / 'review.jsonl').unlink()
        (base / 'review.jsonl').mkdir()
        failed = _Request('/set', {'i': 0, 'value': 2, 'displayed_sha256': digest})
        module.H.do_POST(failed)
        self.assertFalse(failed.response['ok'])
        self.assertIn('审核记录写入失败', failed.response['err'])
        self.assertTrue('Permission denied' in failed.response['err']
                        or 'IsADirectoryError' in failed.response['err'])

        out_of_range = _Request('/set', {'i': 0, 'value': 35, 'displayed_sha256': digest})
        module.H.do_POST(out_of_range)
        self.assertFalse(out_of_range.response['ok'])
        self.assertIn('0~34', out_of_range.response['err'])
        self.assertIn("确认本图", module.PAGE)
        self.assertIn("cache:'no-store'", module.PAGE)

    def test_digit_zero_label_is_accessible_and_can_be_reconfirmed(self):
        base = self.data / 'digit'
        zero = base / 'images' / 'train' / 'zero.png'
        two = base / 'images' / 'val' / 'two.png'
        zero.parent.mkdir(parents=True, exist_ok=True)
        two.parent.mkdir(parents=True, exist_ok=True)
        old_zero = _png(5, 80, 120)
        zero.write_bytes(_png(15, 80, 120))
        two.write_bytes(_png(25, 80, 120))
        _write_csv(base / 'labels.csv', [
            {'file': 'zero.png', 'label': '0', 'split': 'train', 'stem': 'A'},
            {'file': 'two.png', 'label': '2', 'split': 'val', 'stem': 'B'},
        ], ['file', 'label', 'split', 'stem'])
        _write_events(base / 'review.jsonl', [{
            'e': 'review', 'file': 'zero.png', 'label': 0,
            'image_sha256': hashlib.sha256(old_zero).hexdigest(),
        }])
        module = self._digit_api(base)
        self.assertIn(0, module.GROUPS)
        self.assertEqual(module.GROUPS[0], [0])

        req = _Request('/set', {'i': 0, 'value': 0, 'displayed_sha256': sha(zero)})
        module.H.do_POST(req)
        self.assertTrue(req.response['ok'])
        event = json.loads((base / 'review.jsonl').read_text(encoding='utf-8').splitlines()[-1])
        self.assertEqual(event['label'], 0)
        self.assertEqual(event['image_sha256'], sha(zero))

    def test_choice_page_browsing_never_reviews_and_explicit_confirm_sends_displayed_hash(self):
        module = self.choice_server
        match = re.search(r'<script>(.*?)</script>', module.PAGE, re.S)
        self.assertIsNotNone(match)
        node = shutil.which('node')
        self.assertIsNotNone(node, '该回归需要Node执行真实内联页面脚本')
        harness = r'''
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(0,'utf8'), displayHash='ab'.repeat(32), reviewCalls=[];
let row={stem:'A.选择题区',row:1,no:1,n_opts:4,file:'A.选择题区_001.png',answer:'A',draft:'A',pred_answer:'A',note:'',draft_note:'',reviewed:true,image_sha256:null};
class FakeElement{
  constructor(tag='div'){this.tagName=tag;this.children=[];this.dataset={};this.style={};this.className='';this.disabled=false;this.textContent='';this._innerHTML='';this._src='';this.parentNode=null;this.classList={add:n=>{if(!this.className.split(/\s+/).includes(n))this.className=(this.className+' '+n).trim();},remove:n=>{this.className=this.className.split(/\s+/).filter(x=>x!==n).join(' ');}};}
  appendChild(c){c.parentNode=this;this.children.push(c);return c;}
  set innerHTML(v){this._innerHTML=v;if(v==='')this.children=[];} get innerHTML(){return this._innerHTML;}
  set src(v){this._src=v;if(v)queueMicrotask(()=>{if(this.onload)this.onload();});} get src(){return this._src;}
  matches(s){if(s===':hover')return true;if(s==='.cell.cur')return this.className.split(/\s+/).includes('cur');return false;}
  closest(s){if(s==='.cell'&&this.className.split(/\s+/).includes('cell'))return this;return this.parentNode&&this.parentNode.closest?this.parentNode.closest(s):null;}
}
const ids={}; for(const id of ['groups','grid','end','bar','mhead','mimg','mok','mempty','mspec','mhint','mopts','modal','apply'])ids[id]=new FakeElement(id==='mimg'?'img':'div');
const document={body:{scrollHeight:1000},getElementById:id=>ids[id]||(ids[id]=new FakeElement()),createElement:tag=>new FakeElement(tag),addEventListener:()=>{},querySelectorAll:s=>s==='.cell'?ids.grid.children:s==='.cell.cur'?ids.grid.children.filter(x=>x.matches('.cell.cur')):[],querySelector:s=>{const m=s.match(/data-key="([^"]+)"/);return m?ids.grid.children.find(x=>x.dataset.key===m[1])||null:null;}};
class FakeIntersectionObserver{constructor(cb){this.callback=cb;this.targets=[];}observe(t){this.targets.push(t);}unobserve(t){this.targets=this.targets.filter(x=>x!==t);}}
const window={innerHeight:900,scrollY:0,addEventListener:()=>{},IntersectionObserver:FakeIntersectionObserver};
const crypto={subtle:{digest:async()=>Uint8Array.from({length:32},()=>0xab).buffer}};
const URL={createObjectURL:()=>`blob:${Math.random()}`,revokeObjectURL:()=>{}};
const response=data=>({ok:true,json:async()=>data,blob:async()=>({arrayBuffer:async()=>Uint8Array.from([1,2,3]).buffer})});
async function fetch(path,opts={}){
  if(path.startsWith('/api/tree'))return response({total:1,done:1,counts:{A:1},order:['A']});
  if(path.startsWith('/api/rows'))return response({total:1,rows:[row]});
  if(path.startsWith('/img'))return response({});
  if(path==='/api/review'){const body=JSON.parse(opts.body);reviewCalls.push(body);row={...row,answer:body.answer,draft:body.answer,reviewed:true,image_sha256:body.displayed_sha256};return response({row:{...row}});}
  throw new Error('unexpected fetch '+path);
}
const context={window,document,fetch,crypto,URL,IntersectionObserver:FakeIntersectionObserver,setTimeout,clearTimeout,console};
vm.runInNewContext(source,context);
const wait=async test=>{for(let i=0;i<100;i++){if(test())return;await new Promise(r=>setTimeout(r,5));}throw new Error('timeout waiting for page state');};
(async()=>{
  await wait(()=>ids.grid.children.length===1);
  const card=ids.grid.children[0], img=card.children[0], observer=window.INTERSECTION_OBSERVER;
  if(card.onmouseenter)card.onmouseenter();
  observer.callback([{isIntersecting:true,target:img}]);
  await wait(()=>card.dataset.imageLoadState==='loaded');
  if(card.onmouseenter)card.onmouseenter();
  await new Promise(r=>setTimeout(r,20));
  if(reviewCalls.length!==0)throw new Error('browse/load/hover posted review');
  const missing=card._bindingBadge.textContent;
  const stale=context.bindingStatusText({reviewed:true,image_sha256:'cd'.repeat(32)},displayHash,'loaded');
  const valid=context.bindingStatusText({reviewed:true,image_sha256:displayHash},displayHash,'loaded');
  if(!missing.includes('缺少图片绑定')||!stale.includes('绑定失效')||!valid.includes('绑定有效'))throw new Error('binding status not distinguished');
  card.onclick(); await wait(()=>ids.mok.disabled===false); ids.mok.onclick();
  await wait(()=>reviewCalls.length===1);
  if(reviewCalls[0].displayed_sha256!==displayHash)throw new Error('explicit review did not send displayed hash');
  await wait(()=>card._bindingBadge.textContent.includes('绑定有效'));
  process.stdout.write(JSON.stringify({browseReviewCalls:0,explicitHash:reviewCalls[0].displayed_sha256,missing,stale,valid}));
})().catch(e=>{console.error(e.stack||e);process.exitCode=1;});
'''
        result = subprocess.run([node, '-e', harness], input=match.group(1), text=True,
                                encoding='utf-8', capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report['browseReviewCalls'], 0)
        self.assertEqual(report['explicitHash'], 'ab' * 32)
        self.assertIn('缺少图片绑定', report['missing'])
        self.assertIn('绑定失效', report['stale'])
        self.assertIn('绑定有效', report['valid'])

    def test_choice_page_ignores_stale_image_and_review_responses_after_switch(self):
        module = self.choice_server
        match = re.search(r'<script>(.*?)</script>', module.PAGE, re.S)
        self.assertIsNotNone(match)
        node = shutil.which('node')
        self.assertIsNotNone(node, '该回归需要Node执行真实内联页面脚本')
        harness = r'''
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(0,'utf8'), hashA='aa'.repeat(32), hashB='bb'.repeat(32);
const rowA={stem:'A.选择题区',row:1,no:1,n_opts:4,file:'A.png',answer:'A',draft:'A',pred_answer:'A',note:'',draft_note:'',reviewed:false,image_sha256:null};
const rowB={stem:'B.选择题区',row:1,no:2,n_opts:4,file:'B.png',answer:'A',draft:'A',pred_answer:'A',note:'',draft_note:'',reviewed:false,image_sha256:null};
const reviewCalls=[],setCalls=[]; let resolveLateImageA=null,resolveSetA=null,resolveReviewA=null,delayFirstA=true;
class FakeElement{
  constructor(tag='div'){this.tagName=tag;this.children=[];this.dataset={};this.style={};this.className='';this.disabled=false;this.textContent='';this._innerHTML='';this._src='';this.parentNode=null;this.classList={add:n=>{if(!this.className.split(/\s+/).includes(n))this.className=(this.className+' '+n).trim();},remove:n=>{this.className=this.className.split(/\s+/).filter(x=>x!==n).join(' ');}};}
  appendChild(c){c.parentNode=this;this.children.push(c);return c;}
  set innerHTML(v){this._innerHTML=v;if(v==='')this.children=[];} get innerHTML(){return this._innerHTML;}
  set src(v){this._src=v;if(v)queueMicrotask(()=>{if(this.onload)this.onload();});} get src(){return this._src;}
  matches(s){if(s===':hover')return true;if(s==='.cell.cur')return this.className.split(/\s+/).includes('cur');return false;}
  closest(s){if(s==='.cell'&&this.className.split(/\s+/).includes('cell'))return this;return this.parentNode&&this.parentNode.closest?this.parentNode.closest(s):null;}
}
const ids={}; for(const id of ['groups','grid','end','bar','mhead','mimg','mok','mempty','mspec','mhint','mopts','modal','apply'])ids[id]=new FakeElement(id==='mimg'?'img':'div');
const document={body:{scrollHeight:1000},getElementById:id=>ids[id]||(ids[id]=new FakeElement()),createElement:tag=>new FakeElement(tag),addEventListener:()=>{},querySelectorAll:s=>s==='.cell'?ids.grid.children:s==='.cell.cur'?ids.grid.children.filter(x=>x.matches('.cell.cur')):[],querySelector:s=>{const m=s.match(/data-key="([^"]+)"/);return m?ids.grid.children.find(x=>x.dataset.key===m[1])||null:null;}};
class FakeIntersectionObserver{constructor(cb){this.callback=cb;this.targets=[];}observe(t){this.targets.push(t);}unobserve(t){this.targets=this.targets.filter(x=>x!==t);}}
const window={innerHeight:900,scrollY:0,addEventListener:()=>{},IntersectionObserver:FakeIntersectionObserver};
const crypto={subtle:{digest:async(_alg,bytes)=>{const v=new Uint8Array(bytes)[0];return Uint8Array.from({length:32},()=>v).buffer;}}};
const URL={createObjectURL:blob=>'blob:'+blob.name,revokeObjectURL:()=>{}};
const response=data=>({ok:true,json:async()=>data,blob:async()=>data});
function imageResponse(name){return {name,arrayBuffer:async()=>Uint8Array.from([name==='A.png'?0xaa:0xbb]).buffer};}
function reviewRow(body){const r=body.stem===rowA.stem?rowA:rowB;return {...r,answer:body.answer,draft:body.answer,reviewed:true,image_sha256:body.displayed_sha256};}
function fetch(path,opts={}){
  if(path.startsWith('/api/tree'))return Promise.resolve(response({total:2,done:0,counts:{A:2},order:['A']}));
  if(path.startsWith('/api/rows'))return Promise.resolve(response({total:2,rows:[rowA,rowB]}));
  if(path.startsWith('/img')){
    const name=new URLSearchParams(path.split('?')[1]).get('name');
    if(name==='A.png'&&delayFirstA){delayFirstA=false;return new Promise(resolve=>{resolveLateImageA=()=>resolve(response(imageResponse(name)));});}
    return Promise.resolve(response(imageResponse(name)));
  }
  if(path==='/api/set'){
    const body=JSON.parse(opts.body);setCalls.push(body);
    if(body.stem===rowA.stem)return new Promise(resolve=>{resolveSetA=()=>resolve(response({row:{...rowA}}));});
    return Promise.resolve(response({row:{...rowB}}));
  }
  if(path==='/api/review'){
    const body=JSON.parse(opts.body);reviewCalls.push(body);
    if(body.stem===rowA.stem)return new Promise(resolve=>{resolveReviewA=()=>resolve(response({row:reviewRow(body)}));});
    return Promise.resolve(response({row:reviewRow(body)}));
  }
  throw new Error('unexpected fetch '+path);
}
const context={window,document,fetch,crypto,URL,IntersectionObserver:FakeIntersectionObserver,setTimeout,clearTimeout,console,alert:()=>{},prompt:()=>null};
vm.runInNewContext(source,context);
const wait=async test=>{for(let i=0;i<300;i++){if(test())return;await new Promise(r=>setTimeout(r,5));}throw new Error('timeout waiting for page state');};
(async()=>{
  await wait(()=>ids.grid.children.length===2);
  const cardA=ids.grid.children.find(c=>c.dataset.key.startsWith(rowA.stem)), cardB=ids.grid.children.find(c=>c.dataset.key.startsWith(rowB.stem));
  // A's fetch is held; opening B must supersede it before it can replace the shared modal image.
  cardA.onclick(); await wait(()=>typeof resolveLateImageA==='function');
  cardB.onclick(); await wait(()=>ids.mok.disabled===false&&ids.mimg.dataset.displayedSha256===hashB);
  resolveLateImageA(); await new Promise(r=>setTimeout(r,20));
  if(ids.mimg.src!=='blob:B.png'||ids.mimg.dataset.displayedSha256!==hashB)throw new Error('late A image replaced B or left a mismatched fingerprint');
  ids.mcls.onclick();

  // A draft response arriving while B is open must mutate only row A and leave B's hint intact.
  cardA.onclick(); await wait(()=>ids.mok.disabled===false&&ids.mimg.dataset.displayedSha256===hashA);
  context.toggleAns('B'); await wait(()=>typeof resolveSetA==='function');
  cardB.onclick(); await wait(()=>ids.mok.disabled===false&&ids.mimg.dataset.displayedSha256===hashB);
  const hintBeforeSet=ids.mhint.textContent;
  resolveSetA(); await new Promise(r=>setTimeout(r,20));
  if(rowA.draft!=='AB'||rowB.draft!=='A')throw new Error('late A draft response mutated the wrong row');
  if(ids.modal.style.display!=='flex'||ids.mhead.textContent.indexOf(rowB.stem)<0||ids.mhint.textContent!==hintBeforeSet)throw new Error('late A draft response changed B modal state');
  ids.mcls.onclick();

  // A review response arriving during B's modal must update A only and keep B confirmable.
  cardA.onclick(); await wait(()=>ids.mok.disabled===false&&ids.mimg.dataset.displayedSha256===hashA);
  ids.mok.onclick(); await wait(()=>typeof resolveReviewA==='function');
  const aReview=reviewCalls.find(x=>x.stem===rowA.stem);
  if(!aReview||aReview.displayed_sha256!==hashA)throw new Error('A review did not capture its own image fingerprint');
  cardB.onclick(); await wait(()=>ids.mok.disabled===false&&ids.mimg.dataset.displayedSha256===hashB);
  const hintBeforeReview=ids.mhint.textContent;
  resolveReviewA(); await new Promise(r=>setTimeout(r,20));
  if(!rowA.reviewed||rowA.image_sha256!==hashA||rowB.reviewed)throw new Error('late A review response mutated the wrong row');
  if(ids.modal.style.display!=='flex'||ids.mhead.textContent.indexOf(rowB.stem)<0||ids.mhint.textContent!==hintBeforeReview)throw new Error('late A review response closed or changed B modal');
  ids.mok.onclick(); await wait(()=>reviewCalls.length===2&&ids.modal.style.display==='none');
  const bReview=reviewCalls.find(x=>x.stem===rowB.stem);
  if(!bReview||bReview.displayed_sha256!==hashB||!rowB.reviewed||rowB.image_sha256!==hashB)throw new Error('B confirm did not use B displayed fingerprint');
  process.stdout.write(JSON.stringify({lateImageIgnored:true,draftTarget:'A',reviewTarget:'A',bConfirmHash:bReview.displayed_sha256}));
})().catch(e=>{console.error(e.stack||e);process.exitCode=1;});
'''
        result = subprocess.run([node, '-e', harness], input=match.group(1), text=True,
                                encoding='utf-8', capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        report = json.loads(result.stdout)
        self.assertTrue(report['lateImageIgnored'])
        self.assertEqual(report['draftTarget'], 'A')
        self.assertEqual(report['reviewTarget'], 'A')
        self.assertEqual(report['bConfirmHash'], 'bb' * 32)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--workdir', required=True)
    options, remaining = parser.parse_known_args()
    TEST_WORKDIR = _validate_test_paths(options.workdir)
    os.environ['CNN_TEST_DIR'] = str(TEST_WORKDIR)
    unittest.main(argv=[sys.argv[0], *remaining])
