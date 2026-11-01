"""英语序列候选：分区、反传与导出规格检查。"""
import io
import sys
import unittest
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cnn.choice_sequence import SequenceNet
from common.cnn_data import choice_subject_rows


class ChoiceSequenceTest(unittest.TestCase):
    def test_parent_page_isolation(self):
        def row(stem, split):
            return {'stem': stem, 'split': split}
        val = row('1.英语.选择题区.2', 'val')
        keep = row('2.英语.选择题区', 'train')
        rows = [val, keep, row('1.英语.选择题区', 'train'),
                row('3.物理.选择题区', 'train')]
        self.assertEqual(choice_subject_rows(rows, '英语'), [val, keep])

    def test_forward_backward_and_export(self):
        torch.set_num_threads(2)
        torch.manual_seed(42)
        net = SequenceNet()
        x = torch.randn(3, 1, 48, 192)
        out = net(x)
        self.assertEqual(tuple(out.shape), (3, 6))
        loss = torch.nn.functional.binary_cross_entropy_with_logits(out, torch.rand_like(out))
        loss.backward()
        self.assertTrue(all(p.grad is not None and torch.isfinite(p.grad).all()
                            for p in net.parameters()))
        net.eval()
        traced = torch.jit.trace(net, x[:1])
        buffer = io.BytesIO()
        torch.jit.save(traced, buffer)
        loaded = torch.jit.load(io.BytesIO(buffer.getvalue()))
        for sample in (x, x[:1], torch.zeros_like(x[:1])):
            with torch.no_grad():
                torch.testing.assert_close(loaded(sample), net(sample))


if __name__ == '__main__':
    unittest.main()
