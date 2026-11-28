# -*- coding: utf-8 -*-
"""第一波核心闭环全链路单元测试：
1. exam_schema（规范化、防幻觉动态提示词、答案 PDF 提取、VLM 兜底解析、自动判分、多选默认一半、M1 选择大框汇总、CSV 导出）
2. memory_budget & SquarePacker 内存暂存模式（零 grids 落盘、pop_buffer 释放、低内存自动降级）
3. Review 8904 新接口（单空原地修改、单空/连续多空真子集小框重识别与防非连续校验、按需内存裁切 /asset/crops、人工改判、无题号 M3 框一键清理与备份）
4. Workbench 8888 新接口（双模式切换、训练库目录状态、内存预检、答案 PDF 上传提取与 AI 提示词生成、成绩 CSV 导出）
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.exam_schema import (
    build_dynamic_prompt_hint,
    build_subbox_prompt,
    export_scores_csv,
    extract_answer_source,
    grade_batch,
    grade_page,
    normalize_schema,
    parse_schema_via_vlm,
    save_batch_schema,
    summarize_schema,
)
from engine.memory_budget import run_memory_crop_preflight
from engine.vlm_client import SquarePacker, VlmClient
from apps.review import server as review_srv
from apps.workbench import core as wb_core, server as wb_srv


def _http_json(url: str, method: str = "GET", payload=None, headers: dict | None = None, raw_body: bytes | None = None):
    hdrs = dict(headers or {})
    data = raw_body
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"raw": body}


def _make_sample_pdf(path: Path) -> None:
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 70), "Physics Answer Key\n1. A (4pt)\n2. B (4pt)\n9. BD (6pt, partial 3pt)\n14. (1) 2.5  (2) up", fontsize=14)
    doc.save(str(path))
    doc.close()


class TestExamSchemaAndGrading(unittest.TestCase):
    def test_anti_hallucination_prompts_never_leak_standard_answers(self):
        secret_ans_1 = "SECRET_ANSWER_ALPHA_9981"
        secret_ans_2 = "SECRET_ANSWER_BETA_7742"
        schema = normalize_schema({
            "choices": {"1": "A", "9": "BD"},
            "fills": {
                "14": {
                    "score_per_blank": 2,
                    "sub_questions": {
                        "1": {"blanks": 2, "answers": [secret_ans_1, secret_ans_2]},
                        "2": {"discard_if_figure": True},
                    },
                }
            },
        })
        hint = build_dynamic_prompt_hint([{"id": "p000_005", "kind": "fill", "number": 14}], schema)
        self.assertIn("p000_005", hint)
        self.assertIn("共2空", hint)
        self.assertIn("舍弃", hint)
        self.assertNotIn(secret_ans_1, hint)
        self.assertNotIn(secret_ans_2, hint)

        p_single = build_subbox_prompt("14", [{"sub": "1", "blank": "2"}])
        self.assertIn('{"text":', p_single)
        self.assertNotIn(secret_ans_2, p_single)

        p_multi = build_subbox_prompt("14", [{"sub": "1", "blank": "1"}, {"sub": "1", "blank": "2"}])
        self.assertIn('"1:1"', p_multi)
        self.assertIn('"1:2"', p_multi)
        self.assertIn("印刷小题号", p_multi)
        self.assertNotIn(secret_ans_1, p_multi)
        self.assertNotIn(secret_ans_2, p_multi)

    def test_grading_choices_multi_half_zone_summary_and_fills(self):
        schema = normalize_schema({
            "scoring": {
                "default_choice_single_score": 4,
                "default_choice_multi_score": 6,
                "multi_choice_partial_ratio": 0.5,
                "default_fill_blank_score": 2,
            },
            "choices": {
                "1": "A",       # single 4pt
                "2": "C",       # single 4pt
                "9": "BD",      # multi 6pt, partial 3pt
                "10": "AC",     # multi 6pt, partial 3pt
            },
            "fills": {
                "14": {
                    "1": ["2.5 | 5/2", "$F=ma$"],
                    "2": ["变大"],
                }
            },
        })
        page = {
            "id": "p000",
            "name": "stu_01",
            "boxes": [
                {"cls": 1, "box": [20, 100, 500, 300], "stage": "m1"},
            ],
            "items": [
                {"id": "p000_000", "kind": "identity", "box": [10, 10, 200, 60], "transcription": {"name": "张三", "sid": "202601"}},
                {"id": "p000_001", "kind": "choice", "number": 1, "answer": "A", "box": [40, 120, 120, 150]},
                {"id": "p000_002", "kind": "choice", "number": 2, "answer": "B", "box": [40, 160, 120, 190]},
                {"id": "p000_003", "kind": "choice", "number": 9, "answer": "B", "box": [240, 120, 320, 150]},   # 漏选 -> 一半 3分
                {"id": "p000_004", "kind": "choice", "number": 10, "answer": "AD", "box": [240, 160, 320, 190]}, # 错选 -> 0分
                {
                    "id": "p000_005",
                    "kind": "fill",
                    "number": 14,
                    "box": [40, 350, 500, 600],
                    "transcription": {
                        "question_number": "14",
                        "answers": {
                            "1": {"1": "2.50", "2": "F = ma"},
                            "2": {"1": "看不清"},
                        },
                    },
                },
            ],
        }
        gp = grade_page(page, {}, schema)
        self.assertTrue(gp["has_schema"])
        self.assertEqual(gp["student_name"], "张三")
        self.assertEqual(gp["student_sid"], "202601")
        self.assertEqual(gp["items"]["p000_001"]["status"], "correct")
        self.assertEqual(gp["items"]["p000_001"]["earned"], 4.0)
        self.assertEqual(gp["items"]["p000_002"]["status"], "wrong")
        self.assertEqual(gp["items"]["p000_003"]["status"], "partial")
        self.assertEqual(gp["items"]["p000_003"]["earned"], 3.0)
        self.assertEqual(gp["items"]["p000_004"]["status"], "wrong")
        self.assertEqual(gp["items"]["p000_004"]["earned"], 0.0)

        # M1 选择区大框边缘统计
        self.assertEqual(len(gp["choice_zones"]), 1)
        cz = gp["choice_zones"][0]
        self.assertEqual(cz["single_correct"], 1)
        self.assertEqual(cz["single_wrong"], 1)
        self.assertEqual(cz["multi_partial"], 1)
        self.assertEqual(cz["multi_wrong"], 1)
        self.assertEqual(cz["earned"], 7.0)
        self.assertIn("单选 对1错1", cz["label"])
        self.assertIn("多选 对0漏1错1", cz["label"])

        # 填空题归一化 + 看不清待核
        fb_blanks = gp["items"]["p000_005"]["blanks"]
        self.assertEqual(fb_blanks["1:1"]["status"], "correct")
        self.assertEqual(fb_blanks["1:2"]["status"], "correct")
        self.assertEqual(fb_blanks["2:1"]["status"], "pending")
        self.assertEqual(gp["summary"]["total_earned"], 11.0)
        self.assertEqual(gp["summary"]["total_possible"], 26.0)
        self.assertEqual(gp["summary"]["pending_count"], 1)

        csv_txt = export_scores_csv({"pages": [page]}, {}, schema)
        self.assertTrue(csv_txt.startswith("\ufeff"))
        self.assertIn("张三", csv_txt)
        self.assertIn("11", csv_txt)

    def test_answer_pdf_extract_and_vlm_fallback_parser(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdf_p = root / "key.pdf"
            _make_sample_pdf(pdf_p)
            manifest = extract_answer_source(pdf_p, root / "answer_source")
            self.assertEqual(manifest["page_count"], 1)
            self.assertEqual(len(manifest["rendered_images"]), 1)
            self.assertTrue(Path(manifest["rendered_images"][0]).is_file())
            self.assertIn("Physics Answer Key", manifest["text_preview"])

            def fake_transport(payload):
                return {
                    "choices": [{
                        "message": {
                            "content": json.dumps({
                                "title": "物理期中",
                                "choices": {"1": "A", "9": "BD"},
                                "fills": {"14": {"1": ["2.5"], "2": ["up"]}},
                            })
                        }
                    }]
                }

            parsed = parse_schema_via_vlm(manifest, root=root, transport=fake_transport)
            summ = summarize_schema(parsed)
            self.assertEqual(summ["choice_count"], 2)
            self.assertEqual(summ["multi_count"], 1)
            self.assertEqual(parsed["choices"]["9"]["partial_score"], 3.0)
            self.assertEqual(summ["fill_blank_count"], 2)


class TestMemoryCropsAndBudget(unittest.TestCase):
    def test_preflight_and_in_memory_packer_zero_grid_disk_writes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            img_paths = []
            for k in range(3):
                p = root / f"p{k:03d}.jpg"
                Image.new("RGB", (1200, 1600), (245, 245, 245)).save(p, quality=90)
                img_paths.append(p)

            plan = run_memory_crop_preflight(img_paths, requested_concurrency=4)
            self.assertTrue(plan["ok"])
            self.assertEqual(plan["page_count"], 3)
            self.assertGreater(plan["available_ram_mb"], 0)
            self.assertGreater(plan["est_saved_files"], 0)
            self.assertGreaterEqual(plan["max_inflight_grids"], 2)

            # 强制模拟内存吃紧 -> 自动回退硬盘缓存
            low_plan = run_memory_crop_preflight(img_paths, min_free_mb=99999999.0)
            self.assertEqual(low_plan["recommended_mode"], "disk_fallback")
            self.assertFalse(low_plan["use_memory_crops"])

            # 内存打包器零 grids 落盘 + pop_buffer 立即释放
            out = root / "rec"
            out.mkdir()
            packer = SquarePacker(out, size=600, in_memory=True)
            tile_im = Image.new("RGB", (200, 120), (255, 255, 255))
            packer.add({"id": "p000_001", "kind": "fill", "page_id": "p000", "number": 14, "crop": "crops/p000_001.png"}, tile_im)
            tile_im.close()
            grid = packer.finish()
            self.assertTrue(grid["in_memory"])
            self.assertFalse((out / "grids").exists())
            # grid 必须纯 JSON 可序列化（无 bytes 污染）
            json.dumps(grid)
            raw_bytes = packer.pop_buffer(grid["id"])
            self.assertIsInstance(raw_bytes, bytes)
            self.assertGreater(len(raw_bytes), 100)
            self.assertIsNone(packer.pop_buffer(grid["id"]))

            seen_prompt = {}
            def fake_vlm(req):
                seen_prompt["text"] = req["messages"][0]["content"][0]["text"]
                return {
                    "model": "mock-vlm",
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({"p000_001": {"kind": "fill", "question_number": "14", "answers": {"1": {"1": "3.2"}}}})},
                    }],
                }

            client = VlmClient(out, key="dummy-key", transport=fake_vlm)
            schema = normalize_schema({"fills": {"14": {"1": ["DO_NOT_LEAK_XYZ"]}}})
            rec = client.call(grid, refresh=True, schema=schema, image_bytes=raw_bytes)
            self.assertTrue(rec["ok"])
            self.assertIn("共1空", seen_prompt["text"])
            self.assertNotIn("DO_NOT_LEAK_XYZ", seen_prompt["text"])


class TestReviewSubboxAndUnmatchedAPI(unittest.TestCase):
    def test_subbox_reocr_contiguity_on_the_fly_crop_and_purge(self):
        with tempfile.TemporaryDirectory() as td:
            batch_dir = Path(td) / "batch_demo"
            rec_dir = batch_dir / "recognized"
            (rec_dir / "images").mkdir(parents=True)
            # 注意：故意不创建 crops/ 目录，模拟改卷内存暂存 Crop 模式！
            Image.new("RGB", (800, 1000), (255, 255, 255)).save(rec_dir / "images" / "p000.jpg")
            (rec_dir / "manifest.json").write_text(json.dumps({"schema": 2, "sources": []}), encoding="utf-8")
            save_batch_schema(batch_dir, {
                "choices": {"1": "A", "9": "BD"},
                "fills": {"14": {"1": ["2.5", "变大"], "2": ["向下"]}},
            })
            results = {
                "pages": [{
                    "id": "p000",
                    "name": "sheet_01",
                    "image": "images/p000.jpg",
                    "width": 800,
                    "height": 1000,
                    "boxes": [
                        {"cls": 1, "box": [20, 40, 400, 200], "stage": "m1"},
                        {"cls": 5, "box": [50, 260, 650, 520], "stage": "m3", "item_id": "p000_002"},
                        {"cls": 5, "box": [50, 560, 650, 720], "stage": "m3", "item_id": "p000_003"},
                    ],
                    "items": [
                        {"id": "p000_000", "kind": "choice", "number": 1, "answer": "A", "box": [40, 60, 120, 90]},
                        {"id": "p000_001", "kind": "choice", "number": 9, "answer": "B", "box": [160, 60, 240, 90]},
                        {
                            "id": "p000_002",
                            "kind": "fill",
                            "number": 14,
                            "box": [50, 260, 650, 520],
                            "crop": "crops/p000_002.png",
                            "issues": [],
                            "transcription": {
                                "question_number": "14",
                                "answers": {"1": {"1": "错字", "2": "原字"}, "2": {"1": "向上"}},
                            },
                        },
                        {
                            "id": "p000_003",
                            "kind": "fill",
                            "number": None,
                            "box": [50, 560, 650, 720],
                            "crop": "crops/p000_003.png",
                            "issues": ["大题未配到题号框"],
                            "transcription": {"question_number": "题号看不清", "answers": {"无小题号": {"1": "噪声"}}},
                        },
                    ],
                }],
                "grids": [],
                "stats": {},
            }
            (rec_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")

            captured_reqs = []
            def fake_client_factory(folder):
                def transport(req):
                    captured_reqs.append(req)
                    txt = req["messages"][0]["content"][0]["text"]
                    if '"1:1"' in txt and '"1:2"' in txt:
                        content = json.dumps({"values": {"1:1": "2.5", "1:2": "变大"}})
                    else:
                        content = json.dumps({"text": "2.5"})
                    return {"model": "mock-subbox", "choices": [{"message": {"content": content}}]}
                return VlmClient(folder, key="dummy-key", transport=transport)

            srv = review_srv.make_server(rec_dir, port=0, allow_sync_data=False, client_factory=fake_client_factory, work_mode="grading")
            port = srv.server_port
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            base = f"http://127.0.0.1:{port}"
            cli_hdr = {"X-AutoGrade-Internal": "cli"}
            try:
                # 1) 内存暂存模式按需裁切 /asset/crops/p000_002.png 成功返回 PNG
                with urllib.request.urlopen(f"{base}/asset/crops/p000_002.png", timeout=5) as r:
                    self.assertEqual(r.status, 200)
                    png_data = r.read()
                    self.assertTrue(png_data.startswith(b"\x89PNG"))

                # 2) 单空原地修改不破坏其他空位
                st, r_blank = _http_json(f"{base}/api/review/set-blank", "POST", {
                    "page_id": "p000", "item_id": "p000_002", "sub": "2", "blank": "1", "text": "向下",
                }, headers=cli_hdr)
                self.assertEqual(st, 200)
                self.assertTrue(r_blank["ok"])
                self.assertEqual(r_blank["grading"]["pages"][0]["items"]["p000_002"]["blanks"]["2:1"]["status"], "correct")

                # 3) 单空真子集小框重识别（故意传入略越界坐标，验证被严格裁切在父大框内且不修改原 YOLO 框）
                st, r_sub1 = _http_json(f"{base}/api/review/re-ocr-subbox", "POST", {
                    "page_id": "p000",
                    "item_id": "p000_002",
                    "box": [10, 200, 300, 380],  # 故意超出父框 [50, 260, 650, 520] 左上角
                    "targets": [{"sub": "1", "blank": "1"}],
                }, headers=cli_hdr)
                self.assertEqual(st, 200)
                self.assertEqual(r_sub1["clamped_box"], [50.0, 260.0, 300.0, 380.0])
                self.assertEqual(r_sub1["values"]["1:1"], "2.5")

                # 4) 非连续多空联合识别被严格拒绝 (1:1 和 2:1 跳过了 1:2)
                st_bad, r_bad = _http_json(f"{base}/api/review/re-ocr-subbox", "POST", {
                    "page_id": "p000",
                    "item_id": "p000_002",
                    "box": [60, 270, 500, 500],
                    "targets": [{"sub": "1", "blank": "1"}, {"sub": "2", "blank": "1"}],
                }, headers=cli_hdr)
                self.assertEqual(st_bad, 400)
                self.assertIn("连续", r_bad.get("error", ""))

                # 5) 连续多空联合识别（1:1 + 1:2）成功一次性回填
                st_m, r_m = _http_json(f"{base}/api/review/re-ocr-subbox", "POST", {
                    "page_id": "p000",
                    "item_id": "p000_002",
                    "box": [60, 270, 500, 420],
                    "targets": [{"sub": "1", "blank": "1"}, {"sub": "1", "blank": "2"}],
                }, headers=cli_hdr)
                self.assertEqual(st_m, 200)
                self.assertEqual(r_m["values"]["1:1"], "2.5")
                self.assertEqual(r_m["values"]["1:2"], "变大")
                # 全页此时 1(4分)+9(3分)+14三空(6分) = 13 分
                self.assertEqual(r_m["grading"]["pages"][0]["summary"]["total_earned"], 13.0)

                # 6) 人工改判单个空位
                st_j, r_j = _http_json(f"{base}/api/review/judge-blank", "POST", {
                    "page_id": "p000", "item_id": "p000_002", "slot": "1:2", "status": "wrong",
                }, headers=cli_hdr)
                self.assertEqual(st_j, 200)
                self.assertEqual(r_j["grading"]["pages"][0]["summary"]["total_earned"], 11.0)

                # 7) 一键清理未匹配大题号 M3 填空框（含备份且不误删正常框）
                st_p, r_p = _http_json(f"{base}/api/review/purge-unmatched-fills", "POST", {
                    "scope": "page", "page_id": "p000",
                }, headers=cli_hdr)
                self.assertEqual(st_p, 200)
                self.assertEqual(r_p["removed_count"], 1)
                self.assertEqual(r_p["removed_item_ids"], ["p000_003"])

                # 8) 拆分后的静态模块与共享组件路由 + MIME + 防路径穿越
                with urllib.request.urlopen(f"{base}/static/state.js", timeout=5) as r_js:
                    self.assertEqual(r_js.status, 200)
                    self.assertIn("javascript", (r_js.headers.get("Content-Type") or "").lower())
                    self.assertIn(b"pinnedItemId", r_js.read())
                with urllib.request.urlopen(f"{base}/static/review.css", timeout=5) as r_css:
                    self.assertEqual(r_css.status, 200)
                    self.assertIn("text/css", (r_css.headers.get("Content-Type") or "").lower())
                with urllib.request.urlopen(f"{base}/shared/draggable_drawer.js", timeout=5) as r_sh:
                    self.assertEqual(r_sh.status, 200)
                    self.assertIn("javascript", (r_sh.headers.get("Content-Type") or "").lower())
                    self.assertIn(b"initDraggableDrawer", r_sh.read())
                for bad_url in (f"{base}/static/../server.py", f"{base}/static/%2e%2e/server.py", f"{base}/shared/../review/server.py"):
                    with self.assertRaises(urllib.error.HTTPError) as ctx_err:
                        urllib.request.urlopen(bad_url, timeout=5)
                    self.assertIn(ctx_err.exception.code, (403, 404))
                self.assertTrue((rec_dir / "results.before_purge.json").is_file())
                updated_res = json.loads((rec_dir / "results.json").read_text(encoding="utf-8"))
                rem_ids = [it["id"] for it in updated_res["pages"][0]["items"]]
                self.assertIn("p000_002", rem_ids)
                self.assertNotIn("p000_003", rem_ids)
                self.assertEqual(len(updated_res["pages"][0]["boxes"]), 2)
            finally:
                srv._stop_event.set()
                srv.shutdown()
                srv.server_close()


class TestWorkbenchDualModeAndSchemaAPI(unittest.TestCase):
    def test_workbench_dual_mode_training_db_memory_preflight_and_pdf_upload(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            batches_root = root / "batches"
            src_imgs = root / "src_imgs"
            src_imgs.mkdir(parents=True)
            Image.new("RGB", (900, 1200), (250, 250, 250)).save(src_imgs / "scan1.jpg")
            wb_core.ingest_batch(src_imgs, "物理", batches_root=batches_root, batch_name="demo_exam")

            state = wb_srv.WorkbenchState(root=root, batches_root=batches_root, port=0)
            srv = wb_srv.ThreadingHTTPServer(("127.0.0.1", 0), wb_srv.make_handler(state))
            state.port = srv.server_port
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            base = f"http://127.0.0.1:{state.port}"
            try:
                # 1) 默认改试卷模式 -> 切换模型迭代模式
                st, ov = _http_json(f"{base}/api/overview")
                self.assertEqual(st, 200)
                self.assertEqual(ov["work_mode"], "grading")

                st, cfg_r = _http_json(f"{base}/api/config", "POST", {"work_mode": "iteration"})
                self.assertEqual(st, 200)
                self.assertEqual(cfg_r["work_mode"], "iteration")
                self.assertTrue(cfg_r["training_db"]["allow_sync_data"])

                # 2) 初始化训练数据库目录
                st, db_r = _http_json(f"{base}/api/training-db", "POST", {"ensure_dirs": True})
                self.assertEqual(st, 200)
                self.assertTrue((root / "data" / "m1" / "images").is_dir())
                self.assertTrue((root / "data" / "choice").is_dir())

                # 3) 内存暂存 Crop 预检压测
                st, mp = _http_json(f"{base}/api/memory/preflight", "POST", {})
                self.assertEqual(st, 200)
                self.assertTrue(mp["ok"])
                self.assertEqual(mp["page_count"], 1)

                # 4) 二进制上传答案 PDF -> 提取页面 + 返回默认 AI 帮手提示词
                pdf_p = root / "ans.pdf"
                _make_sample_pdf(pdf_p)
                st, up = _http_json(
                    f"{base}/api/schema/upload-source?filename=ans.pdf",
                    "POST",
                    raw_body=pdf_p.read_bytes(),
                    headers={"Content-Type": "application/pdf"},
                )
                self.assertEqual(st, 200)
                self.assertTrue(up["ok"])
                self.assertEqual(up["manifest"]["page_count"], 1)
                self.assertIn("schema set", up["ai_prompt"])

                # 5) 写入标准答案 + 调整多选题漏选给分比例
                st, sc_r = _http_json(f"{base}/api/schema", "POST", {
                    "schema": {"choices": {"1": "A", "9": "BD"}, "fills": {"14": {"1": ["2.5"]}}},
                })
                self.assertEqual(st, 200)
                self.assertEqual(sc_r["schema"]["choices"]["9"]["partial_score"], 3.0)

                st, sc_r2 = _http_json(f"{base}/api/schema", "POST", {"multi_choice_partial_ratio": 0.0})
                self.assertEqual(st, 200)
                self.assertEqual(sc_r2["schema"]["choices"]["9"]["partial_score"], 0.0)

                # 6) 路径探测（文件夹 & 单张图带引号）+ 剪贴板图片上传 + 剪贴板纯文本答案上传 + 在线去重
                st, ins_dir = _http_json(f"{base}/api/ingest/inspect-path", "POST", {"path": f'"{src_imgs}"'})
                self.assertEqual(st, 200)
                self.assertEqual(ins_dir["kind"], "folder")
                self.assertEqual(ins_dir["image_count"], 1)

                single_img = src_imgs / "scan1.jpg"
                st, ins_file = _http_json(f"{base}/api/ingest/inspect-path", "POST", {"path": f'"{single_img}"'})
                self.assertEqual(st, 200)
                self.assertEqual(ins_file["kind"], "file")
                self.assertEqual(ins_file["image_count"], 1)

                # 单张图片路径直接入库
                st, ing_one = _http_json(f"{base}/api/ingest", "POST", {"source_dir": f'"{single_img}"', "subject": "通用"})
                self.assertEqual(st, 200)
                self.assertEqual(ing_one["batch"]["total_images"], 1)

                # 剪贴板 / 拖拽图片直接 base64 上传入库
                import base64
                from io import BytesIO
                buf = BytesIO()
                Image.new("RGB", (600, 800), (240, 240, 240)).save(buf, format="PNG")
                b64_img = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
                st, up_img = _http_json(f"{base}/api/ingest/upload", "POST", {
                    "files": [{"name": "paste_test.png", "data_b64": b64_img}],
                    "subject": "通用",
                })
                self.assertEqual(st, 200)
                self.assertEqual(up_img["batch"]["total_images"], 1)

                # 剪贴板粘贴答案文本 & 本地答案路径探测
                st, ans_txt = _http_json(f"{base}/api/schema/upload-source", "POST", {
                    "batch_id": up_img["batch"]["batch_id"],
                    "text_content": "1. A (4分)\n2. BD (6分)\n14. (1) 2.5 m/s",
                })
                self.assertEqual(st, 200)
                self.assertIn("2.5 m/s", ans_txt["manifest"]["text_preview"])

                st, ins_ans = _http_json(f"{base}/api/schema/inspect-path", "POST", {"path": f'"{pdf_p}"'})
                self.assertEqual(st, 200)
                self.assertEqual(ins_ans["name"], "ans.pdf")

                # 多标签页同名「主机」在线列表自动去重
                _http_json(f"{base}/api/presence", "POST", {"client_id": "tab_1", "name": "主机", "task": "总览"})
                _http_json(f"{base}/api/presence", "POST", {"client_id": "tab_2", "name": "主机", "task": "总览"})
                st, ov2 = _http_json(f"{base}/api/overview")
                self.assertEqual(st, 200)
                host_online = [u for u in ov2["online_users"] if u["name"] == "主机"]
                self.assertEqual(len(host_online), 1)

                # 拆分后的 8888 静态模块与共享组件路由 + MIME + 防路径穿越
                with urllib.request.urlopen(f"{base}/static/workbench.css", timeout=5) as r_css:
                    self.assertEqual(r_css.status, 200)
                    self.assertIn("text/css", (r_css.headers.get("Content-Type") or "").lower())
                    self.assertIn(b"data-work-mode", r_css.read())
                with urllib.request.urlopen(f"{base}/static/state.js", timeout=5) as r_js:
                    self.assertEqual(r_js.status, 200)
                    self.assertIn("javascript", (r_js.headers.get("Content-Type") or "").lower())
                    self.assertIn(b"collectWbUiState", r_js.read())
                with urllib.request.urlopen(f"{base}/shared/draggable_drawer.js", timeout=5) as r_sh:
                    self.assertEqual(r_sh.status, 200)
                    self.assertIn("javascript", (r_sh.headers.get("Content-Type") or "").lower())
                    self.assertIn(b"initDraggableDrawer", r_sh.read())
                for bad_url in (f"{base}/static/../server.py", f"{base}/static/%2e%2e/server.py", f"{base}/shared/../workbench/server.py"):
                    with self.assertRaises(urllib.error.HTTPError) as ctx_err:
                        urllib.request.urlopen(bad_url, timeout=5)
                    self.assertIn(ctx_err.exception.code, (403, 404))
            finally:
                state._stop_event.set()
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
