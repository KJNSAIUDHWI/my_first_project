"""MiMo输出检查、未审保护及预标注不代替人工审核。"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cnn import mimo_digit
from cnn.refresh_digit_crops import match_boxes
from common import row_log


class MiMoDigit(unittest.TestCase):
    def test_strict_ids_labels_and_flags(self):
        valid=[dict(id=1,label=60,uncertain=False,crop_issue=True),dict(id=2,label=None,uncertain=True,crop_issue=False)]
        self.assertEqual(mimo_digit.parse_reply(json.dumps(valid[::-1]),2),valid)
        for invalid in [valid[:1],[valid[0],valid[0]],
                        [dict(valid[0],label=61),valid[1]],
                        [dict(valid[0],label=True),valid[1]],
                        [dict(valid[0],uncertain='false'),valid[1]]]:
            with self.assertRaises(ValueError):mimo_digit.parse_reply(json.dumps(invalid),2)

    def test_prelabel_stays_unreviewed_and_reviewed_is_protected(self):
        prefix=datetime.now().strftime('%Y-%m-%d-%H-%M')+'_codex_mimo_test_'
        with tempfile.TemporaryDirectory(prefix=prefix,dir='D:/temp') as tmp:
            base=Path(tmp)/'data';(base/'images').mkdir(parents=True)
            Image.new('L',(25,30),255).save(base/'images/one.png')
            row=dict(file='one.png',stem='page1',split='train')
            row_log.append(base,'one',{'事件':'预标注','标签':0})
            answer=json.dumps([dict(id=1,label=60,uncertain=False,crop_issue=True)])
            with patch.dict('os.environ',{'MIMO_API_KEY':'unit-test-placeholder'}),patch.object(mimo_digit,'predict',return_value=(answer,{})):
                mimo_digit.run(base,[row],Path(tmp)/'out',apply=True)
                state=row_log.digit_state(row_log.read(base,'one'))
                self.assertEqual(state['label'],60)
                self.assertFalse(state['reviewed'])
                self.assertTrue(state['crop_issue'])
                row_log.append(base,'one',{'事件':'审核','标签':60})
                with self.assertRaises(ValueError):mimo_digit.run(base,[row],Path(tmp)/'out2',apply=True)

    def test_matching_is_one_to_one_and_leaves_missing_unmatched(self):
        old=[[.1,.2,.1,.1],[.1,.4,.1,.1],[.1,.6,.1,.1]]
        new=[old[1],old[0]]
        self.assertEqual(match_boxes(old,new),{0:1,1:0})
        self.assertEqual(match_boxes(old,[]),{})


if __name__=='__main__':unittest.main()
