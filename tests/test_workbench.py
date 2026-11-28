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

    def test_multi_agent_providers_prompt_switch_and_session_concurrency(self):
        """验证多 Agent 适配、默认全权、截图开关提示词切换、防竞争冒险单会话串行/多会话并发及密钥脱敏。"""
        from apps.workbench import agent_providers as ap

        # 1. 多 Agent 手动路径覆盖 + 命令契约（ZCode/OpenCode/WorkBuddy/豆包/Antigravity/Custom）
        fake_root = self.work / "fake_agents"
        zc_dir = fake_root / "ZCode"
        (zc_dir / "resources" / "glm").mkdir(parents=True)
        (zc_dir / "resources" / "config" / "provider").mkdir(parents=True)
        (zc_dir / "ZCode.exe").write_bytes(b"MZ")
        (zc_dir / "resources" / "glm" / "zcode.cjs").write_text("", encoding="utf-8")
        (zc_dir / "resources" / "config" / "provider" / "zcode-builtin.json").write_text("{}", encoding="utf-8")

        bins = {}
        for nm in ("opencode.exe", "codebuddy.exe", "trae-cli.exe", "agy.exe"):
            p = fake_root / nm
            p.write_bytes(b"MZ")
            bins[nm] = str(p)

        cfg = {
            "agent_paths": {
                "zcode": str(zc_dir),
                "opencode": bins["opencode.exe"],
                "workbuddy": bins["codebuddy.exe"],
                "doubao": bins["trae-cli.exe"],
                "antigravity": bins["agy.exe"],
            },
            "agent_custom_cmd": "myagent --cwd {cwd} -p {prompt}",
        }
        det = ap.detect_all_agents(cfg, deep=False)
        self.assertTrue(det["ok"])
        self.assertEqual(set(det["available"]), {"zcode", "opencode", "workbuddy", "doubao", "antigravity", "custom"})

        sp_zc = ap.build_agent_command("测试", ROOT, provider="zcode", mode="yolo", cfg=cfg)
        self.assertTrue(sp_zc["ok"])
        self.assertIn("yolo", sp_zc["cmd"])
        self.assertIn("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE", sp_zc["env_patch"])
        self.assertIn("ZCODE_PERSONAL_PROVIDER_CONFIG_FILE", sp_zc["env_patch"])

        sp_oc = ap.build_agent_command("测试", ROOT, provider="opencode", cfg=cfg)
        self.assertEqual(sp_oc["cmd"][1], "run")
        sp_wb = ap.build_agent_command("测试", ROOT, provider="workbuddy", session_id="s1", cfg=cfg)
        self.assertIn("-r", sp_wb["cmd"])
        sp_db = ap.build_agent_command("测试", ROOT, provider="doubao", cfg=cfg)
        self.assertEqual(sp_db["cmd"][1], "run")
        sp_ag = ap.build_agent_command("测试", ROOT, provider="antigravity", cfg=cfg)
        self.assertIn("-p", sp_ag["cmd"])
        sp_cu = ap.build_agent_command("hello", ROOT, provider="custom", cfg=cfg)
        self.assertEqual(sp_cu["cmd"][0], "myagent")

        # 2. 截图开关提示词自动切换 + 两类改动（含 GitHub 自动推送）规范
        p_text_only = ap.build_computer_use_prompt(user_message="修一下报错", surface="review", attached_images=[])
        self.assertIn("capture --target review", p_text_only)
        self.assertIn("git push origin main", p_text_only)
        self.assertNotIn("不要重复截一次同样的整页图", p_text_only)

        fake_shot = self.work / "shot.png"
        fake_shot.write_bytes(b"PNG")
        p_with_shot = ap.build_computer_use_prompt(user_message="看下这个按钮", surface="workbench", attached_images=[str(fake_shot)])
        self.assertIn("不要重复截一次同样的整页图", p_with_shot)
        self.assertNotIn("当前仅提供文字状态快照", p_with_shot)

        # 3. AiSessionManager：同 chat_id 严格单实例串行（防竞争冒险）+ 不同 chat_id 真正并发 + 密钥输入传递但日志/回复严格脱敏
        trace_file = self.work / "trace.jsonl"
        worker_script = self.work / "mock_agent.py"
        worker_script.write_text(
            "import json, os, sys, time\n"
            "tag, delay, trace_p, secret_check = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4]\n"
            "t_start = time.monotonic()\n"
            "with open(trace_p, 'a', encoding='utf-8') as f: f.write(json.dumps({'tag': tag, 'ev': 'start', 't': t_start}) + '\\n')\n"
            "time.sleep(delay)\n"
            "t_end = time.monotonic()\n"
            "with open(trace_p, 'a', encoding='utf-8') as f: f.write(json.dumps({'tag': tag, 'ev': 'end', 't': t_end}) + '\\n')\n"
            "print(json.dumps({'sessionId': 'sess_' + tag, 'response': 'done ' + tag + ' got=' + secret_check, 'usage': {'inputTokens': 10, 'outputTokens': 5}}))\n",
            encoding="utf-8",
        )
        events = []
        mgr = ap.AiSessionManager(ROOT, self.batches, publish_cb=events.append)

        fake_secret = "sk-dummyimportsecret9876543210"
        # 同 chat_id 连发两轮：必须严格串行（t2_start >= t1_end）
        r1 = mgr.submit_turn(
            chat_id="same_chat",
            provider="zcode",
            mode="yolo",
            user_message=f"请帮我导入密钥 {fake_secret}",
            command_override={"ok": True, "provider": "zcode", "provider_label": "MockZCode", "cwd": str(self.work), "cmd": [sys.executable, str(worker_script), "s1", "0.35", str(trace_file), fake_secret], "env_patch": {}},
        )
        r2 = mgr.submit_turn(
            chat_id="same_chat",
            provider="zcode",
            mode="yolo",
            user_message="第二条排队消息",
            command_override={"ok": True, "provider": "zcode", "provider_label": "MockZCode", "cwd": str(self.work), "cmd": [sys.executable, str(worker_script), "s2", "0.25", str(trace_file), "none"], "env_patch": {}},
        )
        self.assertTrue(r1["ok"] and r2["ok"])

        # 不同 chat_id 并发发送：必须真正并行重叠
        c_a = mgr.submit_turn(
            chat_id="conc_a",
            provider="zcode",
            mode="yolo",
            user_message="并发A",
            command_override={"ok": True, "provider": "zcode", "provider_label": "MockZCode", "cwd": str(self.work), "cmd": [sys.executable, str(worker_script), "ca", "0.45", str(trace_file), "none"], "env_patch": {}},
        )
        c_b = mgr.submit_turn(
            chat_id="conc_b",
            provider="zcode",
            mode="yolo",
            user_message="并发B",
            command_override={"ok": True, "provider": "zcode", "provider_label": "MockZCode", "cwd": str(self.work), "cmd": [sys.executable, str(worker_script), "cb", "0.45", str(trace_file), "none"], "env_patch": {}},
        )
        self.assertTrue(c_a["ok"] and c_b["ok"])

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            ss = [mgr.snapshot_session(x) for x in ("same_chat", "conc_a", "conc_b")]
            if all(s and not s["running"] and s["queued"] == 0 and s["status"] == "done" for s in ss):
                break
            time.sleep(0.05)

        rows = [json.loads(ln) for ln in trace_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        times = {(r["tag"], r["ev"]): r["t"] for r in rows}
        # 同一对话严格不重叠（先完成 s1 再开始 s2）
        self.assertGreaterEqual(times[("s2", "start")], times[("s1", "end")] - 0.01)
        # 不同对话真正并发重叠（cb 在 ca 结束前已开始，ca 在 cb 结束前已开始）
        self.assertLess(times[("cb", "start")], times[("ca", "end")])
        self.assertLess(times[("ca", "start")], times[("cb", "end")])

        # 密钥在消息记录与响应中已彻底脱敏，绝不泄露明文
        same_snap = mgr.snapshot_session("same_chat")
        all_txt = json.dumps(same_snap, ensure_ascii=False)
        self.assertNotIn(fake_secret, all_txt)

    def test_config_and_secret_import_export_and_cli_sanitization(self):
        """验证配置/密钥一键导入导出（隔离临时 root 不污染真实 .env）与 CLI 参数脱敏。"""
        from apps.workbench import cli as wb_cli

        # 1. CLI 参数日志脱敏
        san = wb_cli._sanitize_argv(["config", "set-vlm", "--api-key", "sk-testsecret99998888", "--password=mypwd123", "--model", "demo"])
        self.assertNotIn("sk-testsecret99998888", " ".join(san))
        self.assertNotIn("mypwd123", " ".join(san))
        self.assertIn("***", san)

        # 2. 隔离 root 下的 Hub 配置/密钥导入导出
        iso_root = self.work / "iso_root"
        iso_root.mkdir(parents=True)
        iso_batches = iso_root / "batches"
        state = wb_serve.WorkbenchState(root=iso_root, batches_root=iso_batches, port=0)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), wb_serve.make_handler(state))
        port = srv.server_address[1]
        state.port = port
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            base = f"http://127.0.0.1:{port}"
            fake_key = "ark-dummy-test-key-" + "abcd" * 4
            imp_payload = {
                "workbench": {"api_concurrency": 6, "ai_default_mode": "yolo", "ai_default_screenshot": True},
                "vlm": {"provider": "doubao", "base_url": "https://ark.cn-beijing.volces.com/api/v3/chat/completions", "model": "doubao-seed-test", "api_key": fake_key},
                "source": "ai",
            }
            req = urllib.request.Request(
                f"{base}/api/config/import",
                data=json.dumps(imp_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                res = json.loads(r.read().decode("utf-8"))
            self.assertTrue(res["ok"])
            self.assertEqual(res["config"]["api_concurrency"], 6)
            self.assertTrue(res["config"]["ai_default_screenshot"])
            # 响应只含脱敏密钥，不含明文
            self.assertNotIn(fake_key, json.dumps(res, ensure_ascii=False))
            self.assertTrue((iso_root / ".env").is_file())

            # 默认导出（脱敏）不含明文密钥
            with urllib.request.urlopen(f"{base}/api/config/export", timeout=5) as r:
                exp_safe = json.loads(r.read().decode("utf-8"))
            self.assertNotIn("api_key", exp_safe["vlm"])
            self.assertTrue(exp_safe["vlm"].get("api_key_masked"))

            # 迁移导出（include_secrets=1）包含密钥供跨机导入
            with urllib.request.urlopen(f"{base}/api/config/export?include_secrets=1", timeout=5) as r:
                exp_full = json.loads(r.read().decode("utf-8"))
            self.assertEqual(exp_full["vlm"].get("api_key"), fake_key)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_review_8904_isomorphic_crud_page_locks_sse_and_hub_proxy(self):
        """验证 8904 同构 CLI 改字/改选项/删框、页级版本号防竞争冒险、UI 焦点投送 SSE 与 8888 同源代理。"""
        from PIL import Image
        from apps.review import server as review_server

        rec_dir = self.work / "rec_8904"
        (rec_dir / "pages").mkdir(parents=True)
        (rec_dir / "crops").mkdir(parents=True)
        for pid in ("p001", "p002"):
            Image.new("RGB", (800, 600), (255, 255, 255)).save(rec_dir / "pages" / f"{pid}.jpg")
        (rec_dir / "manifest.json").write_text(json.dumps({"run": "test_8904"}), encoding="utf-8")
        results = {
            "status": "completed",
            "pages": [
                {
                    "id": "p001", "name": "paper_001", "image": "pages/p001.jpg", "width": 800, "height": 600,
                    "boxes": [
                        {"cls": 5, "stage": "m3", "box": [40, 80, 360, 180], "item_id": "p001_001"},
                        {"cls": 3, "stage": "m2", "box": [40, 200, 200, 260], "item_id": "p001_002"},
                    ],
                    "items": [
                        {"id": "p001_001", "page_id": "p001", "kind": "fill", "number": 15, "box": [40, 80, 360, 180], "status": "待复核", "issues": ["涂改"], "transcription": {"question_number": "15", "answers": {"无小题号": {"1": "电"}}}},
                        {"id": "p001_002", "page_id": "p001", "kind": "choice", "number": 1, "box": [40, 200, 200, 260], "status": "已识别", "answer": "A"},
                    ],
                },
                {
                    "id": "p002", "name": "paper_002", "image": "pages/p002.jpg", "width": 800, "height": 600,
                    "boxes": [{"cls": 5, "stage": "m3", "box": [50, 50, 300, 150], "item_id": "p002_001"}],
                    "items": [{"id": "p002_001", "page_id": "p002", "kind": "fill", "number": 16, "box": [50, 50, 300, 150], "status": "已识别", "transcription": {"question_number": "16", "answers": {"无小题号": {"1": "2"}}}}],
                },
            ],
            "stats": {},
        }
        (rec_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")

        # 启动临时 Hub + 临时 8904 服务
        hub_state = wb_serve.WorkbenchState(root=ROOT, batches_root=self.batches, port=0)
        hub_srv = ThreadingHTTPServer(("127.0.0.1", 0), wb_serve.make_handler(hub_state))
        hub_port = hub_srv.server_address[1]
        hub_state.port = hub_port
        threading.Thread(target=hub_srv.serve_forever, daemon=True).start()

        rev_srv = review_server.make_server(rec_dir, port=0, host="127.0.0.1", allow_sync_data=False, hub_port=hub_port)
        rev_port = rev_srv.server_address[1]
        threading.Thread(target=rev_srv.serve_forever, daemon=True).start()

        def cli_post(path, payload):
            req = urllib.request.Request(
                f"http://127.0.0.1:{rev_port}{path}",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-AutoGrade-Internal": "cli"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode("utf-8"))

        try:
            # 1. 跨站 Origin 仍被 403 拦截
            bad_req = urllib.request.Request(
                f"http://127.0.0.1:{rev_port}/api/review/set-text",
                data=b'{"page_id":"p001","item_id":"p001_001","text":"x"}',
                headers={"Content-Type": "application/json", "Origin": "http://evil.example", "X-AutoGrade-Internal": "cli"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(bad_req, timeout=5)
            self.assertEqual(ctx.exception.code, 403)

            # 2. 订阅 8904 SSE，验证改字/改选项/删框/焦点投送实时推送
            sse_events = []
            sse_ready = threading.Event()

            def _read_sse():
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{rev_port}/api/events", timeout=8) as resp:
                        sse_ready.set()
                        for raw in resp:
                            ln = raw.decode("utf-8", errors="ignore").strip()
                            if ln.startswith("data:"):
                                sse_events.append(json.loads(ln[5:].strip()))
                                if len(sse_events) >= 3:
                                    break
                except Exception:
                    pass

            th_sse = threading.Thread(target=_read_sse, daemon=True)
            th_sse.start()
            sse_ready.wait(timeout=3.0)
            time.sleep(0.1)

            # 同构改字 + 乐观版本锁冲突检测
            r_txt = cli_post("/api/review/set-text", {"page_id": "p001", "item_id": "p001_001", "text": "电源", "expected_page_rev": 0})
            self.assertTrue(r_txt["ok"])
            self.assertEqual(r_txt["page_rev"], 1)

            with self.assertRaises(urllib.error.HTTPError) as ctx_rev:
                cli_post("/api/review/set-text", {"page_id": "p001", "item_id": "p001_001", "text": "过期写入", "expected_page_rev": 0})
            self.assertEqual(ctx_rev.exception.code, 409)

            # 改选择题 + 投送焦点指令
            r_ch = cli_post("/api/review/set-choice", {"page_id": "p001", "item_id": "p001_002", "answer": "BC"})
            self.assertTrue(r_ch["ok"])
            r_cmd = cli_post("/api/ui/command", {"action": "focus", "page_id": "p001", "item_id": "p001_001", "label": "查看第15题"})
            self.assertTrue(r_cmd["ok"])

            th_sse.join(timeout=3.0)
            ev_types = [e.get("type") for e in sse_events]
            self.assertIn("data_updated", ev_types)
            self.assertIn("ui_command", ev_types)

            # 3. 删框 + ui-state 镜像读写 + /api/hub/agents 同源代理
            r_del = cli_post("/api/review/delete-box", {"page_id": "p002", "item_id": "p002_001"})
            self.assertTrue(r_del["ok"])
            self.assertEqual(r_del["remaining_items"], 0)

            cli_post("/api/ui-state", {"client_id": "c1", "page_id": "p001", "mode": "edit", "selected_item_id": "p001_001"})
            with urllib.request.urlopen(f"http://127.0.0.1:{rev_port}/api/ui-state?client_id=c1", timeout=5) as r:
                ui_res = json.loads(r.read().decode("utf-8"))
            self.assertEqual(ui_res["active"]["page_id"], "p001")
            self.assertEqual(ui_res["active"]["mode"], "edit")

            with urllib.request.urlopen(f"http://127.0.0.1:{rev_port}/api/hub/agents", timeout=5) as r:
                ag_res = json.loads(r.read().decode("utf-8"))
            self.assertTrue(ag_res["ok"])
            self.assertIn("zcode", ag_res["agents"])
        finally:
            rev_srv._stop_event.set()
            rev_srv.shutdown()
            rev_srv.server_close()
            hub_srv.shutdown()
            hub_srv.server_close()

    def test_offscreen_capture_canvas_and_headless_ui_mirror(self):
        """验证离屏高清试卷合成裁切 + 本机无头浏览器离屏 UI 渲染与元素清单导出。"""
        from PIL import Image
        from apps.workbench import capture as cap_mod

        # 1. 审阅试卷高清离屏合成（整页 + 单题特写 + 自定义比例裁切）
        rec_dir = self.work / "rec_cap"
        (rec_dir / "pages").mkdir(parents=True)
        Image.new("RGB", (900, 700), (250, 250, 250)).save(rec_dir / "pages" / "p001.jpg")
        (rec_dir / "results.json").write_text(json.dumps({
            "pages": [{
                "id": "p001", "name": "cap_001", "image": "pages/p001.jpg", "width": 900, "height": 700,
                "boxes": [{"cls": 5, "stage": "m3", "box": [80, 120, 420, 240], "item_id": "p001_001"}],
                "items": [{"id": "p001_001", "page_id": "p001", "kind": "fill", "number": 15, "box": [80, 120, 420, 240], "status": "待复核", "issues": ["涂改"], "transcription": {"question_number": "15", "answers": {"无小题号": {"1": "电能"}}}}],
            }]
        }, ensure_ascii=False), encoding="utf-8")

        c_page = cap_mod.capture_review_canvas(rec_dir, self.batches, page_id="p001", with_elements=True)
        self.assertTrue(c_page["ok"])
        self.assertTrue(Path(c_page["path"]).is_file())
        self.assertTrue(Path(c_page["elements_path"]).is_file())
        self.assertEqual(c_page["elements_count"], 1)

        c_item = cap_mod.capture_review_canvas(rec_dir, self.batches, item_id="p001_001")
        self.assertTrue(c_item["ok"])
        self.assertEqual(c_item["scope"], "item")
        self.assertLess(c_item["width"], 500)

        c_crop = cap_mod.capture_review_canvas(rec_dir, self.batches, page_id="p001", crop="0.0,0.0,0.5,0.5")
        self.assertTrue(c_crop["ok"])
        self.assertEqual(c_crop["scope"], "crop")
        self.assertEqual(c_crop["width"], 450)

        # 2. 若本机存在 Edge/Chrome，实测无头离屏 UI 渲染 + ui_state 镜像 + 元素清单 + selector 精确裁切
        if cap_mod.find_headless_browser():
            state = wb_serve.WorkbenchState(root=ROOT, batches_root=self.batches, port=0)
            srv = ThreadingHTTPServer(("127.0.0.1", 0), wb_serve.make_handler(state))
            port = srv.server_address[1]
            state.port = port
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                state.update_ui_state({"client_id": "mirror_t", "view": "check"})
                res_ui = cap_mod.capture_headless_ui(
                    f"http://127.0.0.1:{port}/",
                    self.batches,
                    target="workbench",
                    mirror_client="mirror_t",
                    with_elements=True,
                )
                self.assertTrue(res_ui["ok"], res_ui.get("error"))
                self.assertTrue(Path(res_ui["path"]).is_file())
                self.assertGreater(res_ui["elements_count"], 5)
                el_ids = {e.get("id") for e in res_ui["elements"]}
                self.assertIn("btnOpenAi", el_ids)

                res_sel = cap_mod.capture_headless_ui(
                    f"http://127.0.0.1:{port}/",
                    self.batches,
                    target="workbench",
                    selector="#btnOpenAi",
                    with_elements=False,
                )
                self.assertTrue(res_sel["ok"], res_sel.get("error"))
                self.assertLess(res_sel["width"], 260)
                self.assertLess(res_sel["height"], 100)
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
