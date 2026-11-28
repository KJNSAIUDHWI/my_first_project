# -*- coding: utf-8 -*-
"""试卷结构/标准答案（exam_schema.json）、动态防幻觉提示词、答案 PDF 解析与自动判分引擎。

核心红线：
1. 生成 VLM 整页/局部识别提示词时，只注入题型与空位结构约束，绝不注入任何标准答案文本，防止大模型“作弊脑补”。
2. 多选题默认采用「全对满分、漏选半分（0.5 比例，可配置）、错选 0 分」。
3. 无标准答案时完全可降级运行，自动判分模块优雅返回 has_schema=False。
"""
from __future__ import annotations

import base64
import csv
import io
import json
import math
import re
import shutil
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image

DEFAULT_SCORING = {
    'default_choice_single_score': 4.0,
    'default_choice_multi_score': 6.0,
    'multi_choice_partial_ratio': 0.5,
    'default_fill_blank_score': 2.0,
}


def _now_str() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _to_float(val: Any, default: float) -> float:
    try:
        v = float(val)
        if math.isfinite(v) and v >= 0:
            return round(v, 4)
    except (TypeError, ValueError):
        pass
    return float(default)


def _clean_choice_letters(raw: Any) -> str:
    s = unicodedata.normalize('NFKC', str(raw or '')).upper()
    letters = sorted({ch for ch in s if ch in 'ABCDEFG'})
    return ''.join(letters)


def _clean_qno(raw: Any) -> str:
    s = unicodedata.normalize('NFKC', str(raw or '')).strip()
    m = re.search(r'\d+', s)
    return m.group(0) if m else s


def _clean_sub_key(raw: Any) -> str:
    s = unicodedata.normalize('NFKC', str(raw or '')).strip()
    if not s or s in ('无小题号', 'none', 'None'):
        return '无小题号'
    s = s.strip('()（）【】[] .、')
    return s or '无小题号'


def normalize_schema(raw: dict | None) -> dict:
    """将人工/AI/VLM 产出的试卷结构与标准答案规范化为统一 exam_schema.json 结构。"""
    src = raw if isinstance(raw, dict) else {}
    sc_in = src.get('scoring') if isinstance(src.get('scoring'), dict) else {}
    single_def = _to_float(sc_in.get('default_choice_single_score', src.get('default_choice_single_score')), DEFAULT_SCORING['default_choice_single_score'])
    multi_def = _to_float(sc_in.get('default_choice_multi_score', src.get('default_choice_multi_score')), DEFAULT_SCORING['default_choice_multi_score'])
    partial_ratio = _to_float(sc_in.get('multi_choice_partial_ratio', src.get('multi_choice_partial_ratio')), DEFAULT_SCORING['multi_choice_partial_ratio'])
    if partial_ratio > 1.0:
        partial_ratio = 0.5
    fill_def = _to_float(sc_in.get('default_fill_blank_score', src.get('default_fill_blank_score')), DEFAULT_SCORING['default_fill_blank_score'])

    choices_out: dict[str, dict] = {}
    raw_choices = src.get('choices')
    if isinstance(raw_choices, dict):
        for k, v in raw_choices.items():
            qno = _clean_qno(k)
            if not qno:
                continue
            if isinstance(v, str):
                ans = _clean_choice_letters(v)
                ctype = 'multi' if len(ans) > 1 else 'single'
                score = multi_def if ctype == 'multi' else single_def
                item = {'answer': ans, 'type': ctype, 'score': score}
                if ctype == 'multi':
                    item['partial_score'] = round(score * partial_ratio, 2)
                choices_out[qno] = item
            elif isinstance(v, dict):
                ans = _clean_choice_letters(v.get('answer') or v.get('ans') or '')
                raw_type = str(v.get('type') or '').strip().lower()
                if raw_type in ('multi', 'multiple', '多选', '多选题'):
                    ctype = 'multi'
                elif raw_type in ('single', '单选', '单选题'):
                    ctype = 'single'
                else:
                    ctype = 'multi' if len(ans) > 1 else 'single'
                score = _to_float(v.get('score'), multi_def if ctype == 'multi' else single_def)
                item = {'answer': ans, 'type': ctype, 'score': score}
                if ctype == 'multi':
                    if v.get('partial_score') is not None:
                        item['partial_score'] = _to_float(v.get('partial_score'), round(score * partial_ratio, 2))
                    else:
                        item['partial_score'] = round(score * partial_ratio, 2)
                choices_out[qno] = item
    elif isinstance(raw_choices, list):
        for entry in raw_choices:
            if not isinstance(entry, dict):
                continue
            qno = _clean_qno(entry.get('number') or entry.get('qno') or entry.get('id'))
            if not qno:
                continue
            ans = _clean_choice_letters(entry.get('answer') or entry.get('ans') or '')
            raw_type = str(entry.get('type') or '').strip().lower()
            ctype = 'multi' if (raw_type in ('multi', 'multiple', '多选', '多选题') or len(ans) > 1) else 'single'
            score = _to_float(entry.get('score'), multi_def if ctype == 'multi' else single_def)
            item = {'answer': ans, 'type': ctype, 'score': score}
            if ctype == 'multi':
                item['partial_score'] = _to_float(entry.get('partial_score'), round(score * partial_ratio, 2))
            choices_out[qno] = item

    fills_out: dict[str, dict] = {}
    raw_fills = src.get('fills')
    if isinstance(raw_fills, dict):
        for k, v in raw_fills.items():
            qno = _clean_qno(k)
            if not qno:
                continue
            f_entry: dict[str, Any] = {'score_per_blank': fill_def, 'sub_questions': {}}
            if isinstance(v, dict):
                spb = _to_float(v.get('score_per_blank', v.get('score')), fill_def)
                f_entry['score_per_blank'] = spb
                sub_src = v.get('sub_questions') if isinstance(v.get('sub_questions'), dict) else None
                if sub_src is None:
                    # 兼容简写：{"14": {"1": ["答案1", "答案2"], "2": ["答案3"]}}
                    sub_src = {sk: sv for sk, sv in v.items() if sk not in ('score_per_blank', 'score', 'title', 'note', 'question_number')}
                for sk, sv in (sub_src or {}).items():
                    sub_key = _clean_sub_key(sk)
                    if isinstance(sv, list):
                        ans_list = [str(x if x is not None else '').strip() for x in sv]
                        blanks_n = len(ans_list)
                        f_entry['sub_questions'][sub_key] = {
                            'blanks': blanks_n,
                            'answers': ans_list,
                            'scores': [spb] * blanks_n,
                            'discard_if_figure': False,
                        }
                    elif isinstance(sv, str):
                        sval = sv.strip()
                        if sval in ('舍弃', '含图舍弃'):
                            f_entry['sub_questions'][sub_key] = {
                                'blanks': 0,
                                'answers': [],
                                'scores': [],
                                'discard_if_figure': True,
                            }
                        else:
                            f_entry['sub_questions'][sub_key] = {
                                'blanks': 1,
                                'answers': [sval],
                                'scores': [spb],
                                'discard_if_figure': False,
                            }
                    elif isinstance(sv, dict):
                        discard = bool(sv.get('discard_if_figure') or sv.get('discard') or sv.get('舍弃'))
                        raw_ans = sv.get('answers')
                        if isinstance(raw_ans, dict):
                            ordered = sorted(raw_ans.items(), key=lambda kv: (int(kv[0]) if str(kv[0]).isdigit() else 999, str(kv[0])))
                            ans_list = [str(val if val is not None else '').strip() for _, val in ordered]
                        elif isinstance(raw_ans, list):
                            ans_list = [str(x if x is not None else '').strip() for x in raw_ans]
                        elif raw_ans is not None:
                            ans_list = [str(raw_ans).strip()]
                        else:
                            ans_list = []
                        blanks_n = int(sv.get('blanks') or len(ans_list) or (0 if discard else 1))
                        if not discard and len(ans_list) < blanks_n:
                            ans_list.extend([''] * (blanks_n - len(ans_list)))
                        raw_scores = sv.get('scores')
                        if isinstance(raw_scores, list) and raw_scores:
                            scores_list = [_to_float(x, spb) for x in raw_scores]
                            while len(scores_list) < blanks_n:
                                scores_list.append(spb)
                        else:
                            one_sc = _to_float(sv.get('score'), spb)
                            scores_list = [one_sc] * blanks_n
                        f_entry['sub_questions'][sub_key] = {
                            'blanks': 0 if discard else blanks_n,
                            'answers': [] if discard else ans_list[:blanks_n],
                            'scores': [] if discard else scores_list[:blanks_n],
                            'discard_if_figure': discard,
                        }
            elif isinstance(v, list):
                ans_list = [str(x if x is not None else '').strip() for x in v]
                f_entry['sub_questions']['无小题号'] = {
                    'blanks': len(ans_list),
                    'answers': ans_list,
                    'scores': [fill_def] * len(ans_list),
                    'discard_if_figure': False,
                }
            if f_entry['sub_questions']:
                fills_out[qno] = f_entry

    return {
        'version': 1,
        'title': str(src.get('title') or '').strip(),
        'subject': str(src.get('subject') or '').strip(),
        'source_file': str(src.get('source_file') or '').strip(),
        'updated_at': str(src.get('updated_at') or _now_str()),
        'scoring': {
            'default_choice_single_score': single_def,
            'default_choice_multi_score': multi_def,
            'multi_choice_partial_ratio': partial_ratio,
            'default_fill_blank_score': fill_def,
        },
        'choices': choices_out,
        'fills': fills_out,
    }


def schema_path_for_batch(batch_dir: Path) -> Path:
    return Path(batch_dir) / 'exam_schema.json'


def load_batch_schema(batch_dir: Path | None) -> dict | None:
    if not batch_dir:
        return None
    base = Path(batch_dir)
    for cand in (base / 'exam_schema.json', base.parent / 'exam_schema.json'):
        if cand.is_file():
            try:
                raw = json.loads(cand.read_text(encoding='utf-8'))
                if isinstance(raw, dict):
                    return normalize_schema(raw)
            except Exception:
                return None
    return None


def save_batch_schema(batch_dir: Path, raw_schema: dict) -> dict:
    norm = normalize_schema(raw_schema)
    norm['updated_at'] = _now_str()
    p = schema_path_for_batch(batch_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.json.writing')
    tmp.write_text(json.dumps(norm, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(p)
    return norm


def summarize_schema(schema: dict | None) -> dict:
    if not schema:
        return {
            'configured': False,
            'choice_count': 0,
            'single_count': 0,
            'multi_count': 0,
            'fill_question_count': 0,
            'fill_blank_count': 0,
            'total_possible_score': 0.0,
            'multi_choice_partial_ratio': DEFAULT_SCORING['multi_choice_partial_ratio'],
            'title': '',
        }
    norm = normalize_schema(schema)
    choices = norm.get('choices') or {}
    fills = norm.get('fills') or {}
    single_n = sum(1 for c in choices.values() if c.get('type') != 'multi')
    multi_n = sum(1 for c in choices.values() if c.get('type') == 'multi')
    choice_score = sum(float(c.get('score') or 0) for c in choices.values())
    fill_blanks = 0
    fill_score = 0.0
    for f in fills.values():
        for sq in (f.get('sub_questions') or {}).values():
            if sq.get('discard_if_figure'):
                continue
            b = int(sq.get('blanks') or 0)
            fill_blanks += b
            scores = sq.get('scores') or []
            fill_score += sum(float(s) for s in scores[:b]) if scores else b * float(f.get('score_per_blank') or 2.0)
    return {
        'configured': bool(choices or fills),
        'title': norm.get('title') or '',
        'subject': norm.get('subject') or '',
        'source_file': norm.get('source_file') or '',
        'updated_at': norm.get('updated_at') or '',
        'choice_count': len(choices),
        'single_count': single_n,
        'multi_count': multi_n,
        'fill_question_count': len(fills),
        'fill_blank_count': fill_blanks,
        'total_possible_score': round(choice_score + fill_score, 2),
        'multi_choice_partial_ratio': float((norm.get('scoring') or {}).get('multi_choice_partial_ratio', 0.5)),
    }


def _sub_sort_key(sub: str) -> tuple:
    if sub == '无小题号':
        return (9999, 0, sub)
    parts = re.findall(r'\d+', str(sub))
    if parts:
        return (0, *[int(x) for x in parts[:3]], str(sub))
    return (1, 0, str(sub))


def build_dynamic_prompt_hint(grid_items: list[dict], schema: dict | None) -> str:
    """为当前拼图生成动态结构约束提示词。

    【安全红线】：只输出大题号、小题号、空位数量、含图舍弃标记，绝不包含任何标准答案文本！
    """
    if not schema:
        return ''
    fills = (schema.get('fills') or {}) if isinstance(schema, dict) else {}
    if not fills:
        return ''
    lines = []
    for item in grid_items:
        if item.get('kind') != 'fill':
            continue
        qno = _clean_qno(item.get('number'))
        if not qno or qno not in fills:
            continue
        spec = fills[qno]
        sub_qs = spec.get('sub_questions') or {}
        if not sub_qs:
            continue
        parts = []
        for sk in sorted(sub_qs.keys(), key=_sub_sort_key):
            sq = sub_qs[sk]
            lbl = '无小题号' if sk == '无小题号' else f'小题"{sk}"'
            if sq.get('discard_if_figure'):
                parts.append(f'{lbl}含图需固定输出{{"舍弃":"舍弃"}}')
            else:
                bn = int(sq.get('blanks') or 1)
                parts.append(f'{lbl}共{bn}空')
        if parts:
            lines.append(f'- 图块 {item["id"]}（推测大题号 {qno}）：预期卷面结构为 {"，".join(parts)}。')
    if not lines:
        return ''
    return (
        '\n【试卷结构先验约束（仅提供空位布局参考，不含任何参考答案；若图块卷面与结构不符请以图块真实印刷与笔迹为准）】\n'
        + '\n'.join(lines)
        + '\n请严格按实际卷面手写内容忠实转录，不要凭结构提示编造未作答内容；空白处必须输出空字符串""。'
    )


def build_subbox_prompt(question_number: str, targets: list[dict]) -> str:
    """生成大框真子集小框的精准局部重识别提示词（支持单空与几何连续多空，绝不传入标准答案）。"""
    q_label = f'第 {question_number} 题' if question_number else '当前填空题'
    if len(targets) <= 1:
        t = targets[0] if targets else {'sub': '无小题号', 'blank': '1'}
        sub_desc = '' if t.get('sub') in ('', '无小题号', None) else f'第({t["sub"]})小题 '
        blank_desc = f'第 {t.get("blank") or "1"} 空'
        return (
            f'你是答题卡高精度局部转录工具，不解题、不纠错、不评分。\n'
            f'这张裁剪图来自{q_label}的局部手写区域，只对应唯一的 1 个空位（{sub_desc}{blank_desc}）。\n'
            '请严格遵守以下规则：\n'
            '1. 只转录这一处最终有效的学生手写答案；明确划掉/涂黑删除的旧字不要进入结果，不要与新字拼接。\n'
            '2. 忠实保留学生的错别字、错误数字、正负号和单位，不按常识改正；未写完不续写。\n'
            '3. 公式用合法 LaTeX 并以单个美元符号包围（如 $...$），JSON 字符串内每个反斜杠必须双写（例如 \\\\frac{1}{2}）。\n'
            '4. 若确认空白或仅划掉未重写，返回 ""；若笔迹确实无法辨认，返回 "看不清"。\n'
            '只返回一个合法 JSON 对象：{"text": "最终手写答案"}，不要输出任何额外解释。'
        )

    slot_lines = []
    example_map = {}
    for idx, t in enumerate(targets, 1):
        sub = str(t.get('sub') or '无小题号')
        blank = str(t.get('blank') or '1')
        key = f'{sub}:{blank}'
        sub_txt = '无小题号' if sub == '无小题号' else f'({sub})'
        slot_lines.append(f'{idx}. 键 "{key}" -> {sub_txt} 第 {blank} 空')
        example_map[key] = '...'
    return (
        f'你是答题卡高精度局部转录工具，不解题、不纠错、不评分。\n'
        f'这张局部裁剪图来自{q_label}，按从上到下、从左到右的几何连续顺序，恰好包含以下 {len(targets)} 个已知空位（图内已包含对应印刷小题号）：\n'
        + '\n'.join(slot_lines)
        + '\n请严格遵守以下规则：\n'
        '1. 严格按上述给定键名逐一对应转录每个空位的最终有效手写答案，绝不合并空位、不漏空、不错位，也不新增或遗漏任何键。\n'
        '2. 明确划掉/涂黑删除的旧字不进入结果；忠实保留学生的错别字、错误数字、正负号与单位，绝不按常识纠错。\n'
        '3. 公式用合法 LaTeX 并以单个美元符号包围（如 $...$），JSON 字符串中反斜杠必须双写（如 \\\\frac{a}{b}）。\n'
        '4. 真正空白输出 ""；无法辨认输出 "看不清"；印刷小题号和题干不作为学生答案。\n'
        '只返回一个合法 JSON 对象，格式严格为：'
        + json.dumps({'values': example_map}, ensure_ascii=False)
        + '，不要输出任何额外说明。'
    )


def extract_answer_source(source_path: Path, out_dir: Path, max_pages: int = 16, dpi: int = 170) -> dict:
    """将答案 PDF / 图片 / 文本文件规范化提取到批次 answer_source/ 目录，供 AI 帮手或 VLM 解析。"""
    src = Path(source_path)
    if not src.is_file():
        raise FileNotFoundError(f'答案源文件不存在：{src}')
    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower()
    copied = dest / ('source' + ext)
    if src.resolve() != copied.resolve():
        shutil.copy2(src, copied)

    images: list[str] = []
    text_parts: list[str] = []
    page_count = 0

    if ext == '.pdf':
        import fitz  # PyMuPDF
        doc = fitz.open(str(copied))
        try:
            page_count = len(doc)
            zoom = dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)
            for idx in range(min(page_count, max_pages)):
                page = doc.load_page(idx)
                txt = (page.get_text('text') or '').strip()
                if txt:
                    text_parts.append(f'--- 第 {idx + 1} 页文本 ---\n{txt}')
                pix = page.get_pixmap(matrix=mat, alpha=False)
                im = Image.frombytes('RGB', [pix.width, pix.height], pix.samples)
                if max(im.width, im.height) > 1800:
                    im.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
                img_path = dest / f'page_{idx + 1:02d}.png'
                im.save(img_path, format='PNG', optimize=True)
                im.close()
                images.append(str(img_path))
        finally:
            doc.close()
    elif ext in ('.png', '.jpg', '.jpeg', '.bmp', '.webp'):
        im = Image.open(copied).convert('RGB')
        if max(im.width, im.height) > 2000:
            im.thumbnail((2000, 2000), Image.Resampling.LANCZOS)
        img_path = dest / 'page_01.png'
        im.save(img_path, format='PNG', optimize=True)
        im.close()
        images.append(str(img_path))
        page_count = 1
    elif ext in ('.txt', '.md', '.json', '.csv'):
        txt = copied.read_text(encoding='utf-8-sig', errors='ignore').strip()
        if txt:
            text_parts.append(txt)
        page_count = 1
    else:
        raise ValueError(f'暂不支持该答案文件类型：{ext}（支持 PDF / 图片 / TXT / JSON）')

    full_text = '\n\n'.join(text_parts)
    if full_text:
        (dest / 'extracted_text.txt').write_text(full_text, encoding='utf-8')
    manifest = {
        'source_name': src.name,
        'source_copy': str(copied),
        'ext': ext,
        'page_count': page_count,
        'rendered_images': images,
        'text_length': len(full_text),
        'text_preview': full_text[:1200],
        'extracted_text_file': str(dest / 'extracted_text.txt') if full_text else '',
        'created_at': _now_str(),
    }
    (dest / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def parse_schema_via_vlm(
    manifest: dict,
    *,
    root: Path | None = None,
    subject: str = '',
    title: str = '',
    transport=None,
    timeout: float = 120.0,
) -> dict:
    """无本地 CLI Agent 时的兜底方案：直接调用当前配置的视觉大模型将答案 PDF 页图/文本解析为 exam_schema.json。"""
    from engine.vlm_client import load_vlm_config, _strip_json_fences

    cfg = load_vlm_config(root)
    if not cfg.get('api_key') and transport is None:
        raise ValueError('未配置视觉大模型 API Key，无法直连解析答案（请先配置 VLM Key 或改用本地 AI 帮手）')

    prompt = (
        '你是高精度的试卷标准答案与结构化解析助手。请根据提供的参考答案页面（图片与提取文本），整理输出严格合法的 JSON 对象 exam_schema。\n'
        '返回格式必须严格遵守：\n'
        '{\n'
        '  "title": "试卷标题（没有可留空）",\n'
        '  "subject": "学科名",\n'
        '  "scoring": {"default_choice_single_score": 4, "default_choice_multi_score": 6, "multi_choice_partial_ratio": 0.5, "default_fill_blank_score": 2},\n'
        '  "choices": {\n'
        '    "1": {"answer": "A", "type": "single", "score": 4},\n'
        '    "9": {"answer": "BD", "type": "multi", "score": 6, "partial_score": 3}\n'
        '  },\n'
        '  "fills": {\n'
        '    "13": {\n'
        '      "score_per_blank": 2,\n'
        '      "sub_questions": {\n'
        '        "1": {"blanks": 2, "answers": ["2.5 | 5/2", "变大"], "scores": [2, 2], "discard_if_figure": false}\n'
        '      }\n'
        '    }\n'
        '  }\n'
        '}\n'
        '规则：\n'
        '1. 选择题按卷面题号（纯数字字符串）存入 choices；多个正确字母自动标 type="multi"，partial_score 默认为 score 的一半（若原卷明确写出漏选得分则按原卷）。\n'
        '2. 填空题/实验题按大题号存入 fills；小题号用 "1"、"2"、"4.1" 等（无小题号用 "无小题号"）；同一空若有多个等价答案用 " | " 分隔；公式用 LaTeX `$...$` 且反斜杠双写。\n'
        '3. 如果某小题明显是画图/连线/作图题，设置 "discard_if_figure": true。\n'
        '4. 只返回纯 JSON 对象，不要输出解释文字。'
    )
    if subject or title:
        prompt += f'\n补充上下文：学科={subject or "未知"}，标题={title or "未知"}。'
    txt_file = manifest.get('extracted_text_file')
    if txt_file and Path(txt_file).is_file():
        extracted = Path(txt_file).read_text(encoding='utf-8', errors='ignore')[:12000]
        if extracted.strip():
            prompt += f'\n\n【PDF 提取文本参考】\n{extracted}'

    content_parts: list[dict] = [{'type': 'text', 'text': prompt}]
    for img_path in (manifest.get('rendered_images') or [])[:8]:
        p = Path(img_path)
        if not p.is_file():
            continue
        b64 = base64.b64encode(p.read_bytes()).decode('ascii')
        img_obj: dict[str, Any] = {'url': f'data:image/png;base64,{b64}'}
        if cfg.get('provider') == 'doubao' or 'volces.com' in (cfg.get('endpoint') or ''):
            img_obj['detail'] = 'high'
        content_parts.append({'type': 'image_url', 'image_url': img_obj})

    payload: dict[str, Any] = {
        'model': cfg['model'],
        'stream': False,
        'messages': [{'role': 'user', 'content': content_parts}],
        'response_format': {'type': 'json_object'},
    }
    if cfg.get('provider') == 'doubao' or 'volces.com' in (cfg.get('endpoint') or ''):
        payload['thinking'] = {'type': 'disabled'}
        payload['max_completion_tokens'] = 8192
    else:
        payload['max_tokens'] = 8192

    if transport is not None:
        resp_data = transport(payload)
    else:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(
            cfg['endpoint'],
            body,
            {
                'Authorization': 'Bearer ' + cfg['api_key'],
                'Content-Type': 'application/json',
                'User-Agent': 'AutoGradeWorkbench/2.0',
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_data = json.loads(resp.read().decode('utf-8', errors='replace'))

    raw_text = str((((resp_data.get('choices') or [{}])[0].get('message') or {}).get('content')) or '').strip()
    if not raw_text:
        raise ValueError('视觉大模型未返回有效答案 JSON')
    parsed = json.loads(_strip_json_fences(raw_text))
    if not isinstance(parsed, dict):
        raise ValueError('视觉大模型返回的答案格式不是 JSON 对象')
    if title and not parsed.get('title'):
        parsed['title'] = title
    if subject and not parsed.get('subject'):
        parsed['subject'] = subject
    parsed['source_file'] = manifest.get('source_name') or ''
    return normalize_schema(parsed)


# ==================== 自动判分引擎 ====================


def _normalize_fill_answer(raw: str) -> str:
    s = unicodedata.normalize('NFKC', str(raw or '')).strip()
    if not s:
        return ''
    # 去掉公式外层美元符号以便与纯文本答案比对
    s = s.replace('$', '')
    s = s.replace('×', '*').replace('·', '*').replace('\\times', '*').replace('\\cdot', '*')
    s = s.replace('−', '-').replace('—', '-').replace('–', '-')
    s = s.replace('，', ',').replace('。', '.').replace('；', ';').replace('（', '(').replace('）', ')')
    s = re.sub(r'\s+', '', s)
    return s.lower()


def _answers_equivalent(student_raw: str, std_raw: str) -> bool:
    s_norm = _normalize_fill_answer(student_raw)
    if not s_norm:
        return False
    # 支持 | 或 ； 分隔多组等价参考答案
    variants = [v.strip() for v in re.split(r'\s*\|\s*', str(std_raw or '')) if v.strip()]
    for v in variants:
        v_norm = _normalize_fill_answer(v)
        if not v_norm:
            continue
        if s_norm == v_norm:
            return True
        # 纯数值等价（如 2.50 == 2.5, +3 == 3）
        try:
            sf = float(s_norm)
            vf = float(v_norm)
            if math.isfinite(sf) and math.isfinite(vf) and abs(sf - vf) <= 1e-6:
                return True
        except ValueError:
            pass
    return False


def get_effective_fill_answers(item: dict, fb_rec: dict | None = None) -> dict[str, dict[str, str]]:
    """合并原始 VLM 结构化转录与人工/局部重识别订正（answers_override）。"""
    base_ans = ((item.get('transcription') or {}).get('answers')) if isinstance(item.get('transcription'), dict) else None
    merged: dict[str, dict[str, str]] = {}
    if isinstance(base_ans, dict):
        for sub, blanks in base_ans.items():
            sub_k = str(sub)
            if isinstance(blanks, dict):
                merged[sub_k] = {str(bk): str(bv if bv is not None else '') for bk, bv in blanks.items()}
            else:
                merged[sub_k] = {'1': str(blanks if blanks is not None else '')}
    ov = (fb_rec or {}).get('answers_override')
    if isinstance(ov, dict):
        for sub, blanks in ov.items():
            sub_k = str(sub)
            if isinstance(blanks, dict):
                merged.setdefault(sub_k, {})
                for bk, bv in blanks.items():
                    merged[sub_k][str(bk)] = str(bv if bv is not None else '')
    return merged


def format_answers_as_text(answers: dict[str, dict[str, str]]) -> str:
    """将结构化 answers 字典格式化为可读多行文本（向后兼容旧 correction/导出展示）。"""
    lines = []
    for sub in sorted(answers.keys(), key=_sub_sort_key):
        blanks = answers[sub]
        if not isinstance(blanks, dict):
            lines.append(str(blanks))
            continue
        if blanks.get('舍弃') == '舍弃':
            prefix = '' if sub == '无小题号' else f'({sub}) '
            lines.append(f'{prefix}[含图舍弃]')
            continue
        ordered = sorted(blanks.items(), key=lambda kv: (int(kv[0]) if str(kv[0]).isdigit() else 999, str(kv[0])))
        vals = [str(v).strip() or '[空白]' for _, v in ordered]
        prefix = '' if sub == '无小题号' else f'({sub}) '
        lines.append(prefix + ' | '.join(vals))
    return '\n'.join(lines)


def _find_enclosing_choice_zone(item_box: list[float], choice_zones: list[tuple[int, list[float]]]) -> int | None:
    if not item_box or len(item_box) != 4 or not choice_zones:
        return None
    cx = (item_box[0] + item_box[2]) / 2.0
    cy = (item_box[1] + item_box[3]) / 2.0
    for zi, zb in choice_zones:
        if zb[0] - 8 <= cx <= zb[2] + 8 and zb[1] - 8 <= cy <= zb[3] + 8:
            return zi
    # 若中心略微越界，取交集面积最大的选择区
    best_zi, best_inter = None, 0.0
    for zi, zb in choice_zones:
        ix = max(0.0, min(item_box[2], zb[2]) - max(item_box[0], zb[0]))
        iy = max(0.0, min(item_box[3], zb[3]) - max(item_box[1], zb[1]))
        inter = ix * iy
        if inter > best_inter:
            best_inter = inter
            best_zi = zi
    return best_zi


def grade_page(page: dict, feedback_items: dict | None, schema: dict | None) -> dict:
    """对单页试卷执行自动判分，返回逐题/逐空得分与每个选择题大框（M1 cls=1）的边缘统计。"""
    fb_items = feedback_items if isinstance(feedback_items, dict) else {}
    pid = str(page.get('id') or '')

    # 提取学生姓名与学号
    stu_name = ''
    stu_sid = ''
    for it in page.get('items') or []:
        if it.get('kind') == 'identity':
            rec = fb_items.get(it.get('id') or '') or {}
            tr = it.get('transcription') if isinstance(it.get('transcription'), dict) else {}
            if rec.get('correction'):
                parts = re.split(r'[\s/|,，]+', str(rec['correction']).strip())
                if parts:
                    stu_name = parts[0]
                if len(parts) > 1:
                    stu_sid = parts[1]
            else:
                stu_name = str(tr.get('name') or '').strip()
                stu_sid = str(tr.get('sid') or '').strip()
            break

    if not schema:
        return {
            'has_schema': False,
            'page_id': pid,
            'page_name': str(page.get('name') or pid),
            'student_name': stu_name,
            'student_sid': stu_sid,
            'items': {},
            'choice_zones': [],
            'summary': {
                'choice_earned': 0.0,
                'choice_possible': 0.0,
                'fill_earned': 0.0,
                'fill_possible': 0.0,
                'total_earned': 0.0,
                'total_possible': 0.0,
                'choice_correct': 0,
                'choice_partial': 0,
                'choice_wrong': 0,
                'fill_correct': 0,
                'fill_partial': 0,
                'fill_wrong': 0,
                'pending_count': 0,
            },
        }

    norm = normalize_schema(schema)
    choices_cfg = norm.get('choices') or {}
    fills_cfg = norm.get('fills') or {}
    partial_ratio = float((norm.get('scoring') or {}).get('multi_choice_partial_ratio', 0.5))

    m1_choice_zones: list[tuple[int, list[float]]] = []
    for idx, b in enumerate(page.get('boxes') or []):
        if b.get('cls') == 1 and isinstance(b.get('box'), list) and len(b['box']) == 4:
            m1_choice_zones.append((idx, [float(v) for v in b['box']]))

    zone_stats: dict[int, dict[str, Any]] = {
        zi: {
            'zone_box_index': zi,
            'box': zb,
            'single_total': 0,
            'single_correct': 0,
            'single_wrong': 0,
            'single_earned': 0.0,
            'single_possible': 0.0,
            'multi_total': 0,
            'multi_correct': 0,
            'multi_partial': 0,
            'multi_wrong': 0,
            'multi_earned': 0.0,
            'multi_possible': 0.0,
            'total': 0,
            'correct': 0,
            'partial': 0,
            'wrong': 0,
            'earned': 0.0,
            'possible': 0.0,
        }
        for zi, zb in m1_choice_zones
    }

    item_grades: dict[str, dict] = {}
    choice_earned = choice_possible = 0.0
    fill_earned = fill_possible = 0.0
    c_ok = c_part = c_bad = 0
    f_ok = f_part = f_bad = pending_cnt = 0

    for it in page.get('items') or []:
        iid = str(it.get('id') or '')
        kind = it.get('kind')
        fb_rec = fb_items.get(iid) or {}
        if kind == 'choice':
            qno = _clean_qno(it.get('number'))
            spec = choices_cfg.get(qno)
            if not spec or not spec.get('answer'):
                continue
            std = _clean_choice_letters(spec['answer'])
            ctype = spec.get('type') or ('multi' if len(std) > 1 else 'single')
            full_sc = float(spec.get('score') or 0.0)
            part_sc = float(spec.get('partial_score') if spec.get('partial_score') is not None else round(full_sc * partial_ratio, 2))
            stu_raw = fb_rec.get('answer') if fb_rec.get('answer') is not None else (it.get('answer') or '')
            stu = _clean_choice_letters(stu_raw)

            manual = (fb_rec.get('manual_judges') or {}).get('choice')
            if isinstance(manual, dict) and manual.get('status') in ('correct', 'partial', 'wrong'):
                status = manual['status']
                earned = _to_float(manual.get('score'), full_sc if status == 'correct' else (part_sc if status == 'partial' else 0.0))
                is_manual = True
            else:
                is_manual = False
                if stu == std:
                    status = 'correct'
                    earned = full_sc
                elif ctype == 'multi' and stu and set(stu).issubset(set(std)) and len(stu) < len(std):
                    status = 'partial'
                    earned = part_sc
                else:
                    status = 'wrong'
                    earned = 0.0

            choice_earned += earned
            choice_possible += full_sc
            if status == 'correct':
                c_ok += 1
            elif status == 'partial':
                c_part += 1
            else:
                c_bad += 1

            zi = _find_enclosing_choice_zone(it.get('box') or [], m1_choice_zones)
            if zi is not None and zi in zone_stats:
                zs = zone_stats[zi]
                zs['total'] += 1
                zs['earned'] = round(zs['earned'] + earned, 2)
                zs['possible'] = round(zs['possible'] + full_sc, 2)
                if status == 'correct':
                    zs['correct'] += 1
                elif status == 'partial':
                    zs['partial'] += 1
                else:
                    zs['wrong'] += 1
                if ctype == 'multi':
                    zs['multi_total'] += 1
                    zs['multi_earned'] = round(zs['multi_earned'] + earned, 2)
                    zs['multi_possible'] = round(zs['multi_possible'] + full_sc, 2)
                    if status == 'correct':
                        zs['multi_correct'] += 1
                    elif status == 'partial':
                        zs['multi_partial'] += 1
                    else:
                        zs['multi_wrong'] += 1
                else:
                    zs['single_total'] += 1
                    zs['single_earned'] = round(zs['single_earned'] + earned, 2)
                    zs['single_possible'] = round(zs['single_possible'] + full_sc, 2)
                    if status == 'correct':
                        zs['single_correct'] += 1
                    else:
                        zs['single_wrong'] += 1

            item_grades[iid] = {
                'kind': 'choice',
                'number': qno,
                'type': ctype,
                'student': stu,
                'standard': std,
                'status': status,
                'earned': round(earned, 2),
                'possible': round(full_sc, 2),
                'partial_score': round(part_sc, 2) if ctype == 'multi' else 0.0,
                'manual': is_manual,
                'zone_box_index': zi,
            }

        elif kind == 'fill':
            tr = it.get('transcription') if isinstance(it.get('transcription'), dict) else {}
            qno = _clean_qno(tr.get('question_number') or it.get('number'))
            spec = fills_cfg.get(qno)
            if not spec:
                continue
            sub_specs = spec.get('sub_questions') or {}
            eff_answers = get_effective_fill_answers(it, fb_rec)
            manual_map = fb_rec.get('manual_judges') if isinstance(fb_rec.get('manual_judges'), dict) else {}

            blanks_out: dict[str, dict] = {}
            it_earned = 0.0
            it_possible = 0.0

            # 遍历所有卷面空位和配置空位的并集
            all_subs = sorted(set(eff_answers.keys()) | set(sub_specs.keys()), key=_sub_sort_key)
            for sub in all_subs:
                sq_spec = sub_specs.get(sub)
                stu_blanks = eff_answers.get(sub) or {}
                if sq_spec and sq_spec.get('discard_if_figure'):
                    blanks_out[f'{sub}:舍弃'] = {
                        'sub': sub,
                        'blank': '舍弃',
                        'status': 'discarded',
                        'earned': 0.0,
                        'possible': 0.0,
                        'student': '含图舍弃',
                        'standard': '含图舍弃',
                    }
                    continue
                if stu_blanks.get('舍弃') == '舍弃':
                    blanks_out[f'{sub}:舍弃'] = {
                        'sub': sub,
                        'blank': '舍弃',
                        'status': 'discarded',
                        'earned': 0.0,
                        'possible': 0.0,
                        'student': '含图舍弃',
                        'standard': '',
                    }
                    continue
                max_blanks = max(
                    int((sq_spec or {}).get('blanks') or 0),
                    len(stu_blanks),
                )
                blank_keys = sorted(
                    set(stu_blanks.keys()) | {str(k) for k in range(1, max_blanks + 1)},
                    key=lambda x: (int(x) if str(x).isdigit() else 999, str(x)),
                )
                for bk in blank_keys:
                    slot_key = f'{sub}:{bk}'
                    stu_val = str(stu_blanks.get(bk) or '').strip()
                    idx0 = int(bk) - 1 if str(bk).isdigit() and int(bk) >= 1 else -1
                    std_val = ''
                    full_sc = float(spec.get('score_per_blank') or 2.0)
                    has_std = False
                    if sq_spec and 0 <= idx0 < int(sq_spec.get('blanks') or 0):
                        has_std = True
                        ans_arr = sq_spec.get('answers') or []
                        sc_arr = sq_spec.get('scores') or []
                        std_val = str(ans_arr[idx0]).strip() if idx0 < len(ans_arr) else ''
                        full_sc = float(sc_arr[idx0]) if idx0 < len(sc_arr) else full_sc
                    if not has_std:
                        blanks_out[slot_key] = {
                            'sub': sub,
                            'blank': bk,
                            'status': 'unconfigured',
                            'earned': 0.0,
                            'possible': 0.0,
                            'student': stu_val,
                            'standard': '',
                        }
                        continue

                    manual = manual_map.get(slot_key)
                    if isinstance(manual, dict) and manual.get('status') in ('correct', 'partial', 'wrong'):
                        status = manual['status']
                        default_m = full_sc if status == 'correct' else (round(full_sc * 0.5, 2) if status == 'partial' else 0.0)
                        earned = _to_float(manual.get('score'), default_m)
                        is_manual = True
                    else:
                        is_manual = False
                        if not std_val or std_val.startswith(('开放:', '主观:', '酌情:')):
                            status = 'pending'
                            earned = 0.0
                        elif stu_val == '看不清':
                            status = 'pending'
                            earned = 0.0
                        elif not stu_val:
                            status = 'wrong'
                            earned = 0.0
                        elif _answers_equivalent(stu_val, std_val):
                            status = 'correct'
                            earned = full_sc
                        else:
                            status = 'wrong'
                            earned = 0.0

                    it_earned += earned
                    it_possible += full_sc
                    fill_earned += earned
                    fill_possible += full_sc
                    if status == 'correct':
                        f_ok += 1
                    elif status == 'partial':
                        f_part += 1
                    elif status == 'pending':
                        pending_cnt += 1
                    else:
                        f_bad += 1

                    blanks_out[slot_key] = {
                        'sub': sub,
                        'blank': bk,
                        'status': status,
                        'earned': round(earned, 2),
                        'possible': round(full_sc, 2),
                        'student': stu_val,
                        'standard': std_val,
                        'manual': is_manual,
                    }

            item_grades[iid] = {
                'kind': 'fill',
                'number': qno,
                'earned': round(it_earned, 2),
                'possible': round(it_possible, 2),
                'blanks': blanks_out,
            }

    # 为每个有选择题的 M1 选择区生成紧凑边缘标签
    zones_out = []
    for zi, zs in zone_stats.items():
        if zs['total'] <= 0:
            continue
        segs = []
        if zs['single_total'] > 0:
            segs.append(f"单选 对{zs['single_correct']}错{zs['single_wrong']} ({_fmt_num(zs['single_earned'])}/{_fmt_num(zs['single_possible'])}分)")
        if zs['multi_total'] > 0:
            m_txt = f"多选 对{zs['multi_correct']}"
            if zs['multi_partial'] > 0:
                m_txt += f"漏{zs['multi_partial']}"
            m_txt += f"错{zs['multi_wrong']} ({_fmt_num(zs['multi_earned'])}/{_fmt_num(zs['multi_possible'])}分)"
            segs.append(m_txt)
        zs['label'] = ' · '.join(segs)
        zs['total_label'] = f"区得分 {_fmt_num(zs['earned'])}/{_fmt_num(zs['possible'])}"
        zones_out.append(zs)

    return {
        'has_schema': True,
        'page_id': pid,
        'page_name': str(page.get('name') or pid),
        'student_name': stu_name,
        'student_sid': stu_sid,
        'items': item_grades,
        'choice_zones': zones_out,
        'summary': {
            'choice_earned': round(choice_earned, 2),
            'choice_possible': round(choice_possible, 2),
            'fill_earned': round(fill_earned, 2),
            'fill_possible': round(fill_possible, 2),
            'total_earned': round(choice_earned + fill_earned, 2),
            'total_possible': round(choice_possible + fill_possible, 2),
            'choice_correct': c_ok,
            'choice_partial': c_part,
            'choice_wrong': c_bad,
            'fill_correct': f_ok,
            'fill_partial': f_part,
            'fill_wrong': f_bad,
            'pending_count': pending_cnt,
        },
    }


def _fmt_num(val: float) -> str:
    if abs(val - round(val)) < 1e-6:
        return str(int(round(val)))
    return f'{val:.1f}'.rstrip('0').rstrip('.')


def grade_batch(results: dict, feedback: dict | None, schema: dict | None) -> dict:
    """汇总整批次所有试卷页的自动判分结果。"""
    fb_items = (feedback or {}).get('items') if isinstance(feedback, dict) else {}
    pages_out = [grade_page(p, fb_items, schema) for p in (results.get('pages') or [])]
    totals = [p['summary']['total_earned'] for p in pages_out if p['has_schema']]
    avg_score = round(sum(totals) / len(totals), 2) if totals else 0.0
    max_score = round(max(totals), 2) if totals else 0.0
    min_score = round(min(totals), 2) if totals else 0.0
    return {
        'has_schema': bool(schema),
        'schema_summary': summarize_schema(schema),
        'page_count': len(pages_out),
        'average_score': avg_score,
        'max_score': max_score,
        'min_score': min_score,
        'pages': pages_out,
    }


def export_scores_csv(results: dict, feedback: dict | None, schema: dict | None) -> str:
    """导出带 UTF-8 BOM 的 Excel 友好成绩汇总 CSV 字符串。"""
    graded = grade_batch(results, feedback, schema)
    norm = normalize_schema(schema) if schema else None
    choice_qnos = sorted((norm.get('choices') or {}).keys(), key=lambda x: (int(x) if x.isdigit() else 9999, x)) if norm else []
    fill_qnos = sorted((norm.get('fills') or {}).keys(), key=lambda x: (int(x) if x.isdigit() else 9999, x)) if norm else []

    buf = io.StringIO()
    writer = csv.writer(buf)
    header = ['页码', '试卷文件名', '姓名', '学号', '选择题得分', '填空题得分', '总分', '满分', '待人工确认空数']
    for q in choice_qnos:
        header.append(f'选择{q}')
    for q in fill_qnos:
        header.append(f'大题{q}得分')
    writer.writerow(header)

    for gp in graded['pages']:
        s = gp['summary']
        by_choice: dict[str, dict] = {}
        by_fill: dict[str, float] = {}
        for ig in (gp.get('items') or {}).values():
            if ig.get('kind') == 'choice':
                by_choice[str(ig.get('number') or '')] = ig
            elif ig.get('kind') == 'fill':
                qn = str(ig.get('number') or '')
                by_fill[qn] = round(by_fill.get(qn, 0.0) + float(ig.get('earned') or 0.0), 2)
        row = [
            gp['page_id'],
            gp['page_name'],
            gp['student_name'],
            gp['student_sid'],
            _fmt_num(s['choice_earned']),
            _fmt_num(s['fill_earned']),
            _fmt_num(s['total_earned']),
            _fmt_num(s['total_possible']),
            s['pending_count'],
        ]
        for q in choice_qnos:
            ig = by_choice.get(q)
            if not ig:
                row.append('')
            else:
                mark = '✓' if ig['status'] == 'correct' else ('½' if ig['status'] == 'partial' else '✗')
                row.append(f"{ig['student'] or '-'}({mark}{_fmt_num(ig['earned'])})")
        for q in fill_qnos:
            row.append(_fmt_num(by_fill.get(q, 0.0)) if q in by_fill else '')
        writer.writerow(row)
    return '\ufeff' + buf.getvalue()
