# -*- coding: utf-8 -*-
"""局域网多人动态抢占锁与识别验收并发合并测试。"""
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "pipeline"))
from common.lan_collab import ClaimManager, resolve_host
from common.recognition_api import save_json


class LanCollabTests(unittest.TestCase):
    def test_resolve_host(self):
        self.assertEqual(resolve_host(None, False), "127.0.0.1")
        self.assertEqual(resolve_host(None, True), "0.0.0.0")
        self.assertEqual(resolve_host("192.168.1.10", True), "192.168.1.10")

    def test_claim_next_locks_and_releases(self):
        cm = ClaimManager(ttl_seconds=60)
        items = ["001", "002", "003"]
        done = set()

        r1 = cm.claim_next("dad_tab", "爸爸", items, lambda k: k in done)
        self.assertEqual(r1["key"], "001")

        # 妈妈后进入，自动跳过被爸爸锁住的 001，拿到 002
        r2 = cm.claim_next("mom_tab", "妈妈", items, lambda k: k in done)
        self.assertEqual(r2["key"], "002")

        # 妈妈强行 acquire 001 被拒绝
        acq = cm.acquire("001", "mom_tab", "妈妈")
        self.assertFalse(acq["ok"])
        self.assertEqual(acq["locked_by"], "爸爸")

        # 爸爸审完 001 释放后再领，拿到 003
        done.add("001")
        cm.release("dad_tab", "001")
        r3 = cm.claim_next("dad_tab", "爸爸", items, lambda k: k in done)
        self.assertEqual(r3["key"], "003")

        # 此时只剩 002（妈妈锁着）和 003（爸爸锁着），第三个人进来看到 all_locked
        r4 = cm.claim_next("kid_tab", "孩子", items, lambda k: k in done)
        self.assertIsNone(r4["key"])
        self.assertEqual(r4["reason"], "all_locked")

    def test_concurrent_claims_never_collide(self):
        cm = ClaimManager(ttl_seconds=60)
        items = [f"img_{i:03d}" for i in range(40)]
        claimed = []
        lock = threading.Lock()

        def worker(idx):
            res = cm.claim_next(f"client_{idx}", f"用户{idx}", items, lambda k: False)
            with lock:
                claimed.append(res["key"])

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(worker, range(40)))
        self.assertEqual(len(set(claimed)), 40)

    def test_review_store_merges_different_items_and_blocks_same_item(self):
        spec = importlib.util.spec_from_file_location("review_srv_test", ROOT / "tools/识别验收.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            save_json(folder / "manifest.json", {"input": "fake"})
            save_json(folder / "results.json", {
                "pages": [
                    {"id": "p0", "name": "卷1", "items": [{"id": "p0_0"}]},
                    {"id": "p1", "name": "卷2", "items": [{"id": "p1_0"}]},
                ]
            })
            store = mod.ReviewStore(folder)
            # 爸爸改 p0_0，revision 0 -> 1
            s1 = store.update({"id": "p0_0", "label": "正确", "correction": "", "revision": 0, "reviewer": "爸爸"})
            self.assertEqual(s1["revision"], 1)
            # 妈妈手里还是旧 revision=0，但改的是另一页的 p1_0，应该自动合并成功！
            s2 = store.update({"id": "p1_0", "label": "识别错误", "correction": "改成B", "revision": 0, "reviewer": "妈妈"})
            self.assertEqual(s2["revision"], 2)
            self.assertEqual(s2["items"]["p0_0"]["reviewer"], "爸爸")
            self.assertEqual(s2["items"]["p1_0"]["reviewer"], "妈妈")
            # 如果妈妈用旧 revision=0 去覆盖爸爸刚改过的同一题 p0_0，必须拦截
            with self.assertRaises(ValueError):
                store.update({"id": "p0_0", "label": "看不清", "correction": "", "revision": 0, "reviewer": "妈妈"})

            # 测试 HTTP /api/claim 动态领卷
            srv = mod.make_server(folder, 0, host="127.0.0.1")
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            base = f"http://127.0.0.1:{srv.server_port}"
            try:
                with urllib.request.urlopen(base) as r:
                    html = r.read().decode("utf-8")
                token = re.search("const TOKEN='([^']+)'", html).group(1)
                def post(path, payload):
                    req = urllib.request.Request(
                        base + path,
                        json.dumps(payload).encode("utf-8"),
                        {"Content-Type": "application/json", "X-Review-Token": token, "Origin": base},
                    )
                    with urllib.request.urlopen(req) as resp:
                        return json.load(resp)
                c1 = post("/api/claim", {"client_id": "dad", "user": "爸爸"})
                c2 = post("/api/claim", {"client_id": "mom", "user": "妈妈"})
                self.assertEqual(c1["key"], "p0")
                self.assertEqual(c2["key"], "p1")
            finally:
                srv.shutdown()
                srv.server_close()
                t.join()


if __name__ == "__main__":
    unittest.main()
