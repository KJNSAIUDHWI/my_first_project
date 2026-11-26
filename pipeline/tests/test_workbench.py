# -*- coding: utf-8 -*-
"""统一工作台自动化测试（环境自检、非破坏性入库与去重、进度与人员贡献榜、SSE 事件联动）。"""
from __future__ import annotations

import csv
import json
import shutil
import sys
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "工作台"))

import core  # noqa: E402
import serve as wb_serve  # noqa: E402

TEMP_BASE = Path(r"D:\temp\antigravity_20261127_workbench")


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.work = TEMP_BASE / self._testMethodName
        if self.work.exists():
            shutil.rmtree(self.work)
        self.work.mkdir(parents=True, exist_ok=True)
        self.batches = self.work / "batches"

    def tearDown(self):
        if self.work.exists():
            shutil.rmtree(self.work, ignore_errors=True)

    def test_check_environment_and_prompt(self):
        rep = core.check_environment(ROOT, self.batches)
        self.assertIn("items", rep)
        self.assertIn("elapsed_ms", rep)
        self.assertLess(rep["elapsed_ms"], 5000)
        prompt = core.build_agent_prompt(rep, root=ROOT, extra_note="测试补充说明")
        self.assertIn("自动改卷", prompt)
        self.assertIn("测试补充说明", prompt)

    def test_ingest_non_destructive_and_dedup(self):
        from PIL import Image

        src = self.work / "raw_scans"
        src.mkdir(parents=True)
        # 2 张不同图 + 1 张完全相同的重复图 + 1 张 PNG
        img1 = src / "IMG_001.jpg"
        Image.new("RGB", (40, 30), (240, 240, 240)).save(img1, format="JPEG")
        shutil.copy2(img1, src / "IMG_001_copy.jpg")
        Image.new("RGB", (40, 30), (120, 180, 220)).save(src / "scan_02.png", format="PNG")

        meta = core.ingest_batch(src, subject="物理", batches_root=self.batches, batch_name="期中测试")
        self.assertEqual(meta["total_images"], 2)
        self.assertEqual(meta["last_imported"], 2)
        self.assertEqual(meta["last_skipped_dup"], 1)
        # 原图完整保留
        self.assertEqual(len(list(src.iterdir())), 3)

        out_imgs = sorted(Path(meta["images_dir"]).glob("*.jpg"))
        self.assertEqual(len(out_imgs), 2)
        self.assertTrue(out_imgs[0].name.endswith("_物理_0001.jpg"))
        self.assertTrue(out_imgs[1].name.endswith("_物理_0002.jpg"))

        # 验证 manifest.csv 可溯源
        manifest = Path(meta["images_dir"]).parent / "manifest.csv"
        with manifest.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["subject"] for r in rows}, {"物理"})
        self.assertTrue(all(r["sha256"] and r["orig_name"] for r in rows))

    def test_http_overview_presence_and_sse_event(self):
        state = wb_serve.WorkbenchState(root=ROOT, batches_root=self.batches, port=0)
        server = ThreadingHTTPServer(("127.0.0.1", 0), wb_serve.make_handler(state))
        port = server.server_address[1]
        state.port = port
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            base = f"http://127.0.0.1:{port}"
            # 1. 健康检查
            with urllib.request.urlopen(f"{base}/api/health", timeout=3) as r:
                self.assertTrue(json.loads(r.read().decode("utf-8"))["ok"])

            # 2. 协作者上线登记
            req = urllib.request.Request(
                f"{base}/api/presence",
                data=json.dumps({"client_id": "c1", "name": "妈妈", "task": "总览"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                self.assertTrue(json.loads(r.read().decode("utf-8"))["ok"])

            # 3. 概览返回主机标识与在线人员
            with urllib.request.urlopen(f"{base}/api/overview", timeout=3) as r:
                ov = json.loads(r.read().decode("utf-8"))
            self.assertTrue(ov["is_host"])
            self.assertEqual(len(ov["tasks"]), 3)
            self.assertEqual([t["id"] for t in ov["tasks"] if t["tier"] == "primary"], ["review", "choice"])
            self.assertTrue(any(u["name"] == "妈妈" for u in ov["online_users"]))

            # 4. CLI emit_event 推送到总线
            q = state.bus.subscribe()
            ok = core.emit_event("ingest", "done", "CLI 测试联动", hub_url=base)
            self.assertTrue(ok)
            ev = q.get(timeout=2)
            self.assertEqual(ev["source"], "cli")
            self.assertEqual(ev["step"], "ingest")
            state.bus.unsubscribe(q)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
