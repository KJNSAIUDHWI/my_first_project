"""印刷题号解析边界；不加载模型，不读写正式数据。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cnn.printed_number import parse_number


def test_parse_number():
    for text, expected in [('20.', 20), ('（２１）', 21), ('23', 23), (' 1 3 。', 13),
                           ('①', 1), ('20(10分)', 0), ('I3', 0), ('2A3', 0),
                           ('0', 0), ('61', 0), ('', 0), ('(2)', 2)]:
        assert parse_number(text) == expected, (text, expected)


if __name__ == '__main__':
    test_parse_number()
    print('printed number parser passed')
