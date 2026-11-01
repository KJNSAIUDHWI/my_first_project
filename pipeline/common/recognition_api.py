"""识别拼图、严格JSON和可恢复的豆包调用；不写人工标签。"""
import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

MODEL = 'doubao-seed-2-1-lite-260915'
ENDPOINT = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'
PROMPT = r'''你是答题卡忠实转录工具，不解题、不纠错、不评分。图片包含多个独立图块，每块上方的黑色标题为ID编号；白色间隔和标题不是试题。必须逐块处理，不得把不同图块的题号或答案合并。
只返回一个合法JSON对象，结构为 {"图块ID":{"kind":"fill","question_number":"卷面大题号","answers":{"小题号":{"空序号":"最终手写答案"}}}}。姓名学号图块改为 {"kind":"identity","name":"姓名","sid":"学号"}。
最高优先级的用户规则：只要某个填空/实验探究小题里出现任何图片、示意图、曲线图、坐标图或其他图形，整个小题一律舍弃，不读其中任何答案，即使它要求的是文字填空而不是作图。保留该小题号，固定输出 {"舍弃":"舍弃"}，这不是第1空，不表示空位数量。例如 {"2":{"舍弃":"舍弃"}}；同一道大题的其他无图小题继续按原规则识别。没有小题号的题出现图片，按"无小题号":{"舍弃":"舍弃"}输出。不要仅因为图片属于别的图块就舍弃本图块。不能遗漏被舍弃题目的题号，也不能把舍弃当作空白或看不清。
填空图块按实际印刷的小题号定位；没有小题号用"无小题号"。每个小题内按上到下、左到右为空位编号，从"1"起；一条连续填空线只算一空。大题号看不清写"题号看不清"，小题号看不清写"小题号看不清"，不能顺序猜号。每个可见的空都保留，绝不能合并空、漏空或把后面的答案前移。实验探究的文字回答也逐空保留。
先区分印刷题号/子题号与学生答案。印刷的(4)①、②表示第4小题的两个子题，answers键分别写"4.1"、"4.2"，各子题的空序号重新从"1"起；不要再嵌套一层小题号，不把①②当作答案，不把②的空白漏掉。层级格式示例：{"4.1":{"1":"某答案"},"4.2":{"1":""}}。没有印刷子题号时，不因同一小题有多空而人为创建子题；例如(1)的三个填空仍是"1":{"1":"...","2":"...","3":"..."}。
手写文字按可见字形逐字辨认，结合该字的横竖、穿出位置、封口、连笔及上下文确定；特别区分甲/乙/丙、甲/平、左/右、受/重等相似字，不以题型常见答案替代原字。每空检查开头、末尾和紧贴填空线的笔画，避免漏字、把连笔拆成多字或把正常收笔误当删除线。学生写到哪里就转录到哪里：未写完整的短语不续写，明显错别字不改正；若关键字存在无法排除的多种读法，该空写"看不清"，不输出猜测拼接的词。输出前再逐空核对文字、数字和空位数量，所有例子仅说明格式与辨认原则，不是本图答案。
逐空先检查划掉、覆盖、重写或插入，特别留意穿过文字、与笔画重叠的细横线、斜线、叉号和反复涂抹；不要把印刷填空线或正常字形笔画当作删除线。明确划掉/涂黑删除的内容不进入最终答案，不能因旧字仍可读就与新字拼接；例如“质量”被划掉、重写“位置”，只输出“位置”，不得输出“质量位置”。无法区分删除与有效笔迹，或最终答案认不清，直接写"看不清"，不能依据常识猜测。真正空白或明确仅删除未重写写空字符串。学生的错别字、错误数字、单位、正负号、公式忠实保留，不依据常识改正。印刷题干不作学生答案。
公式必须用合法LaTeX转录，以单个美元符号包围，例如 $...$；文字与公式混合时只包围公式部分。先按原图辨认分子/分母、分数线范围、上下标归属、括号、根号范围、正负号、乘除号及等号两边，再转录；严格区分数字1、小写l、大写L以及数字0、字母O，不按物理公式补项、改写、化简或纠错。分子分母、上下标及根号内容均用花括号明确范围。分数写\frac{分子}{分母}，禁止漏掉反斜杠或把frac写成rac。关键符号无法辨认时该空写“看不清”，不要生成貌似合理的公式。
JSON转义必须正确：公式里的每一个LaTeX反斜杠在原始JSON字符串中必须写成两个反斜杠；不得把\frac中的\f变成换页控制字符，也不得把\text中的\t、\begin中的\b等当作JSON控制转义。完整合法JSON示例：{"图块ID":{"kind":"fill","question_number":"22","answers":{"3":{"1":"$\\frac{F_{1}L_{1}}{F_{2}}=L_{2}$"}}}}。这只是格式示例，实际内容必须来自各自图块，不照抄示例。输出前检查JSON解码后的公式仍保留完整LaTeX命令，花括号和美元符号成对；只返回JSON，不输出检查过程。
姓名、学号分别忠实读取，学号必须用字符串保留前导零；认不清写"看不清"，没写用空字符串。
只输出清单内每个ID一次，不新增ID。缺块、严重裁切时为该ID返回 {"kind":"fill","error":"具体原因"}，不编造空位。'''


def digest(value):
    return hashlib.sha256(value).hexdigest()


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.writing')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('JSON重复键：' + key)
            result[key] = value
        return result
    clean = text.strip()
    if clean.startswith('```') and clean.endswith('```'):
        clean = clean.split('\n', 1)[1].rsplit('```', 1)[0]
    return json.loads(clean, object_pairs_hook=pairs,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError('非法数值：' + x)))


def validate_item(item, kind):
    if not isinstance(item, dict) or item.get('kind') != kind:
        raise ValueError('图块类型或结构不符')
    if item.get('error'):
        raise ValueError('模型报告：' + str(item['error']))
    if kind == 'identity':
        if not all(isinstance(item.get(k), str) for k in ('name', 'sid')):
            raise ValueError('姓名学号必须是字符串')
        return
    if not isinstance(item.get('question_number'), str) or not item['question_number']:
        raise ValueError('缺大题号')
    answers = item.get('answers')
    if not isinstance(answers, dict) or not answers:
        raise ValueError('没有逐空结构，不能当作空白')
    for sub, blanks in answers.items():
        if sub not in ('无小题号', '小题号看不清') and not re.fullmatch(r'[1-9]\d*(?:\.[1-9]\d*)*', sub):
            raise ValueError('小题号非法：' + sub)
        if not isinstance(blanks, dict) or not blanks:
            raise ValueError('小题缺空位：' + sub)
        if blanks == {'舍弃': '舍弃'}:
            continue
        siblings = {}
        for key in blanks:
            if not re.fullmatch(r'[1-9]\d*(?:\.[1-9]\d*)*', key):
                raise ValueError('空序号非法：' + sub)
            prefix, _, number = key.rpartition('.')
            siblings.setdefault(prefix, set()).add(int(number or key))
            if any(other.startswith(key+'.') for other in blanks):
                raise ValueError('空位编号与子层级冲突：' + sub)
        if any(numbers != set(range(1,len(numbers)+1)) for numbers in siblings.values()):
            raise ValueError('空序号不连续：' + sub)
        if not all(isinstance(v, str) for v in blanks.values()):
            raise ValueError('每空必须是字符串')


def normalize_item(item):
    """仅统一可逆的小题括号/全角；碰撞拒绝，不展平、不补位。"""
    if not isinstance(item, dict) or item.get('kind') != 'fill' or not isinstance(item.get('answers'), dict):
        return item
    normalized = {}
    for raw, blanks in item['answers'].items():
        key = unicodedata.normalize('NFKC', raw).strip()
        match = re.fullmatch(r'\((\d+)\)', key)
        if match:
            key = match.group(1)
        compound = re.fullmatch(r'\((\d+)\)\s*(\d+)', key)
        if compound:
            key = compound.group(1) + '.' + compound.group(2)
        if key in normalized:
            raise ValueError('小题号规范化后重复：' + key)
        # Preserve explicit nested paths. Never infer whether a repeated prefix is a subquestion or a blank.
        if isinstance(blanks, dict) and blanks != {'舍弃':'舍弃'}:
            flat = {}
            def walk(values, path=()):
                for raw_number, value in values.items():
                    number = unicodedata.normalize('NFKC', raw_number).strip()
                    address = path + (number,)
                    if isinstance(value, dict):
                        walk(value, address)
                    else:
                        name = '.'.join(address)
                        if name in flat:
                            raise ValueError('空位完整编号重复：' + name)
                        flat[name] = value
            walk(blanks)
            blanks = flat
        normalized[key] = blanks
    return {**item, 'answers': normalized}


def parse_record(record, grid):
    parsed = strict_json(record['raw_text'])
    if not isinstance(parsed, dict):
        raise ValueError('顶层必须为图块对象')
    expected = {i['id']: i['kind'] for i in grid['items']}
    record.update(items={}, errors={}, extra_ids=sorted(set(parsed)-set(expected)))
    for ident, kind in expected.items():
        try:
            item = normalize_item(parsed.get(ident))
            validate_item(item, kind)
            record['items'][ident] = item
        except ValueError as exc:
            record['errors'][ident] = str(exc)
    return record


class SquarePacker:
    """在线shelf排版：边检测边完成拼图；仅超大单块缩小，不为塞更多题缩字。"""
    def __init__(self, out, size=1500, prefix='g'):
        self.out, self.size = Path(out), size
        self.prefix = prefix
        self.index = 0
        try:
            self.font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 24)
        except OSError:
            self.font = ImageFont.load_default()
        self._reset()

    def _reset(self):
        self.canvas = Image.new('RGB', (self.size, self.size), 'white')
        self.items = []
        self.free = [(12, 12, self.size-24, self.size-24)]

    def add(self, item, image):
        image = image.convert('RGB')
        max_w, max_h = self.size - 24, self.size - 60
        scale = min(1., max_w / image.width, max_h / image.height)
        resized = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS) if scale < 1 else image
        w, h = resized.size
        finished = None
        def slot():
            fits = [(fw*fh-w*(h+36), i) for i, (_, _, fw, fh) in enumerate(self.free) if fw >= w and fh >= h+36]
            return min(fits)[1] if fits else None
        selected = slot()
        if selected is None:
            finished = self.finish()
            selected = slot()
        x,y,fw,fh = self.free.pop(selected)
        if fw > w+10:
            self.free.append((x+w+10,y,fw-w-10,h+36))
        if fh > h+46:
            self.free.append((x,y+h+46,fw,fh-h-46))
        ImageDraw.Draw(self.canvas).text((x, y), 'ID ' + item['id'], fill='black', font=self.font)
        self.canvas.paste(resized, (x, y + 32))
        self.items.append({**item, 'grid_box': [x, y + 32, x + w, y + 32 + h],
                           'scale': scale, 'original_size': list(image.size)})
        return finished

    def finish(self):
        if not self.items:
            return None
        name = f'{self.prefix}{self.index:03d}'
        path = self.out / 'grids' / (name + '.png')
        path.parent.mkdir(parents=True, exist_ok=True)
        self.canvas.save(path)
        result = {'id': name, 'image': path.relative_to(self.out).as_posix(), 'size': [self.size, self.size],
                  'sha256': digest(path.read_bytes()), 'items': self.items,
                  'occupied_fraction': sum((i['grid_box'][2]-i['grid_box'][0])*(i['grid_box'][3]-i['grid_box'][1]) for i in self.items) / self.size**2}
        save_json(path.with_suffix('.json'), result)
        self.index += 1
        self._reset()
        return result


class DoubaoClient:
    def __init__(self, out, key=None, transport=None):
        self.out = Path(out)
        self.key = key or os.environ.get('ARK_API_KEY', '')
        if not self.key:
            local = Path(__file__).resolve().parents[2]/'.env'
            if local.is_file():
                for line in local.read_text(encoding='utf-8-sig').splitlines():
                    if line.strip().startswith('ARK_API_KEY='):
                        self.key = line.strip().split('=', 1)[1].strip().strip('\"\x27')
        self.transport = transport
        self.stop = threading.Event()
        self.stop_reason = '凭据或权限失败，已停止后续请求'
        self.serial = threading.Semaphore(1)
        self.throttled = threading.Event()

    def call(self, grid, refresh=False):
        content = PROMPT + '\n本次图块清单（只标明来源，不含参考答案）：\n' + json.dumps(
            [{'id': i['id'], 'kind': i['kind']} for i in grid['items']], ensure_ascii=False)
        request = {'model': MODEL, 'thinking': {'type': 'disabled'}, 'stream': False,
                   'max_completion_tokens': 16384,
                   'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': content},
                       {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode((self.out/grid['image']).read_bytes()).decode(), 'detail': 'high'}}]}]}
        legacy_fingerprint = digest(json.dumps({'body': request, 'endpoint': ENDPOINT}, sort_keys=True).encode())
        request['response_format'] = {'type': 'json_object'}
        request['messages'][0]['content'][0]['text'] += '''\n严格格式示例：{"p000_000":{"kind":"fill","question_number":"20","answers":{"1":{"1":"答案一","2":""},"2":{"1":"看不清"}}}}。
小题键用数字字符串1、2，不加括号；若存在(1)下面的①②子层级，则分别用1.1、1.2，不得把子层级和普通空混合或输出重复键。每小题多个空统一放在同一个对象中，空序号1、2、3连续。不得新增无小题号来装本应属于某小题的后续空位。回答前检查所有大括号平衡、每块ID独立闭合，空字符串不是空对象。'''
        fingerprint = digest(json.dumps({'body': request, 'endpoint': ENDPOINT}, sort_keys=True).encode())
        path = self.out / 'api' / (grid['id'] + '_' + fingerprint[:16] + '.json')
        legacy_path = self.out / 'api' / (grid['id'] + '_' + legacy_fingerprint[:16] + '.json')
        if not refresh:
            for cached_path in (path, legacy_path):
                if not cached_path.exists():
                    continue
                cached = json.loads(cached_path.read_text(encoding='utf-8'))
                if cached.get('ok') and cached['fingerprint'] in (fingerprint, legacy_fingerprint):
                    parse_record(cached, grid)
                    save_json(cached_path, cached)
                    return {**cached, 'reused': True, 'compatible_legacy_cache': cached_path == legacy_path}
        record = {'grid': grid['id'], 'fingerprint': fingerprint, 'ok': False, 'model': MODEL,
                  'detail': 'high', 'thinking': 'disabled', 'attempts': [], 'items': {}, 'errors': {},
                  'image_sha256': grid['sha256']}
        start = time.monotonic()
        if not self.key and self.transport is None:
            record['error'] = '缺少ARK_API_KEY，尚未调用豆包'
        else:
            for attempt in range(3):
                if self.stop.is_set():
                    record['error'] = self.stop_reason
                    record['not_sent'] = True
                    break
                try:
                    body = json.dumps(request, ensure_ascii=False).encode()
                    def send():
                        if self.stop.is_set():
                            raise PermissionError(self.stop_reason)
                        if self.transport:
                            return self.transport(request)
                        req = urllib.request.Request(ENDPOINT, body, {'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
                        with urllib.request.urlopen(req, timeout=180) as response:
                            return json.loads(response.read())
                    if self.throttled.is_set():
                        with self.serial:
                            response = send()
                    else:
                        response = send()
                    record['attempts'].append({'attempt': attempt + 1, 'status': 200})
                    message = response['choices'][0]
                    record.update(usage=response.get('usage', {}), returned_model=response.get('model'),
                                  raw_text=message['message'].get('content', ''), finish_reason=message['finish_reason'])
                    # Save billable response before validation; a restart must not lose paid evidence.
                    save_json(path, record)
                    if message['finish_reason'] != 'stop':
                        raise ValueError('响应未完整结束：' + message['finish_reason'])
                    parse_record(record, grid)
                    record['ok'] = True
                    break
                except urllib.error.HTTPError as exc:
                    record['attempts'].append({'attempt': attempt + 1, 'status': exc.code})
                    record['error'] = 'HTTP ' + str(exc.code)
                    try:
                        details = json.loads(exc.read().decode('utf-8')).get('error', {}) if getattr(exc, 'fp', None) else {}
                    except (ValueError, OSError):
                        details = {}
                    code = details.get('code', '')
                    record['error_code'] = code
                    if code in ('SetLimitExceeded', 'InsufficientBalance', 'AccountOverdueError'):
                        self.stop_reason = '账号用量/费用限制，模型已暂停：' + code
                        record['error'] = self.stop_reason
                        self.stop.set()
                        break
                    if exc.code in (401, 403):
                        self.stop.set()
                        break
                    if exc.code == 429:
                        self.throttled.set()
                    if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                        break
                    time.sleep(2 ** (attempt + 1))
                except (TimeoutError, OSError) as exc:
                    if self.stop.is_set():
                        record['error'] = self.stop_reason
                        record['not_sent'] = True
                        break
                    record['error'] = type(exc).__name__
                    if attempt == 2:
                        break
                    time.sleep(2 ** (attempt + 1))
                except (ValueError, KeyError, TypeError, IndexError) as exc:
                    record['error'] = str(exc).replace(self.key, '[REDACTED]') if self.key else str(exc)
                    break
        record['seconds'] = round(time.monotonic() - start, 3)
        save_json(path, record)
        return record
