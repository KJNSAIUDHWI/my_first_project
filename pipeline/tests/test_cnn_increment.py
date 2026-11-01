"""增量裁图及旧题号权重扩展的边界回归。"""
import io
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime
import numpy as np
import torch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cnn.prepare_increment import crops
from cnn.train_digit import Net
from common.cnn_training import initialize
from common.model_registry import inspect_weight


class Increment(unittest.TestCase):
    def test_crop_orphans_and_paired_number(self):
        image=np.arange(100*300,dtype=np.uint16).reshape(100,300).astype('uint8')
        # 第一选项框内有已配对题号，第二选项框没有可配对题号。
        boxes=[[2,.1,.25,.06,.2],[3,.45,.25,.9,.3],[3,.5,.8,.8,.2]]
        digits,choices=crops(image,boxes)
        self.assertEqual(len(digits),1)
        self.assertEqual(len(choices),2)
        self.assertEqual(choices[0][2],0)
        self.assertIsNone(choices[1][2])
        self.assertLess(choices[0][-1].shape[1],272)
        self.assertEqual(choices[1][-1].shape[1],choices[1][3][2]-choices[1][3][0])

    def test_expand_old_output_and_validate_both_versions(self):
        folder='D:/temp/'+datetime.now().strftime('%Y-%m-%d-%H-%M')+'_codex_cnn_weight_test_'
        with tempfile.TemporaryDirectory(prefix=Path(folder).name,dir='D:/temp') as tmp:
            tmp=Path(tmp)
            old=Net(35).eval()
            path=tmp/'old.pt'
            torch.save(old.state_dict(),path)
            expanded=Net(61)
            new_rows=expanded.fc2.weight[35:].detach().clone()
            initialize(expanded,path,False,expand_digit=True)
            self.assertTrue(torch.equal(expanded.fc2.weight[:35],old.fc2.weight))
            self.assertTrue(torch.equal(expanded.fc2.bias[:35],old.fc2.bias))
            self.assertTrue(torch.equal(expanded.fc2.weight[35:],new_rows))
            self.assertTrue(torch.equal(expanded.c1.weight,old.c1.weight))
            for nc,net in [(35,old),(61,expanded),(36,Net(36))]:
                net.eval()
                buffer=io.BytesIO()
                torch.jit.save(torch.jit.trace(net,torch.zeros(1,1,64,64)),buffer)
                p=tmp/('%d.pt'%nc);p.write_bytes(buffer.getvalue())
                if nc==36:
                    with self.assertRaises(ValueError):inspect_weight(p,'digit')
                else:
                    self.assertTrue(inspect_weight(p,'digit')['finite'])
                    if nc==35:
                        initialize(Net(61),p,False,expand_digit=True)


if __name__=='__main__':unittest.main()
