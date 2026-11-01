"""链路评估的漏检、未知真值与一对一计数边界。"""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eval_chain import assign, detection, row_global, score_rows


class ChainEvaluationTest(unittest.TestCase):
    def test_detection_class_and_one_to_one(self):
        box=[0,0,10,10]
        gt=[{'cls':2,'box':box}]
        pred=[{'cls':2,'box':box},{'cls':2,'box':box},{'cls':3,'box':box}]
        result=detection(gt,pred)
        self.assertEqual(result['2']['matched'],1)
        self.assertEqual(result['2']['extra'],1)
        self.assertEqual(result['3']['matched'],0)
        self.assertEqual(assign([box],[[20,20,30,30]]),[])

    def test_missing_is_error_unknown_is_not_correct(self):
        truth=[{'box':[0,0,10,10],'answer':'C','no':3},
               {'box':[20,0,30,10],'answer':None,'no':None}]
        result=score_rows(copy.deepcopy(truth),[])
        self.assertEqual(result['answer_known'],1)
        self.assertEqual(result['number_known'],1)
        self.assertEqual(result['joint_correct'],0)
        self.assertEqual(result['rows'],2)

    def test_blank_and_invalid_result_are_distinct(self):
        truth=[{'box':[0,0,10,10],'answer':'','no':3}]
        prediction=[{'box':[0,0,10,10],'answer':'','no':3,'no_raw':0,'valid':False}]
        result=score_rows(truth,prediction)
        self.assertEqual(result['answer_correct'],1)
        self.assertEqual(result['joint_correct'],1)
        self.assertEqual(result['number_raw_correct'],0)
        self.assertEqual(result['usable_correct'],0)

    def test_global_coordinates(self):
        row={'opt_box':[.5,.5,.5,.5]}
        self.assertEqual(row_global(row,[100,200,300,300])['box'],[150,225,250,275])


if __name__=='__main__':
    unittest.main()
