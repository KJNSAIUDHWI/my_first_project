"""印刷题号解析边界测试；不加载模型，不读写正式数据。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from engine.printed_number_ocr import parse_number


class PrintedNumberOcrTests(unittest.TestCase):
    def test_parse_number(self):
        for text, expected in [('20.', 20), ('（２１）', 21), ('23', 23), (' 1 3 。', 13),
                               ('①', 1), ('20(10分)', 0), ('I3', 0), ('2A3', 0),
                               ('0', 0), ('61', 0), ('', 0), ('(2)', 2)]:
            self.assertEqual(parse_number(text), expected, (text, expected))


if __name__ == '__main__':
    unittest.main()
