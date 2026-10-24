# -*- coding: utf-8 -*-
"""新数据流回归：日志准入、freeze 不复制图片、未审拒绝、卷组不跨分区。"""
import csv
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone, timedelta

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'pipeline'))
from common import cnn_data, fixed_dataset  # noqa: E402


class DataFlow(unittest.TestCase):
    def setUp(self):
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d-%H-%M')
        self.tmp = Path(tempfile.mkdtemp(prefix=stamp + '_codex_data_flow_', dir=r'D:\temp'))
        self.data = self.tmp / 'data'
        self.old_data, self.old_root = cnn_data.DATA, cnn_data.ROOT
        self.old_mdir = fixed_dataset.m_dir
        cnn_data.DATA = self.data
        cnn_data.ROOT = self.tmp
        fixed_dataset.m_dir = lambda m: self.data / m
        fixed_dataset._PLAN_CACHE.clear()

        (self.data / 'm2').mkdir(parents=True)
        self.write_splits('page1,train,1,g1,物理\npage2,val,1,g2,物理\n')

        for name in ('1', '2'):
            p = self.data / 'digit' / 'images' / (name + '.png')
            p.parent.mkdir(parents=True, exist_ok=True)
            Image.new('L', (64, 64), color=128).save(p)
        rows = [
            {'file': '1.png', 'label': '5', 'split': 'train', 'stem': 'page1'},
            {'file': '2.png', 'label': '3', 'split': 'val', 'stem': 'page2'},
        ]
        with open(self.data / 'digit' / 'labels.csv', 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['file', 'label', 'split', 'stem'])
            w.writeheader()
            w.writerows(rows)
        for name, label in (('1', 5), ('2', 3)):
            self.write_log(name, label)

    def tearDown(self):
        cnn_data.DATA, cnn_data.ROOT = self.old_data, self.old_root
        fixed_dataset.m_dir = self.old_mdir
        fixed_dataset._PLAN_CACHE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_splits(self, lines):
        p = self.data / 'm2' / 'splits.csv'
        p.write_text('new_name,split,stage,group,subject_original\n' + lines, encoding='utf-8')
        fixed_dataset._PLAN_CACHE.clear()

    def write_log(self, name, label):
        p = self.data / 'digit' / 'log' / (name + '.log')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"时间":"2026-10-24 20:00:00","事件":"旧记录","标签":%d,'
                     '"依据":"人工审核","分区":"train","说明":"测试"}\n' % label,
                     encoding='utf-8')

    def test_collect_and_freeze_without_copying_images(self):
        rows, inferred = cnn_data.collect('digit')
        self.assertEqual(len(rows), 2)
        self.assertEqual(inferred, [])
        out = self.tmp / 'snap'
        cnn_data.freeze('digit', out)
        self.assertEqual([p.name for p in out.iterdir()], ['selection.json'])
        self.assertFalse((out / 'images').exists())
        items = cnn_data.load_frozen(out, 'digit')
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]['g'].shape, (64, 64))

    def test_unreviewed_is_rejected(self):
        (self.data / 'digit' / 'log' / '1.log').write_text(
            '{"时间":"2026-10-24 20:00:00","事件":"预标注","标签":5,"说明":"测试"}\n',
            encoding='utf-8')
        with self.assertRaises(ValueError):
            cnn_data.collect('digit')

    def test_group_cross_split_is_rejected(self):
        self.write_splits('page1,train,1,g1,物理\npage2,val,1,g1,物理\n')
        with self.assertRaises(ValueError):
            cnn_data.collect('digit')


if __name__ == '__main__':
    unittest.main()
