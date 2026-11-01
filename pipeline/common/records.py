"""Small durable records shared by iteration tools. No training imports."""

import csv

import hashlib

import json

import os

from concurrent.futures import ThreadPoolExecutor, as_completed

from contextlib import contextmanager

from datetime import datetime, timezone, timedelta

from pathlib import Path

import tempfile

import time





def now():

    return datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')





def sha(path):

    h = hashlib.sha256()

    with Path(path).open('rb') as f:

        for part in iter(lambda: f.read(1024 * 1024), b''):

            h.update(part)

    return h.hexdigest()





def _sha_key(path):

    return os.path.normcase(str(Path(path).resolve()))





def _sha_file(path):

    return sha(path)





class ShaCache:

    """按 mtime+size 复用的文件哈希缓存，可落盘成 JSON。



    逐张哈希几万张小图时瓶颈是每张一次 open/read 的系统调用，不是哈希计算。

    文件内容变了（mtime 或 size 变）就重算，不会拿旧哈希冒充。

    """



    VERSION = 1



    def __init__(self, path=None):

        self.path = Path(path) if path else None

        self.items = {}

        self.dirty = False

        if self.path and self.path.exists():

            try:

                raw = json.loads(self.path.read_text(encoding='utf-8-sig'))

                items = raw.get('items') if isinstance(raw, dict) else None

                self.items = items if isinstance(items, dict) else {}

            except (ValueError, OSError):

                self.items = {}



    def _fresh(self, path, key):

        hit = self.items.get(key)

        if not hit or not isinstance(hit, list) or len(hit) != 3:

            return False

        try:

            st = Path(path).stat()

        except OSError:

            return False

        return hit[0] == st.st_mtime_ns and hit[1] == st.st_size and isinstance(hit[2], str)



    def get(self, path):

        path = Path(path)

        key = _sha_key(path)

        if self._fresh(path, key):

            return self.items[key][2]

        st = path.stat()

        value = sha(path)

        self.items[key] = [st.st_mtime_ns, st.st_size, value]

        self.dirty = True

        return value



    def preload(self, paths, workers=None):

        """批量填充；sha256 在 C 层执行会释放 GIL，线程池对 IO 密集已足够。"""

        todo = []

        for p in paths:

            p = Path(p)

            key = _sha_key(p)

            if self._fresh(p, key):

                continue

            try:

                st = p.stat()

            except OSError:

                continue

            todo.append((p, key, st))

        if not todo:

            return

        workers = workers or min(16, (os.cpu_count() or 4) * 2)

        with ThreadPoolExecutor(max_workers=workers) as pool:

            futures = {pool.submit(_sha_file, p): (key, st) for p, key, st in todo}

            for fut in as_completed(futures):

                key, st = futures[fut]

                self.items[key] = [st.st_mtime_ns, st.st_size, fut.result()]

                self.dirty = True



    def get_many(self, paths, workers=None):

        paths = [Path(p) for p in paths]

        self.preload(paths, workers)

        return {_sha_key(p): self.get(p) for p in paths}



    def flush(self):

        if not self.dirty or not self.path:

            return

        with lock(self.path.with_suffix(self.path.suffix + '.lock')):

            merged = {}

            if self.path.exists():

                try:

                    raw = json.loads(self.path.read_text(encoding='utf-8-sig'))

                    merged = raw.get('items') or {}

                except (ValueError, OSError):

                    merged = {}

            merged.update(self.items)

            self.items = merged

            atomic_text(self.path, json.dumps({'version': self.VERSION, 'items': merged},

                                              ensure_ascii=False, separators=(',', ':')) + '\n')

        self.dirty = False





def atomic_text(path, text):

    path = Path(path)

    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.pending', dir=path.parent)

    try:

        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:

            f.write(text)

            f.flush()

            os.fsync(f.fileno())

        for attempt in range(15):

            try:

                os.replace(tmp, path)

                break

            except (PermissionError, OSError):

                if attempt == 14:

                    raise

                time.sleep(0.05 * (attempt + 1))

    finally:

        Path(tmp).unlink(missing_ok=True)





def write_json(path, value):

    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')





def read_json(path, default=None):

    p = Path(path)

    return json.loads(p.read_text(encoding='utf-8-sig')) if p.exists() else default





def read_events(path):

    p = Path(path)

    if not p.exists():

        return []

    out = []

    for n, line in enumerate(p.read_text(encoding='utf-8-sig').splitlines(), 1):

        if not line.strip():

            continue

        try:

            row = json.loads(line)

            if not isinstance(row, dict):

                raise ValueError('event must be an object')

            out.append(row)

        except ValueError as e:

            raise ValueError(f'日志损坏：{p} 第{n}行：{e}') from e

    return out





def append(path, row):

    p = Path(path)

    p.parent.mkdir(parents=True, exist_ok=True)

    with p.open('a', encoding='utf-8') as f:

        f.write(json.dumps({'time': now(), **row}, ensure_ascii=False, allow_nan=False) + '\n')

        f.flush()





@contextmanager

def lock(path):

    """OS lock releases on crashes; the tiny lock file is a permanent coordination file."""

    p = Path(path)

    p.parent.mkdir(parents=True, exist_ok=True)

    with p.open('a+b') as f:

        f.seek(0, 2)

        if f.tell() == 0:

            f.write(b'0'); f.flush()

        f.seek(0)

        if os.name == 'nt':

            import msvcrt

            try:

                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

            except OSError as e:

                raise RuntimeError('已有操作进行中：' + str(p)) from e

        else:

            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:

            yield

        finally:

            f.seek(0)

            if os.name == 'nt':

                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

            else:

                fcntl.flock(f, fcntl.LOCK_UN)





def read_csv(path):

    with Path(path).open(encoding='utf-8-sig', newline='') as f:

        return list(csv.DictReader(f))





def write_csv(path, rows, fields):

    import io

    buf = io.StringIO(newline='')

    writer = csv.DictWriter(buf, fieldnames=fields)

    writer.writeheader(); writer.writerows(rows)

    atomic_text(path, buf.getvalue())
