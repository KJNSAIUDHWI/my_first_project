# -*- coding: utf-8 -*-
"""防 NaN 梯度裁剪与故障现场留档回归测试（不读写正式训练数据）。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import torch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from training.grad_guard import GradGuardTrainer, stable_clip_grad_norm_


class GradGuardTests(unittest.TestCase):
    def _param(self, values):
        p = torch.nn.Parameter(torch.zeros(len(values)))
        p.grad = torch.tensor(values, dtype=torch.float32)
        return p

    def test_ordinary_equivalence_and_extreme_safe_scaling(self):
        for values in [[3.0, 4.0], [30.0, 40.0], [0.0, 0.0]]:
            actual, ref = self._param(values), self._param(values)
            old = torch.nn.utils.clip_grad_norm_([ref], 10.0)
            norm = stable_clip_grad_norm_([actual], 10.0)
            self.assertTrue(torch.allclose(norm, old.double(), atol=1e-6, rtol=1e-6))
            self.assertTrue(torch.allclose(actual.grad, ref.grad, atol=1e-6, rtol=1e-6))

        actual, ref = self._param([3e20, 4e20]), self._param([3e20, 4e20])
        old = torch.nn.utils.clip_grad_norm_([ref], 10.0)
        self.assertFalse(bool(torch.isfinite(old)))
        norm = stable_clip_grad_norm_([actual], 10.0)
        self.assertTrue(bool(torch.isfinite(norm)))
        self.assertTrue(torch.allclose(actual.grad, torch.tensor([6.0, 8.0]), atol=2e-6))

        actual = self._param([1e38, -1e38])
        norm = stable_clip_grad_norm_([actual], 10.0)
        self.assertTrue(bool(torch.isfinite(norm)))
        self.assertLess(abs(float(torch.linalg.vector_norm(actual.grad.double())) - 10.0), 1e-5)

        first, second = self._param([3e20]), self._param([4e20])
        stable_clip_grad_norm_([first, second], 10.0)
        self.assertTrue(torch.allclose(first.grad, torch.tensor([6.0]), atol=2e-6))
        self.assertTrue(torch.allclose(second.grad, torch.tensor([8.0]), atol=2e-6))

        for invalid in [float("nan"), float("inf")]:
            p = self._param([invalid, 1.0])
            with self.assertRaises(FloatingPointError):
                stable_clip_grad_norm_([p], 10.0)
        self.assertEqual(stable_clip_grad_norm_([]), 0)

    def test_numerical_failure_capture_preserves_raw_state(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            for fail_capture in (False, True):
                obj = GradGuardTrainer.__new__(GradGuardTrainer)
                obj.save_dir = work / str(fail_capture)
                obj.save_dir.mkdir()
                obj.model = torch.nn.BatchNorm1d(2)
                obj.model.running_mean[0] = float("nan")
                obj.guard_step = 1
                obj.epoch = 0
                obj.guard_batch_files = ["fixture.jpg"]
                obj.guard_batch = None

                def injected(*args, **kwargs):
                    raise RuntimeError("capture test only")

                with self.assertRaises(FloatingPointError) as ctx:
                    if fail_capture:
                        with patch("torch.save", side_effect=injected):
                            obj._fail("fixture.loss", "NaN/Inf")
                    else:
                        obj._fail("fixture.loss", "NaN/Inf")
                self.assertIn("fixture.loss", str(ctx.exception))
                rec = json.loads((obj.save_dir / "numerical_failure.jsonl").read_text("utf-8"))
                if fail_capture:
                    self.assertIn("capture test only", rec["model_state_capture_error"])
                else:
                    saved = torch.load(obj.save_dir / "failure_model_state.pt", map_location="cpu", weights_only=False)
                    self.assertTrue(bool(torch.isnan(saved["state_dict"]["running_mean"][0])))
                    self.assertTrue(bool(torch.isnan(obj.model.running_mean[0])))


if __name__ == "__main__":
    unittest.main()
