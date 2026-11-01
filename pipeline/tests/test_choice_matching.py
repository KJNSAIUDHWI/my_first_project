"""题号配对、版式排序及保守纠正的回归；不加载训练权重。"""
import copy
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stages.match import match_nums_to_options
from stages.choice import crop_option_row, fix_no_sequence, _order_row, read_choice_zone


def rows(numbers):
    return [{"no": n, "choice_conf": .99, "digit_conf": .99,
             "num_box": [.05, .05 + i * .08, .025, .04],
             "opt_box": [.25, .05 + i * .08, .4, .04]}
            for i, n in enumerate(numbers)]


class Matching(unittest.TestCase):
    def test_unrelated_boxes_remain_unmatched(self):
        self.assertEqual(match_nums_to_options([(10, 10, 10, 10)], [(50, 500, 80, 15)]), [])
        self.assertEqual(match_nums_to_options([(10, 10, 10, 10)], [(500, 10, 80, 15)]), [])

    def test_missing_option_does_not_shift_later_rows(self):
        nums = [(10, y, 10, 10) for y in [10, 30, 50]]
        opts = [(50, y, 100, 10) for y in [10, 50]]
        self.assertEqual([(i, j) for i, j, _ in match_nums_to_options(nums, opts)], [(0, 0), (2, 1)])

    def test_two_columns_do_not_cross_pair(self):
        nums = [(10, 10, 10, 10), (210, 10, 10, 10)]
        opts = [(250, 10, 100, 10), (50, 10, 100, 10)]
        self.assertEqual([(i, j) for i, j, _ in match_nums_to_options(nums, opts)], [(0, 1), (1, 0)])

    def test_multiselect_label_gap_keeps_real_rows(self):
        nums = [(1482.601, 687.164, 43.558, 32.197),
                (1484.959, 739.328, 45.177, 32.108)]
        opts = [(1773.142, 687.160, 274.346, 49.757),
                (1780.486, 740.692, 269.307, 44.702)]
        for scale in (.5, 1, 2):
            n = [tuple(v * scale for v in b) for b in nums]
            o = [tuple(v * scale for v in b) for b in opts]
            self.assertEqual([(i, j) for i, j, _ in match_nums_to_options(n, o)], [(0, 0), (1, 1)])
            self.assertEqual([(i, j) for i, j, _ in match_nums_to_options(n, o[1:])], [(1, 0)])
        self.assertEqual(match_nums_to_options(nums[:1], [(2100, 687.16, 274, 50)]), [])

    def test_invalid_geometry_and_empty_inputs(self):
        self.assertEqual(match_nums_to_options([], []), [])
        self.assertEqual(match_nums_to_options([(10, 10, -1, 10)], [(50, 10, 100, 10)]), [])

    def test_outside_number_keeps_first_option(self):
        image = np.tile(np.arange(1000), (40, 1))
        crop = crop_option_row(image, [(90, 20, 10, 20)], 100, 5, 500, 35,
                               paired=(90, 20, 10, 20), w=1000)
        self.assertEqual(crop[0, 0], 100)

    def test_crop_uses_paired_number_only(self):
        image = np.tile(np.arange(1000), (40, 1))
        crop = crop_option_row(image, [(110, 20, 10, 20), (400, 20, 10, 20)],
                               100, 5, 500, 35, paired=(110, 20, 10, 20))
        self.assertEqual(crop[0, 0], 117)

    def test_compact_rows_sort_by_row(self):
        data = [{"no": i * 2 + j + 1, "opt_box": [.2 + j * .5, .1 + i * .04, .2, .025]}
                for i in range(3) for j in range(2)]
        self.assertEqual([r["no"] for r in _order_row(data)], list(range(1, 7)))

    def test_missing_number_is_not_renumbered(self):
        data = rows([1, 2, 4, 5, 6, 7, 8, 9, 10])
        before = copy.deepcopy(data)
        fix_no_sequence(data)
        self.assertEqual([r["no"] for r in data], [r["no"] for r in before])

    def test_real_number_jump_is_preserved(self):
        data = rows([1, 2, 3, 4, 5, 31, 32])
        fix_no_sequence(data)
        self.assertEqual([r["no"] for r in data], [1, 2, 3, 4, 5, 31, 32])

    def test_isolated_ocr_error_can_be_corrected(self):
        data = rows([1, 2, 0, 4, 5])
        fix_no_sequence(data)
        self.assertEqual([r["no"] for r in data], [1, 2, 3, 4, 5])
        self.assertEqual(data[2]["no_raw"], 0)
        self.assertEqual(data[2]["no_source"], "sequence")

    def test_two_anchors_do_not_invent_a_third_number(self):
        data = rows([0, 24, 25])
        fix_no_sequence(data)
        self.assertEqual([r["no"] for r in data], [0, 24, 25])

    def test_layout_blocks_can_correct_without_crossing_number_jump(self):
        data = rows([1, 2, 3, 4, 5, 0, 7, 8, 9, 10, 31, 32])
        for i, row in enumerate(data):
            row["opt_box"][2] = .4 if i < 5 or i >= 10 else .2
        fix_no_sequence(data)
        self.assertEqual([r["no"] for r in data], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 31, 32])

    def test_duplicate_readings_are_not_marked_valid(self):
        class Digit:
            def read(self, image):
                return 1, .99
        class Choice:
            def read(self, image):
                return 'A', .99
        boxes = [[cls, x, y, bw, .05] for y in [.2, .5]
                 for cls, x, bw in [(2, .1, .05), (3, .3, .5)]]
        data = read_choice_zone(np.zeros((200, 200, 3), np.uint8), boxes,
                                Digit(), Choice(), fix_no=False)
        self.assertEqual(len(data), 2)
        self.assertTrue(all(not r["valid"] for r in data))

    def test_unrelated_pair_is_never_read(self):
        class Reader:
            def read(self, image):
                raise AssertionError("无关框不应送入识别模型")
        data = read_choice_zone(np.zeros((600, 200, 3), np.uint8),
                                [[2, .05, .02, .04, .02], [3, .5, .85, .4, .03]],
                                Reader(), Reader())
        self.assertEqual(data, [])


if __name__ == "__main__":
    unittest.main()
