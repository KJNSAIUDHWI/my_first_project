# -*- coding: utf-8 -*-
"""视觉大模型官方 API 价格自动爬取、永久本地缓存与花费计算模块。

设计原则：
1. 首次运行（本地无缓存文件时）自动并发从各模型官网定价文档页抓取官方单价（元 / 百万 tokens），
   解析完成后原子写入本地永久缓存文件（默认 <项目根>/batches/api_pricing_cache.json）。
2. 一旦本地永久缓存存在，后续任意启动与识别均直接读取本地文件，不再重复访问官网；
   仅在用户主动触发刷新（界面「更新官网价格」或 CLI `--refresh`）时才重新抓取。
3. 支持用户为自定义中转站模型保存手动单价（永久保留，刷新官网价格时不被覆盖）。

用法：
    python engine/api_pricing.py                 # 首次自动爬取并永久保存，已有缓存则直接展示
    python engine/api_pricing.py --refresh       # 强制重新爬取官网最新定价并覆盖缓存
    python engine/api_pricing.py --model doubao-seed-2-1-lite-260915
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE_SCHEMA_VERSION = 1

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
)

OFFICIAL_SOURCES = {
    'doubao': {
        'label': '火山方舟（豆包）',
        'url': 'https://www.volcengine.com/docs/82379/1544106',
    },
    'mimo': {
        'label': '小米 MiMo',
        'url': 'https://mimo.mi.com/pricing',
    },
    'deepseek': {
        'label': 'DeepSeek 官方',
        'url': 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing',
    },
    'qwen': {
        'label': '阿里云百炼（通义千问）',
        'url': 'https://help.aliyun.com/zh/model-studio/model-pricing',
    },
    'zhipu': {
        'label': '智谱 BigModel',
        'url': 'https://docs.bigmodel.cn/cn/guide/start/pricing',
    },
}

# 离线兜底参考价（仅当本机断网且从未成功爬取过官网时使用，单位：元 / 百万 tokens）
BUILTIN_FALLBACK_PRICES: dict[str, dict] = {
    'doubao-seed-2.1-lite': {'provider': 'doubao', 'display_name': 'doubao-seed-2.1-lite', 'input_per_m': 0.80, 'output_per_m': 2.70, 'cache_hit_per_m': 0.16},
    'doubao-seed-2.1-pro': {'provider': 'doubao', 'display_name': 'doubao-seed-2.1-pro', 'input_per_m': 6.00, 'output_per_m': 30.00, 'cache_hit_per_m': 1.20},
    'doubao-seed-2.0-lite': {'provider': 'doubao', 'display_name': 'doubao-seed-2.0-lite', 'input_per_m': 0.60, 'output_per_m': 3.60, 'cache_hit_per_m': 0.12},
    'doubao-seed-2.0-pro': {'provider': 'doubao', 'display_name': 'doubao-seed-2.0-pro', 'input_per_m': 3.20, 'output_per_m': 16.00, 'cache_hit_per_m': 0.64},
    'doubao-seed-2.0-mini': {'provider': 'doubao', 'display_name': 'doubao-seed-2.0-mini', 'input_per_m': 0.20, 'output_per_m': 2.00, 'cache_hit_per_m': 0.04},
    'mimo-v2.6-flash': {'provider': 'mimo', 'display_name': 'mimo-v2.6-flash', 'input_per_m': 1.00, 'output_per_m': 2.00, 'cache_hit_per_m': 0.02},
    'mimo-v2.6-pro': {'provider': 'mimo', 'display_name': 'mimo-v2.6-pro', 'input_per_m': 3.00, 'output_per_m': 6.00, 'cache_hit_per_m': 0.025},
    'deepseek-flash': {'provider': 'deepseek', 'display_name': 'deepseek-flash', 'input_per_m': 1.00, 'output_per_m': 4.00, 'cache_hit_per_m': 0.02},
    'deepseek-chat': {'provider': 'deepseek', 'display_name': 'deepseek-chat', 'input_per_m': 1.00, 'output_per_m': 4.00, 'cache_hit_per_m': 0.02},
    'deepseek-v4-pro': {'provider': 'deepseek', 'display_name': 'deepseek-v4-pro', 'input_per_m': 4.50, 'output_per_m': 13.50, 'cache_hit_per_m': 0.15},
    'qwen3-vl-plus': {'provider': 'qwen', 'display_name': 'qwen3-vl-plus', 'input_per_m': 1.00, 'output_per_m': 10.00, 'cache_hit_per_m': None},
    'qwen-vl-max': {'provider': 'qwen', 'display_name': 'qwen-vl-max', 'input_per_m': 1.50, 'output_per_m': 4.50, 'cache_hit_per_m': None},
    'qwen-vl-plus': {'provider': 'qwen', 'display_name': 'qwen-vl-plus', 'input_per_m': 0.75, 'output_per_m': 2.25, 'cache_hit_per_m': None},
    'qwen-vl-ocr': {'provider': 'qwen', 'display_name': 'qwen-vl-ocr', 'input_per_m': 0.30, 'output_per_m': 0.50, 'cache_hit_per_m': None},
    'glm-5.3-flash': {'provider': 'zhipu', 'display_name': 'GLM-5.3-Flash', 'input_per_m': 0.80, 'output_per_m': 2.80, 'cache_hit_per_m': 0.23},
    'glm-4v-flash': {'provider': 'zhipu', 'display_name': 'glm-4v-flash', 'input_per_m': 0.00, 'output_per_m': 0.00, 'cache_hit_per_m': 0.00},
    'glm-4v-plus': {'provider': 'zhipu', 'display_name': 'glm-4v-plus', 'input_per_m': 4.00, 'output_per_m': 4.00, 'cache_hit_per_m': None},
}

_MEM_LOCK = threading.RLock()
_MEM_CACHE: dict[str, tuple[float, dict]] = {}  # str(path) -> (mtime, data)
_CRAWL_LOCK = threading.Lock()


def default_cache_path(root: Path | None = None) -> Path:
    base = Path(root) if root else ROOT
    return base / 'batches' / 'api_pricing_cache.json'


def normalize_model_key(model_id: str) -> str:
    """规范化模型名以便跨文档写法匹配：统一小写，点/下划线转连字符，保留版本号主干。"""
    s = (model_id or '').strip().lower()
    if '/' in s:
        s = s.rsplit('/', 1)[-1]
    s = re.sub(r'[\s_.]+', '-', s)
    s = re.sub(r'-+', '-', s).strip('-')
    return s


def strip_model_date_suffix(norm_key: str) -> str:
    """剥离末尾日期/快照后缀（如 -260915 / -2025-11-20 / -0111 / -latest）。"""
    s = norm_key
    s = re.sub(r'-(?:20\d{2}-\d{2}-\d{2}|20\d{6}|\d{6}|\d{4}|latest|preview)$', '', s)
    return s.strip('-')


def _clean_html_text(raw: str) -> str:
    txt = re.sub(r'<[^>]+>', ' ', raw or '')
    txt = html_lib.unescape(txt)
    txt = re.sub(r'[\u200b\ufeff\xa0\s]+', ' ', txt)
    return txt.strip()


def _parse_cny_number(cell: str) -> float | None:
    s = (cell or '').strip()
    if not s or s in ('-', '--', '—', '无', '不支持'):
        return None
    if '免费' in s:
        return 0.0
    if '$' in s or '美元' in s or 'USD' in s.upper():
        return None
    m = re.search(r'(?:¥|￥)?\s*(\d+(?:\.\d+)?)', s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _fetch_text(url: str, timeout: float = 12.0) -> str:
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', errors='ignore')


def _extract_tr_cells(html_str: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in re.findall(r'<tr\b[^>]*>(.*?)</tr>', html_str, re.S | re.I):
        cells = [_clean_html_text(c) for c in re.findall(r'<t[dh]\b[^>]*>(.*?)</t[dh]>', tr, re.S | re.I)]
        if cells:
            rows.append(cells)
    return rows


def _parse_volcengine_bytedoc_json(page: str, url: str) -> dict[str, dict]:
    """兼容火山引擎文档页返回嵌入文档 JSON（未展开 HTML table）的正常公开页面结构。"""
    out: dict[str, dict] = {}
    pat = re.compile(r'\\"insert\\":\\"((?:[^\\"]|\\.)*)\\"')
    tokens = [
        m.group(1).replace('\\\\n', ' ').replace('\\n', ' ').strip()
        for m in pat.finditer(page)
    ]
    tokens = [t for t in tokens if t and t != '*']
    if not tokens:
        return out
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        m_id = re.match(r'^((?:doubao|deepseek|kimi|glm|qwen)[a-zA-Z0-9._-]+)$', tok, re.I)
        if m_id and i + 2 < n and '输入长度' in tokens[i + 1]:
            disp = m_id.group(1)
            norm = normalize_model_key(disp)
            if norm not in out:
                # 标准在线推理表格式：[模型, 输入长度, 输入(非音频), 输入(音频), 缓存存储, 缓存命中(非音频), 缓存命中(音频), 输出]
                # 收集紧随其后的数值/占位单元格直到下一个模型名或条件行
                vals: list[str] = []
                j = i + 2
                while j < min(n, i + 9):
                    nxt = tokens[j]
                    if re.match(r'^(?:doubao|deepseek|kimi|glm|qwen)[a-zA-Z0-9._-]+$', nxt, re.I) or '输入长度' in nxt or '模型名称' in nxt:
                        break
                    vals.append(nxt)
                    j += 1
                if len(vals) >= 6:
                    p_in = _parse_cny_number(vals[0])
                    p_cache = _parse_cny_number(vals[3]) if len(vals) >= 6 else None
                    p_out = _parse_cny_number(vals[-1])
                    if p_in is not None and p_out is not None and (p_in > 0 or p_out == 0):
                        out[norm] = {
                            'provider': 'doubao',
                            'display_name': disp,
                            'input_per_m': p_in,
                            'output_per_m': p_out,
                            'cache_hit_per_m': p_cache,
                            'currency': 'CNY',
                            'source': 'official_crawl',
                            'source_url': url,
                        }
        i += 1
    return out


def crawl_volcengine(timeout: float = 14.0) -> dict[str, dict]:
    """抓取火山方舟官方模型定价页（标准在线推理首个基础阶梯）。"""
    url = OFFICIAL_SOURCES['doubao']['url']
    page = _fetch_text(url, timeout=timeout)
    if len(page) < 20000 and 'doubao' not in page.lower():
        raise RuntimeError('官网页面暂受访问频率限制，已自动回退参考单价')
    rows = _extract_tr_cells(page)
    out: dict[str, dict] = {}
    col_in = col_out = col_cache = -1
    in_standard_table = False

    for cells in rows:
        joined = ' '.join(cells)
        if '模型名称' in cells[0] and ('输入' in joined and '输出' in joined):
            col_in = col_out = col_cache = -1
            for idx, h in enumerate(cells):
                is_audio_only = ('音频' in h and '非音频' not in h)
                if col_in < 0 and '输入' in h and '条件' not in h and '长度' not in h and not is_audio_only and '图' not in h:
                    col_in = idx
                if col_cache < 0 and '缓存命中' in h and not is_audio_only:
                    col_cache = idx
                if '输出' in h and not is_audio_only and '图' not in h:
                    col_out = idx
            # 优先识别标准在线推理表（含 M token 或 百万）；后出现的加急/离线表因 first-seen-wins 不会覆盖
            in_standard_table = (col_in >= 0 and col_out >= 0 and ('token' in joined.lower() or '百万' in joined))
            continue

        if not in_standard_table or len(cells) <= max(col_in, col_out):
            continue
        raw_name = cells[0].strip()
        m_id = re.search(r'((?:doubao|deepseek|kimi|glm|qwen)[a-zA-Z0-9._-]+)', raw_name, re.I)
        if not m_id:
            continue
        disp = m_id.group(1).strip('.,;:')
        norm = normalize_model_key(disp)
        if norm in out:
            continue
        p_in = _parse_cny_number(cells[col_in])
        p_out = _parse_cny_number(cells[col_out])
        p_cache = _parse_cny_number(cells[col_cache]) if 0 <= col_cache < len(cells) else None
        if p_in is None or p_out is None or (p_in == 0 and p_out > 0):
            continue
        out[norm] = {
            'provider': 'doubao',
            'display_name': disp,
            'input_per_m': p_in,
            'output_per_m': p_out,
            'cache_hit_per_m': p_cache,
            'currency': 'CNY',
            'source': 'official_crawl',
            'source_url': url,
        }
    if not out:
        out = _parse_volcengine_bytedoc_json(page, url)
    return out


def crawl_mimo(timeout: float = 14.0) -> dict[str, dict]:
    """抓取小米 MiMo 官网按量计费国内人民币定价表。"""
    url = OFFICIAL_SOURCES['mimo']['url']
    page = _fetch_text(url, timeout=timeout)
    m_js = re.search(r'src="(/doc-data/site-data\.[^"]+\.js)"', page)
    if not m_js:
        raise RuntimeError('未找到 MiMo 文档索引脚本')
    site_js = _fetch_text('https://mimo.mi.com' + m_js.group(1), timeout=timeout)
    idx = site_js.find('pay-as-you-go')
    content_ref = ''
    while idx != -1:
        m_ref = re.search(r'"contentRef":"([^"]+)"', site_js[idx:idx + 450])
        if m_ref:
            content_ref = m_ref.group(1)
            break
        idx = site_js.find('pay-as-you-go', idx + 1)
    if not content_ref:
        raise RuntimeError('未找到 MiMo 按量计费内容引用')

    payload = json.loads(_fetch_text('https://mimo.mi.com/doc-data/' + content_ref, timeout=timeout))
    doc_html = str(payload.get('html') or '')
    out: dict[str, dict] = {}

    for tbl in re.findall(r'<table\b[^>]*>.*?</table>', doc_html, re.S | re.I):
        rows = _extract_tr_cells(tbl)
        if not rows or '模型名称' not in ' '.join(rows[0]):
            continue
        # 仅取人民币（¥）国内表，不取海外美元（$）表
        tbl_text = ' '.join(' '.join(r) for r in rows)
        if '¥' not in tbl_text and '￥' not in tbl_text:
            continue
        mode = 'realtime'
        for r in rows[1:]:
            if not r:
                continue
            if r[0] in ('实时推理', '批量推理'):
                mode = 'realtime' if r[0] == '实时推理' else 'batch'
                r = r[1:]
            if mode != 'realtime' or len(r) < 4:
                continue
            disp = r[0].strip()
            if not re.match(r'^mimo[-a-z0-9.]+$', disp, re.I):
                continue
            p_cache = _parse_cny_number(r[1])
            p_in = _parse_cny_number(r[2])
            p_out = _parse_cny_number(r[3])
            if p_in is None or p_out is None:
                continue
            norm = normalize_model_key(disp)
            if norm not in out:
                out[norm] = {
                    'provider': 'mimo',
                    'display_name': disp,
                    'input_per_m': p_in,
                    'output_per_m': p_out,
                    'cache_hit_per_m': p_cache,
                    'currency': 'CNY',
                    'source': 'official_crawl',
                    'source_url': url,
                }
    return out


def crawl_deepseek(timeout: float = 12.0) -> dict[str, dict]:
    """抓取 DeepSeek 官方中文定价文档页。"""
    url = OFFICIAL_SOURCES['deepseek']['url']
    page = _fetch_text(url, timeout=timeout)
    out: dict[str, dict] = {}
    for tbl in re.findall(r'<table\b[^>]*>.*?</table>', page, re.S | re.I):
        rows = _extract_tr_cells(tbl)
        if not rows or rows[0][0] != '模型':
            continue
        model_cols = [re.sub(r'\s*\(\d+\)\s*', '', c).strip() for c in rows[0][1:]]
        if not model_cols:
            continue
        in_prices: list[float | None] = [None] * len(model_cols)
        cache_prices: list[float | None] = [None] * len(model_cols)
        out_prices: list[float | None] = [None] * len(model_cols)
        cur_metric = ''
        for r in rows[1:]:
            joined = ' '.join(r)
            if '缓存命中' in joined:
                cur_metric = 'cache'
            elif '缓存未命中' in joined:
                cur_metric = 'input'
            elif '百万tokens输出' in joined.replace(' ', '').lower() or '百万token输出' in joined.replace(' ', '').lower():
                cur_metric = 'output'
            elif '高峰时段' in joined:
                # 仅取首档标准/空闲价，跳过高峰加倍价
                continue
            if not cur_metric:
                continue
            vals = r[-len(model_cols):]
            if len(vals) != len(model_cols):
                continue
            for i, cell in enumerate(vals):
                num = _parse_cny_number(cell)
                if num is None:
                    continue
                if cur_metric == 'input' and in_prices[i] is None:
                    in_prices[i] = num
                elif cur_metric == 'cache' and cache_prices[i] is None:
                    cache_prices[i] = num
                elif cur_metric == 'output' and out_prices[i] is None:
                    out_prices[i] = num
        for i, m_name in enumerate(model_cols):
            if in_prices[i] is not None and out_prices[i] is not None:
                norm = normalize_model_key(m_name)
                out[norm] = {
                    'provider': 'deepseek',
                    'display_name': m_name,
                    'input_per_m': in_prices[i],
                    'output_per_m': out_prices[i],
                    'cache_hit_per_m': cache_prices[i],
                    'currency': 'CNY',
                    'source': 'official_crawl',
                    'source_url': url,
                }
    # 兼容官方经典别名 deepseek-chat / deepseek-reasoner
    if 'deepseek-flash' in out and 'deepseek-chat' not in out:
        out['deepseek-chat'] = {**out['deepseek-flash'], 'display_name': 'deepseek-chat (alias)'}
    return out


def crawl_aliyun(timeout: float = 14.0) -> dict[str, dict]:
    """抓取阿里云百炼（通义千问）官方定价页国内主价格。"""
    url = OFFICIAL_SOURCES['qwen']['url']
    page = _fetch_text(url, timeout=timeout)
    out: dict[str, dict] = {}
    for tbl in re.findall(r'<table\b[^>]*>.*?</table>', page, re.S | re.I):
        rows = _extract_tr_cells(tbl)
        if len(rows) < 2:
            continue
        hdr = rows[0]
        hdr_join = ' '.join(hdr)
        if '输入单价' not in hdr_join or '输出单价' not in hdr_join:
            continue
        if '每百万' not in hdr_join and '百万' not in hdr_join:
            continue
        col_in = next((i for i, h in enumerate(hdr) if '输入单价' in h), -1)
        col_out = next((i for i, h in enumerate(hdr) if '输出单价' in h), -1)
        col_scope = next((i for i, h in enumerate(hdr) if '部署范围' in h or '地域' in h), -1)
        if col_in < 0 or col_out < 0:
            continue
        for r in rows[1:]:
            if len(r) <= max(col_in, col_out):
                continue
            if 0 <= col_scope < len(r) and any(k in r[col_scope] for k in ('国际', '欧盟', '中国香港', '新加坡', '弗吉尼亚')):
                continue
            m_id = re.search(r'\b(qwen[a-z0-9._-]+|qvq[a-z0-9._-]+)\b', r[0], re.I)
            if not m_id:
                continue
            disp = m_id.group(1)
            norm = normalize_model_key(disp)
            if norm in out:
                continue
            p_in = _parse_cny_number(r[col_in])
            p_out = _parse_cny_number(r[col_out])
            if p_in is None or p_out is None:
                continue
            out[norm] = {
                'provider': 'qwen',
                'display_name': disp,
                'input_per_m': p_in,
                'output_per_m': p_out,
                'cache_hit_per_m': None,
                'currency': 'CNY',
                'source': 'official_crawl',
                'source_url': url,
            }
    return out


def crawl_zhipu(timeout: float = 14.0) -> dict[str, dict]:
    """抓取智谱 BigModel 官方定价文档页。"""
    url = OFFICIAL_SOURCES['zhipu']['url']
    page = _fetch_text(url, timeout=timeout)
    rows = _extract_tr_cells(page)
    out: dict[str, dict] = {}
    col_in = col_out = col_cache = -1

    for cells in rows:
        joined = ' '.join(cells)
        if '模型名称' in cells[0] and '输入单价' in joined and '输出单价' in joined:
            col_in = next((i for i, h in enumerate(cells) if '输入单价' in h), -1)
            col_out = next((i for i, h in enumerate(cells) if '输出单价' in h), -1)
            col_cache = next((i for i, h in enumerate(cells) if '缓存命中' in h), -1)
            continue
        if col_in < 0 or col_out < 0 or len(cells) <= max(col_in, col_out):
            continue
        m_id = re.search(r'\b(glm[-a-z0-9.]+)\b', cells[0], re.I)
        if not m_id:
            continue
        disp = m_id.group(1)
        norm = normalize_model_key(disp)
        if norm in out:
            continue
        p_in = _parse_cny_number(cells[col_in])
        p_out = _parse_cny_number(cells[col_out])
        p_cache = _parse_cny_number(cells[col_cache]) if 0 <= col_cache < len(cells) else None
        if p_in is None or p_out is None:
            continue
        out[norm] = {
            'provider': 'zhipu',
            'display_name': disp,
            'input_per_m': p_in,
            'output_per_m': p_out,
            'cache_hit_per_m': p_cache,
            'currency': 'CNY',
            'source': 'official_crawl',
            'source_url': url,
        }
    return out


_CRAWLERS = {
    'doubao': crawl_volcengine,
    'mimo': crawl_mimo,
    'deepseek': crawl_deepseek,
    'qwen': crawl_aliyun,
    'zhipu': crawl_zhipu,
}


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f'.json.writing.{os.getpid()}.{threading.get_ident()}')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    last_exc = None
    for _ in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last_exc = exc
            time.sleep(0.04)
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    if last_exc:
        raise last_exc


def load_pricing_cache(root: Path | None = None, cache_path: Path | None = None) -> dict | None:
    path = Path(cache_path) if cache_path else default_cache_path(root)
    key = str(path.resolve())
    if not path.is_file():
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _MEM_LOCK:
        cached = _MEM_CACHE.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if isinstance(data, dict):
            with _MEM_LOCK:
                _MEM_CACHE[key] = (mtime, data)
            return data
    except Exception:
        return None
    return None


def crawl_and_save_pricing(
    root: Path | None = None,
    cache_path: Path | None = None,
    providers: list[str] | None = None,
    timeout: float = 14.0,
) -> dict:
    """并发抓取各官网定价文档页，合并既有手动覆盖项后永久保存到本地 JSON。"""
    path = Path(cache_path) if cache_path else default_cache_path(root)
    with _CRAWL_LOCK:
        existing = load_pricing_cache(root, path) or {}
        models: dict[str, dict] = dict(existing.get('models') or {})
        provider_status: dict[str, dict] = dict(existing.get('providers') or {})
        custom_overrides: dict[str, dict] = dict(existing.get('custom_overrides') or {})

        target_provs = [p for p in (providers or list(_CRAWLERS.keys())) if p in _CRAWLERS]
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with ThreadPoolExecutor(max_workers=max(1, len(target_provs))) as pool:
            futs = {pool.submit(_CRAWLERS[p], timeout): p for p in target_provs}
            for fut in as_completed(futs):
                prov = futs[fut]
                src_info = OFFICIAL_SOURCES[prov]
                try:
                    found = fut.result()
                    if found:
                        models.update(found)
                        provider_status[prov] = {
                            'ok': True,
                            'label': src_info['label'],
                            'url': src_info['url'],
                            'count': len(found),
                            'crawled_at': now_str,
                            'error': '',
                        }
                    else:
                        provider_status[prov] = {
                            'ok': False,
                            'label': src_info['label'],
                            'url': src_info['url'],
                            'count': 0,
                            'crawled_at': now_str,
                            'error': '页面未解析到定价行',
                        }
                except Exception as exc:
                    provider_status[prov] = {
                        'ok': False,
                        'label': src_info['label'],
                        'url': src_info['url'],
                        'count': 0,
                        'crawled_at': now_str,
                        'error': str(exc)[:220],
                    }

        payload = {
            'schema_version': CACHE_SCHEMA_VERSION,
            'permanent': True,
            'crawled_at': now_str,
            'unit': 'CNY per 1,000,000 tokens',
            'providers': provider_status,
            'models': models,
            'custom_overrides': custom_overrides,
        }
        _atomic_write_json(path, payload)
        try:
            mtime = path.stat().st_mtime
            with _MEM_LOCK:
                _MEM_CACHE[str(path.resolve())] = (mtime, payload)
        except OSError:
            pass
        return payload


def ensure_pricing_cache(root: Path | None = None, cache_path: Path | None = None, force: bool = False) -> dict:
    """核心入口：已有本地永久缓存时直接返回（零网络请求）；首次无缓存或强制刷新时才爬取官网。"""
    path = Path(cache_path) if cache_path else default_cache_path(root)
    if not force:
        existing = load_pricing_cache(root, path)
        if existing and existing.get('models'):
            return existing
    return crawl_and_save_pricing(root=root, cache_path=path)


def save_custom_price_override(
    model_id: str,
    input_per_m: float,
    output_per_m: float,
    cache_hit_per_m: float | None = None,
    root: Path | None = None,
    cache_path: Path | None = None,
) -> dict:
    """保存用户自定义模型单价（如第三方中转站），永久保留在本地缓存中。"""
    norm = normalize_model_key(model_id)
    if not norm:
        raise ValueError('模型名称不能为空')
    if input_per_m < 0 or output_per_m < 0:
        raise ValueError('单价不能为负数')
    path = Path(cache_path) if cache_path else default_cache_path(root)
    with _CRAWL_LOCK:
        data = load_pricing_cache(root, path) or {
            'schema_version': CACHE_SCHEMA_VERSION,
            'permanent': True,
            'crawled_at': '',
            'unit': 'CNY per 1,000,000 tokens',
            'providers': {},
            'models': {},
            'custom_overrides': {},
        }
        overrides = dict(data.get('custom_overrides') or {})
        overrides[norm] = {
            'provider': 'custom',
            'display_name': model_id.strip(),
            'input_per_m': round(float(input_per_m), 6),
            'output_per_m': round(float(output_per_m), 6),
            'cache_hit_per_m': round(float(cache_hit_per_m), 6) if cache_hit_per_m is not None else None,
            'currency': 'CNY',
            'source': 'manual',
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        data['custom_overrides'] = overrides
        _atomic_write_json(path, data)
        try:
            mtime = path.stat().st_mtime
            with _MEM_LOCK:
                _MEM_CACHE[str(path.resolve())] = (mtime, data)
        except OSError:
            pass
        return overrides[norm]


def _match_in_table(norm: str, stripped: str, table: dict[str, dict]) -> dict | None:
    if not table:
        return None
    if norm in table:
        return table[norm]
    if stripped and stripped in table:
        return table[stripped]
    # 前缀最长匹配（例如 doubao-seed-2-1-lite-260915 -> doubao-seed-2-1-lite）
    best_key = ''
    for k in table:
        nk = normalize_model_key(k)
        if stripped and (stripped == nk or stripped.startswith(nk + '-') or nk.startswith(stripped + '-')):
            if len(nk) > len(best_key):
                best_key = k
        elif norm.startswith(nk + '-'):
            if len(nk) > len(best_key):
                best_key = k
    return table[best_key] if best_key else None


def lookup_model_price(
    model_id: str,
    provider: str = '',
    root: Path | None = None,
    cache_path: Path | None = None,
) -> dict | None:
    """按模型名查找定价：优先手动自定义 > 官网永久缓存 > 离线内置参考。"""
    norm = normalize_model_key(model_id)
    if not norm:
        return None
    stripped = strip_model_date_suffix(norm)
    cache = load_pricing_cache(root, cache_path) or {}
    crawled_at = str(cache.get('crawled_at') or '')

    # 1. 手动覆盖优先
    overrides = {normalize_model_key(k): v for k, v in (cache.get('custom_overrides') or {}).items()}
    hit = _match_in_table(norm, stripped, overrides)
    if hit:
        return {**hit, 'crawled_at': hit.get('updated_at') or crawled_at, 'subscription': provider == 'mimo_token'}

    # 2. 官网永久缓存
    models = {normalize_model_key(k): v for k, v in (cache.get('models') or {}).items()}
    hit = _match_in_table(norm, stripped, models)
    if hit:
        return {**hit, 'crawled_at': crawled_at, 'subscription': provider == 'mimo_token'}

    # 3. 离线兜底参考价
    fb = {normalize_model_key(k): v for k, v in BUILTIN_FALLBACK_PRICES.items()}
    hit = _match_in_table(norm, stripped, fb)
    if hit:
        src_url = OFFICIAL_SOURCES.get(hit.get('provider', ''), {}).get('url', '')
        return {
            **hit,
            'currency': 'CNY',
            'source': 'builtin_fallback',
            'source_url': src_url,
            'crawled_at': '',
            'subscription': provider == 'mimo_token',
        }
    return None


def format_cny(amount: float) -> str:
    val = max(0.0, float(amount))
    if val == 0:
        return '¥0.00'
    if val < 0.001:
        return f'¥{val:.4f}'
    if val < 0.1:
        return f'¥{val:.3f}'
    if val < 10:
        return f'¥{val:.2f}'
    return f'¥{val:.2f}'


def estimate_cost(
    model_id: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    provider: str = '',
    pages: int = 0,
    reused: int = 0,
    api_done: int = 0,
    root: Path | None = None,
    cache_path: Path | None = None,
) -> dict:
    """根据实际消耗 token 数与本地缓存官方单价计算精确 API 花费。"""
    p_tok = max(0, int(prompt_tokens or 0))
    c_tok = max(0, int(completion_tokens or 0))
    ch_tok = max(0, min(p_tok, int(cached_tokens or 0)))
    uncached_p = max(0, p_tok - ch_tok)

    price = lookup_model_price(model_id, provider=provider, root=root, cache_path=cache_path)
    if not price:
        return {
            'found': False,
            'currency': 'CNY',
            'symbol': '¥',
            'cost': None,
            'cost_display': '',
            'prompt_tokens': p_tok,
            'cached_tokens': ch_tok,
            'completion_tokens': c_tok,
            'total_tokens': p_tok + c_tok,
            'matched_model': '',
            'source': 'unknown',
            'subscription': provider == 'mimo_token',
            'all_reused': bool(api_done > 0 and reused >= api_done),
        }

    in_rate = float(price['input_per_m'])
    out_rate = float(price['output_per_m'])
    cache_rate = float(price['cache_hit_per_m']) if price.get('cache_hit_per_m') is not None else in_rate

    cost = (uncached_p * in_rate + ch_tok * cache_rate + c_tok * out_rate) / 1_000_000.0
    per_page = (cost / pages) if pages > 0 else 0.0
    return {
        'found': True,
        'currency': 'CNY',
        'symbol': '¥',
        'cost': round(cost, 6),
        'cost_display': format_cny(cost),
        'per_page_cost': round(per_page, 6),
        'per_page_display': format_cny(per_page) if pages > 0 else '',
        'input_per_m': in_rate,
        'output_per_m': out_rate,
        'cache_hit_per_m': price.get('cache_hit_per_m'),
        'prompt_tokens': p_tok,
        'cached_tokens': ch_tok,
        'completion_tokens': c_tok,
        'total_tokens': p_tok + c_tok,
        'matched_model': str(price.get('display_name') or model_id),
        'source': str(price.get('source') or 'official_crawl'),
        'source_url': str(price.get('source_url') or ''),
        'crawled_at': str(price.get('crawled_at') or ''),
        'subscription': bool(price.get('subscription')),
        'all_reused': bool(api_done > 0 and reused >= api_done),
    }


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    ap = argparse.ArgumentParser(description='自动爬取各视觉大模型官网定价并永久保存至本地缓存')
    ap.add_argument('--refresh', action='store_true', help='忽略本地缓存，强制重新爬取官网最新定价')
    ap.add_argument('--model', default='', help='查询指定模型的单价（元/百万 tokens）')
    ap.add_argument('--cache', type=Path, default=None, help='自定义缓存 JSON 路径')
    args = ap.parse_args()

    cache_file = args.cache or default_cache_path(ROOT)
    existed = cache_file.is_file()
    data = ensure_pricing_cache(root=ROOT, cache_path=cache_file, force=args.refresh)
    action = '已从官网重新爬取并永久保存' if (args.refresh or not existed) else '命中本地永久缓存（零网络请求）'
    print(f'[{action}] {cache_file}')
    print(f"爬取时间：{data.get('crawled_at') or '-'} · 共收录 {len(data.get('models') or {})} 个官方模型单价")
    for pid, info in (data.get('providers') or {}).items():
        st = f"✓ {info.get('count', 0)} 个模型" if info.get('ok') else f"✗ {info.get('error', '失败')}"
        print(f"  - {info.get('label', pid)}: {st}")
    if args.model:
        hit = lookup_model_price(args.model, root=ROOT, cache_path=cache_file)
        if hit:
            print(f"\n匹配模型：{hit.get('display_name')} | 输入 ¥{hit['input_per_m']}/M | 输出 ¥{hit['output_per_m']}/M | 缓存命中 {hit.get('cache_hit_per_m')} | 来源：{hit.get('source')}")
        else:
            print(f'\n未匹配到模型：{args.model}')


if __name__ == '__main__':
    main()
