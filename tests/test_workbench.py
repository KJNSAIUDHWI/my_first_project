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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "workbench"))

import core  # noqa: E402
import server as wb_serve  # noqa: E402

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
            self.assertEqual([t["id"] for t in ov["tasks"] if t["tier"] == "primary"], ["review"])
            self.assertEqual([t["id"] for t in ov["tasks"] if t["tier"] == "secondary"], ["choice", "annotate"])
            self.assertTrue(any(u["name"] == "妈妈" for u in ov["online_users"]))

            # 4. CLI emit_event 推送到总线
            q = state.bus.subscribe()
            ok = core.emit_event("ingest", "done", "CLI 测试联动", hub_url=base)
            self.assertTrue(ok)
            ev = q.get(timeout=2)
            self.assertEqual(ev["source"], "cli")
            self.assertEqual(ev["step"], "ingest")
            state.bus.unsubscribe(q)

            # 5. 路径穿越拦截 (/api/open-edit & set_active_batch)
            self.assertIsNone(core.set_active_batch("../evil", self.batches))
            self.assertIsNone(core.set_active_batch("a/b", self.batches))
            bad_req = urllib.request.Request(
                f"{base}/api/open-edit",
                data=json.dumps({"page_name": "../../.env"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                urllib.request.urlopen(bad_req, timeout=3)
                # 即使进入合法文件名分支也绝不能复制 .env
                self.assertFalse((ROOT / "data" / "m1" / "images" / ".env.jpg").exists())
            except urllib.error.HTTPError as he:
                self.assertEqual(he.code, 400)

            # 6. 互斥校验：训练运行中禁止启动识别，且 /api/pipeline/stop 可正常调用
            state.train_job["running"] = True
            res_block = state.start_pipeline()
            self.assertFalse(res_block["ok"])
            self.assertIn("训练", res_block["error"])
            state.train_job["running"] = False

            stop_req = urllib.request.Request(
                f"{base}/api/pipeline/stop",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(stop_req, timeout=3) as r:
                self.assertTrue(json.loads(r.read().decode("utf-8"))["ok"])
        finally:
            server.shutdown()
            server.server_close()

    def test_audit_regressions_bom_self_ingest_and_review_claim(self):
        # 1. UTF-8 BOM .env 读写兼容与原子更新
        fake_root = self.work / "fake_root"
        fake_root.mkdir(parents=True)
        (fake_root / ".env").write_bytes(b"\xef\xbb\xbfVLM_PROVIDER=doubao\nARK_API_KEY=old_key\n")
        self.assertEqual(core._read_env_key(fake_root, "VLM_PROVIDER"), "doubao")
        core.save_env_keys({"VLM_PROVIDER": "mimo", "VLM_MODEL": "mimo-v2.6-flash"}, root=fake_root)
        self.assertEqual(core._read_env_key(fake_root, "VLM_PROVIDER"), "mimo")
        self.assertEqual(core._read_env_key(fake_root, "ARK_API_KEY"), "old_key")

        # 2. 禁止选择包含 batches/ 的祖先目录导致递归自导入
        self.batches.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ValueError):
            core.ingest_batch(self.work, subject="物理", batches_root=self.batches)

        # 3. 8904 /api/claim 必须同时返回 page_id 与 key，且 submit-page 持久化 boxes 到 feedback.json
        import importlib.util
        spec = importlib.util.spec_from_file_location("_review_srv", str(ROOT / "apps" / "review" / "server.py"))
        rev_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rev_mod)

        rec_dir = self.work / "rec_demo"
        rec_dir.mkdir(parents=True)
        (rec_dir / "manifest.json").write_text('{"schema":2}', encoding="utf-8")
        (rec_dir / "results.json").write_text(json.dumps({
            "pages": [
                {"id": "p000", "name": "page0", "width": 1000, "height": 1400, "image": "p0.jpg", "boxes": [], "items": []},
                {"id": "p001", "name": "page1", "width": 1000, "height": 1400, "image": "p1.jpg", "boxes": [], "items": []},
            ],
            "grids": [],
            "status": "待人工验收",
        }, ensure_ascii=False), encoding="utf-8")

        store = rev_mod.ReviewStore(rec_dir)
        out = store.submit_page({
            "page_id": "p000",
            "reviewer": "测试员",
            "boxes_modified": True,
            "boxes": [{"cls": 4, "box": [10, 20, 200, 120]}],
            "sync_log": False,
        }, allow_sync_data=False)
        self.assertTrue(out["ok"])
        fb = store.read()
        self.assertEqual(len(fb["items"]["p000"]["boxes"]), 1)

        srv = rev_mod.make_server(rec_dir, port=0, host="127.0.0.1", allow_sync_data=False)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            base = f"http://127.0.0.1:{srv.server_port}"
            html = urllib.request.urlopen(base + "/", timeout=3).read().decode("utf-8")
            import re
            m = re.search(r"const TOKEN='([^']+)'", html)
            self.assertIsNotNone(m)
            tok = m.group(1)
            req = urllib.request.Request(
                base + "/api/claim",
                data=json.dumps({"client_id": "c_test", "user": "老师", "exclude": ["p000"]}).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Review-Token": tok},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                claim = json.loads(r.read().decode("utf-8"))
            self.assertEqual(claim.get("key"), "p001")
            self.assertEqual(claim.get("page_id"), "p001")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_api_pricing_permanent_cache_and_cost(self):
        from engine import api_pricing

        cache_file = self.work / "api_pricing_cache.json"
        cache_file.write_text(json.dumps({
            "schema_version": 1,
            "permanent": True,
            "crawled_at": "2026-11-28 03:00:00",
            "providers": {"doubao": {"ok": True, "count": 1}},
            "models": {
                "doubao-seed-2-1-lite": {
                    "provider": "doubao",
                    "display_name": "doubao-seed-2.1-lite",
                    "input_per_m": 0.80,
                    "output_per_m": 2.70,
                    "cache_hit_per_m": 0.16,
                    "currency": "CNY",
                    "source": "official_crawl",
                    "source_url": "https://www.volcengine.com/docs/82379/1544106",
                }
            },
            "custom_overrides": {},
        }, ensure_ascii=False), encoding="utf-8")

        # 已有永久缓存时 ensure_pricing_cache 不触发网络爬取，直接返回缓存
        loaded = api_pricing.ensure_pricing_cache(cache_path=cache_file, force=False)
        self.assertEqual(loaded["crawled_at"], "2026-11-28 03:00:00")

        # 带日期后缀与连字符的模型 ID 正确匹配官网点号版本
        est = api_pricing.estimate_cost(
            "doubao-seed-2-1-lite-260915",
            prompt_tokens=65876,
            completion_tokens=9397,
            cached_tokens=5876,
            pages=30,
            cache_path=cache_file,
        )
        self.assertTrue(est["found"])
        self.assertEqual(est["matched_model"], "doubao-seed-2.1-lite")
        expected = (60000 * 0.80 + 5876 * 0.16 + 9397 * 2.70) / 1_000_000.0
        self.assertAlmostEqual(est["cost"], expected, places=5)

        # 自定义单价永久覆盖优先于官网缓存
        api_pricing.save_custom_price_override(
            "my-relay-vl",
            input_per_m=1.2,
            output_per_m=3.6,
            cache_path=cache_file,
        )
        est_custom = api_pricing.estimate_cost(
            "my-relay-vl",
            prompt_tokens=1_000_000,
            completion_tokens=500_000,
            cache_path=cache_file,
        )
        self.assertTrue(est_custom["found"])
        self.assertEqual(est_custom["source"], "manual")
        self.assertAlmostEqual(est_custom["cost"], 3.0, places=5)

    def test_subservice_cli_flags_and_choice_fast_bind(self):
        import socket
        import subprocess

        def free_port() -> int:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                return int(s.getsockname()[1])

        # 1. 验证训练监控 (--no-browser) 与标框工具 (--data / --no-browser) 启动参数合法且能秒级监听
        mon_port = free_port()
        ann_port = free_port()
        choice_port = free_port()

        ann_dir = self.work / "m1_demo"
        (ann_dir / "images").mkdir(parents=True)
        ch_dir = self.work / "choice_demo"
        (ch_dir / "images").mkdir(parents=True)
        (ch_dir / "log").mkdir(parents=True)
        (ch_dir / "manifest.csv").write_text("stem,row,no,n_opts,subject\ndemo_page,1,1,4,物理\n", encoding="utf-8-sig")

        procs = []
        try:
            cmds = [
                (mon_port, [sys.executable, str(ROOT / "apps" / "train_monitor" / "server.py"), "--port", str(mon_port), "--no-browser"]),
                (ann_port, [sys.executable, str(ROOT / "apps" / "annotator" / "server.py"), "--data", str(ann_dir), "--port", str(ann_port), "--lan", "--no-browser"]),
                (choice_port, [sys.executable, str(ROOT / "apps" / "choice_review" / "server.py"), "--data", str(ch_dir), "--port", str(choice_port), "--lan"]),
            ]
            for port, cmd in cmds:
                p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                procs.append((port, p))

            for port, p in procs:
                deadline = time.time() + 6.0
                ready = False
                while time.time() < deadline:
                    if p.poll() is not None:
                        err = p.stderr.read() if p.stderr else ""
                        self.fail(f"子服务端口 {port} 立即退出 ({p.returncode}): {err}")
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1.0) as r:
                            if r.status == 200:
                                ready = True
                                break
                    except Exception:
                        time.sleep(0.15)
                self.assertTrue(ready, f"子服务端口 {port} 启动超时")
            # 涂黑审核 /tree 正常返回分组数据
            with urllib.request.urlopen(f"http://127.0.0.1:{choice_port}/tree", timeout=3.0) as r:
                tree = json.loads(r.read().decode("utf-8"))
            self.assertEqual(tree["total"], 1)
        finally:
            for _, p in procs:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        p.kill()

    def test_review_box_delete_sync_and_single_box_api_recognize(self):
        import importlib.util
        import threading
        from PIL import Image
        from engine.vlm_client import VlmClient

        spec = importlib.util.spec_from_file_location("review_srv", str(ROOT / "apps" / "review" / "server.py"))
        review_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(review_mod)

        rec_dir = self.work / "rec_crud_demo"
        (rec_dir / "images").mkdir(parents=True)
        (rec_dir / "pages").mkdir(parents=True)
        (rec_dir / "crops").mkdir(parents=True)
        Image.new("RGB", (600, 400), "white").save(rec_dir / "images" / "p000.jpg")
        (rec_dir / "manifest.json").write_text(json.dumps({"schema": 2, "sources": ["p000.jpg"]}), encoding="utf-8")

        init_page = {
            "id": "p000",
            "name": "demo_p000",
            "image": "images/p000.jpg",
            "width": 600,
            "height": 400,
            "boxes": [
                {"cls": 5, "box": [20, 40, 260, 120], "stage": "m3"},
                {"cls": 5, "box": [120, 45, 255, 118], "stage": "m3"},
            ],
            "items": [
                {
                    "id": "p000_000",
                    "page_id": "p000",
                    "kind": "fill",
                    "box": [20, 40, 260, 120],
                    "number": 13,
                    "status": "已识别",
                    "issues": [],
                    "transcription": {"kind": "fill", "question_number": "13", "answers": {"1": {"1": "无规则"}}},
                },
                {
                    "id": "p000_001",
                    "page_id": "p000",
                    "kind": "fill",
                    "box": [120, 45, 255, 118],
                    "number": None,
                    "status": "已识别",
                    "issues": [],
                    "transcription": {"kind": "fill", "question_number": "题号看不清", "answers": {"1": {"1": "间隔"}}},
                },
            ],
        }
        init_results = {
            "pages": [init_page],
            "grids": [],
            "model": "doubao-seed-2-1-lite-260915",
            "provider": "doubao",
            "provider_label": "豆包",
            "status": "识别完成，待人工验收",
            "stats": {"pages": 1, "fill_items": 2, "items_with_transcription": 2, "prompt_tokens": 100, "completion_tokens": 20},
        }
        (rec_dir / "results.json").write_text(json.dumps(init_results, ensure_ascii=False), encoding="utf-8")
        (rec_dir / "pages" / "p000.json").write_text(json.dumps(init_page, ensure_ascii=False), encoding="utf-8")

        def mock_transport(req_payload):
            txt = req_payload["messages"][0]["content"][0]["text"]
            import re
            ids = re.findall(r'"id":\s*"([^"]+)"', txt)
            kinds = dict(re.findall(r'"id":\s*"([^"]+)",\s*"kind":\s*"([^"]+)"', txt))
            out = {}
            for ident in ids:
                if kinds.get(ident) == "identity":
                    out[ident] = {"kind": "identity", "name": "张三", "sid": "00123"}
                else:
                    out[ident] = {"kind": "fill", "question_number": "20", "answers": {"1": {"1": "电流大小", "2": "转换法"}}}
            return {
                "model": "doubao-seed-2-1-lite-260915",
                "usage": {"prompt_tokens": 320, "completion_tokens": 45},
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(out, ensure_ascii=False)}}],
            }

        def client_factory(out_dir):
            return VlmClient(out_dir, key="test-key", transport=mock_transport, provider="doubao", model="doubao-seed-2-1-lite-260915")

        srv = review_mod.make_server(rec_dir, port=0, allow_sync_data=False, client_factory=client_factory)
        port = srv.server_port
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3.0).read().decode("utf-8")
            import re
            m = re.search(r"const TOKEN='([^']+)'", html)
            self.assertIsNotNone(m)
            tok = m.group(1)

            def post_json(path, payload):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-Review-Token": tok},
                )
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))

            # 1. 删除多余框 p000_001 并提交，确认 results.json & pages/p000.json 同步消失
            st, res_sub = post_json("/api/submit-page", {
                "page_id": "p000",
                "boxes": [{"cls": 5, "box": [20, 40, 260, 120], "item_id": "p000_000"}],
                "boxes_modified": True,
                "items": [init_page["items"][0]],
                "items_modified": True,
                "sync_log": False,
            })
            self.assertEqual(st, 200)
            self.assertTrue(res_sub["ok"])
            self.assertEqual(res_sub["deleted_item_ids"], ["p000_001"])
            self.assertEqual(len(res_sub["items"]), 1)

            disk_res = json.loads((rec_dir / "results.json").read_text(encoding="utf-8"))
            self.assertEqual([it["id"] for it in disk_res["pages"][0]["items"]], ["p000_000"])
            self.assertEqual(disk_res["stats"]["fill_items"], 1)
            disk_page = json.loads((rec_dir / "pages" / "p000.json").read_text(encoding="utf-8"))
            self.assertEqual(len(disk_page["items"]), 1)

            # 2. 对新增的大题框(cls=5)和姓名学号框(cls=0)调用 /api/recognize-box
            st2, res_rec = post_json("/api/recognize-box", {
                "page_id": "p000",
                "targets": [
                    {"cls": 5, "box": [320, 120, 560, 240], "client_uid": "bx_new_1"},
                    {"cls": 0, "box": [20, 10, 180, 38], "client_uid": "bx_new_2"},
                ],
            })
            self.assertEqual(st2, 200)
            self.assertTrue(res_rec["ok"])
            self.assertEqual(len(res_rec["recognized"]), 2)
            by_uid = {r["client_uid"]: r["item"] for r in res_rec["recognized"]}
            self.assertEqual(by_uid["bx_new_1"]["number"], 20)
            self.assertEqual(by_uid["bx_new_1"]["transcription"]["answers"]["1"]["1"], "电流大小")
            self.assertEqual(by_uid["bx_new_2"]["transcription"]["name"], "张三")
            self.assertEqual(by_uid["bx_new_2"]["transcription"]["sid"], "00123")

            # 验证已持久化落盘且 token 累加
            disk_res2 = json.loads((rec_dir / "results.json").read_text(encoding="utf-8"))
            self.assertEqual(len(disk_res2["pages"][0]["items"]), 3)
            self.assertEqual(disk_res2["stats"]["fill_items"], 2)
            self.assertEqual(disk_res2["stats"]["identity_items"], 1)
            self.assertGreater(disk_res2["stats"]["prompt_tokens"], 100)

            # 3. 非 0/5 类别调用 /api/recognize-box 应明确返回 400
            with self.assertRaises(urllib.error.HTTPError) as ctx_err:
                post_json("/api/recognize-box", {
                    "page_id": "p000",
                    "targets": [{"cls": 3, "box": [20, 20, 80, 50], "client_uid": "bx_bad"}],
                })
            self.assertEqual(ctx_err.exception.code, 400)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_lease_zcode_contract_runs_api_and_cli_hub_sync(self):
        import subprocess
        from engine.run_log import RunLogger, mask_secret

        # 1. 跨进程任务租约互斥与释放
        l1 = core.acquire_pipeline_lease("recognize", "b1", owner="gui", batches_root=self.batches)
        self.assertTrue(l1["ok"])
        l2 = core.acquire_pipeline_lease("recognize", "b1", owner="cli", batches_root=self.batches)
        self.assertFalse(l2["ok"])
        self.assertIn("已有任务运行中", l2["error"])
        self.assertTrue(core.release_pipeline_lease(l1["lease"]["token"], batches_root=self.batches))
        l3 = core.acquire_pipeline_lease("retry", "b1", owner="cli", batches_root=self.batches)
        self.assertTrue(l3["ok"])
        core.release_pipeline_lease(l3["lease"]["token"], batches_root=self.batches)

        # 2. ZCode 无头调用契约：强制禁止 yolo，自动降级 plan，且传递 ELECTRON_RUN_AS_NODE=1
        fake_zc_dir = self.work / "ZCode"
        (fake_zc_dir / "resources" / "glm").mkdir(parents=True)
        fake_exe = fake_zc_dir / "ZCode.exe"
        fake_cli = fake_zc_dir / "resources" / "glm" / "zcode.cjs"
        fake_exe.write_bytes(b"MZ")
        fake_cli.write_text("console.log('ok');", encoding="utf-8")
        spec = core.build_zcode_command(
            "诊断测试",
            ROOT,
            mode="yolo",
            cfg={"zcode_exe": str(fake_exe), "zcode_cli": str(fake_cli)},
        )
        self.assertTrue(spec["ok"])
        self.assertEqual(spec["mode"], "plan")
        self.assertEqual(spec["env_patch"].get("ELECTRON_RUN_AS_NODE"), "1")
        self.assertIn("--mode", spec["cmd"])
        self.assertIn("plan", spec["cmd"])
        self.assertNotIn("yolo", spec["cmd"])

        # 3. 脱敏校验（使用合成占位字符串，不含任何真实密钥）
        fake_key = "ark-" + "1234abcd" * 4 + "-test9"
        masked_out = mask_secret(f"key={fake_key}")
        self.assertNotIn("1234abcd1234abcd", masked_out)

        # 4. RunLogger 写入 + Hub /api/machine, /api/runs, /api/logs, /api/check/sync + CLI --hub 后置
        rec_dir = self.batches / "20261128_demo" / "recognized"
        rl = RunLogger(rec_dir, kind="recognize", batch_id="20261128_demo")
        rl.emit("api.grid_fail", "测试事件", level="ERROR", grid_id="g001", page_id="p001")
        rl.finish("warn", stats={"pages": 1})

        state = wb_serve.WorkbenchState(root=ROOT, batches_root=self.batches, port=0)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), wb_serve.make_handler(state))
        port = srv.server_address[1]
        state.port = port
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            base = f"http://127.0.0.1:{port}"
            with urllib.request.urlopen(f"{base}/api/machine", timeout=4) as r:
                mach = json.loads(r.read().decode("utf-8"))
            self.assertTrue(mach["ok"])
            self.assertIn("python", mach)
            self.assertIn("hardware", mach)

            with urllib.request.urlopen(f"{base}/api/runs", timeout=4) as r:
                runs_res = json.loads(r.read().decode("utf-8"))
            self.assertTrue(runs_res["ok"])
            self.assertTrue(any(x["run_id"] == rl.run_id for x in runs_res["runs"]))

            with urllib.request.urlopen(f"{base}/api/logs?run_id={rl.run_id}&level=ERROR", timeout=4) as r:
                logs_res = json.loads(r.read().decode("utf-8"))
            self.assertEqual(logs_res["total"], 1)
            self.assertEqual(logs_res["items"][0]["grid_id"], "g001")

            # CLI --hub 后置到子命令之后 + status/capabilities/doctor/check 回写 Hub last_check
            proc_st = subprocess.run(
                [sys.executable, "-m", "apps.workbench", "status", "--hub", base, "--json"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=15,
            )
            self.assertEqual(proc_st.returncode, 0, proc_st.stderr)
            st_json = json.loads(proc_st.stdout)
            self.assertTrue(st_json["ok"])
            self.assertTrue(st_json["data"]["from_hub"])

            proc_cap = subprocess.run(
                [sys.executable, "-m", "apps.workbench", "capabilities", "--hub", base, "--json"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=15,
            )
            self.assertEqual(proc_cap.returncode, 0, proc_cap.stderr)
            cap_json = json.loads(proc_cap.stdout)
            self.assertTrue(cap_json["ok"])
            cmd_names = {c["name"] for c in cap_json["commands"]}
            self.assertTrue({"run", "retry", "stop", "benchmark", "vlm-test", "doctor", "runs", "logs"}.issubset(cmd_names))

            proc_chk = subprocess.run(
                [sys.executable, "-m", "apps.workbench", "check", "--hub", base, "--json"],
                cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", timeout=20,
            )
            self.assertIn(proc_chk.returncode, (0, 1))
            self.assertIsNotNone(state.last_check)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
