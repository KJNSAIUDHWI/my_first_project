"""报告中的原页坐标必须恢复实际模型裁区。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chain_report import prediction_geometry


class ReportGeometryTest(unittest.TestCase):
    def test_crop_origin_and_number_box(self):
        crop, box = prediction_geometry({'opt_box':[.5,.5,.5,.5],
                                         'box':[150,225,250,275],
                                         'num_box':[.1,.5,.1,.2]})
        self.assertEqual(crop, (100,200,200,100))
        self.assertEqual(box, [110,240,130,260])


if __name__ == '__main__':
    unittest.main()
