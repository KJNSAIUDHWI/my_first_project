"""训练监控只读回归；假日志放调用者指定的D盘临时目录，不接触训练数据。"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('training_monitor', ROOT / 'tools/训练监控/serve.py')
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.run = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_latest_progress_accepts_minutes_and_slow_iterations(self):
        for speed, elapsed, remain, seconds in [
            ('4.6it/s', '24.4s', '1:13', 73),
            ('2.5s/it', '1:02:03', '2:03', 123),
            ('4.6it/s', '60.0s', '36.4s', 36.4),
            ('4.6it/s', '24.4s', '800ms', .8),
        ]:
            (self.run / 'train.log').write_text(
                '4/80 3.3G 0.4 1024: 60% 285/452 4.5it/s 60.0s<36.4s\n'
                f'\x1b[K 5/80 3.3G 0.4 1024: 25% 117/452 {speed} {elapsed}<{remain}\n', encoding='utf-8')
            actual = monitor.scan_live(self.run)
            self.assertEqual((actual['epoch'], actual['step'], actual['remain']), (5, 117, seconds))

    def test_traceback_warning_and_structured_failure_are_preserved(self):
        (self.run / 'train.log').write_text(
            'Normal training: loss=0.4\nUserWarning: sample warning\n'
            'Traceback (most recent call last):\n  File "train.py", line 12\n'
            '\x1b[31mRuntimeError: CUDA out of memory\x1b[0m\n', encoding='utf-8')
        failure = {'epoch': 2, 'where': 'gradient', 'files': ['sample.jpg'], 'details': 'NaN/Inf'}
        (self.run / 'numerical_failure.jsonl').write_text(json.dumps(failure) + '\n', encoding='utf-8')
        (self.run / 'amp_overflow.jsonl').write_text('{"epoch":1}\n', encoding='utf-8')
        actual = monitor.scan_errors(self.run)
        self.assertEqual((actual['errors'], actual['warnings']), (2, 1))
        trace = actual['items'][1]
        self.assertEqual(trace['line'], 5)
        self.assertIn('Traceback', trace['detail'])
        self.assertNotIn('\x1b', trace['detail'])
        self.assertIn('sample.jpg', actual['items'][-1]['detail'])

    def test_empty_log_incomplete_trace_and_capture_limit(self):
        self.assertEqual(monitor.scan_errors(self.run)['items'], [])
        (self.run / 'train.err.log').write_text('Traceback (most recent call last):\n frame\n', encoding='utf-8')
        self.assertEqual(monitor.scan_errors(self.run)['errors'], 1)
        (self.run / 'train.err.log').write_text('ValueError: bad\n' * 45, encoding='utf-8')
        actual = monitor.scan_errors(self.run)
        self.assertEqual((actual['errors'], len(actual['items'])), (45, 40))
        # 截断重写后重新读取，不能保留上一份文件里的幽灵错误。
        (self.run / 'train.err.log').write_text('Training normally\n', encoding='utf-8')
        self.assertEqual(monitor.scan_errors(self.run)['errors'], 0)


if __name__ == '__main__':
    SCRATCH = Path(sys.argv.pop(1)).resolve()
    if not SCRATCH.is_relative_to(Path('D:/temp').resolve()) or 'codex' not in str(SCRATCH):
        raise ValueError('测试目录须位于D:/temp带codex的目录')
    SCRATCH.mkdir(exist_ok=True, parents=True)
    unittest.main()
