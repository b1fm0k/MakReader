"""
MakReader - gestore download offline.
Salva le pagine dei capitoli su disco (cartella ./downloads) e tiene un registro
in downloads/index.json, così i capitoli scaricati restano disponibili offline.
"""

import os
import re
import json
import time
import queue
import hashlib
import threading

import sources


def did(source, chapter_id):
    return hashlib.sha1((source + "::" + chapter_id).encode("utf-8")).hexdigest()[:16]


def _ext(url):
    m = re.search(r"\.(jpg|jpeg|png|webp|gif)", url.split("?")[0], re.I)
    return "." + m.group(1).lower() if m else ".jpg"


class Manager:
    def __init__(self, base_dir):
        self.dir = os.path.join(base_dir, "downloads")
        os.makedirs(self.dir, exist_ok=True)
        self.index_file = os.path.join(self.dir, "index.json")
        self.lock = threading.Lock()
        self.reg = self._load()
        self.q = queue.Queue()
        self.worker = None
        # eventuali download interrotti -> rimetti in coda
        for k, it in list(self.reg.items()):
            if it.get("status") in ("downloading", "queued"):
                self.q.put(k)
        self._ensure_worker()

    def _load(self):
        if os.path.exists(self.index_file):
            try:
                with open(self.index_file) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save(self):
        tmp = self.index_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.reg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.index_file)

    def _ensure_worker(self):
        if self.worker and self.worker.is_alive():
            return
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def enqueue(self, item):
        """item: {source, chapter_id, manga_key, title, chapter_name}"""
        k = did(item["source"], item["chapter_id"])
        with self.lock:
            existing = self.reg.get(k)
            if existing and existing.get("status") == "done":
                return k
            self.reg[k] = {
                "id": k, "source": item["source"], "chapter_id": item["chapter_id"],
                "manga_key": item.get("manga_key", ""), "title": item.get("title", ""),
                "chapter_name": item.get("chapter_name", ""),
                "status": "queued", "total": 0, "done": 0, "files": [], "error": "",
                "ts": time.time(),
            }
            self._save()
        self.q.put(k)
        self._ensure_worker()
        return k

    def _run(self):
        while True:
            try:
                k = self.q.get(timeout=2)
            except queue.Empty:
                return  # niente da fare, il worker si ferma (riparte all'occorrenza)
            it = self.reg.get(k)
            if not it or it.get("status") == "done":
                continue
            try:
                self._set(k, status="downloading", error="")
                src = sources.REGISTRY[it["source"]]
                data = src.pages(it["chapter_id"])
                urls, ref = data["pages"], data.get("referer", "")
                folder = os.path.join(self.dir, k)
                os.makedirs(folder, exist_ok=True)
                files = []
                self._set(k, total=len(urls))
                for i, u in enumerate(urls):
                    name = "%03d%s" % (i + 1, _ext(u))
                    raw = sources.http_get(u, referer=ref or None, binary=True)
                    with open(os.path.join(folder, name), "wb") as f:
                        f.write(raw)
                    files.append(name)
                    self._set(k, done=i + 1, files=files)
                self._set(k, status="done", files=files)
            except Exception as e:
                self._set(k, status="error", error=str(e))

    def _set(self, k, **kw):
        with self.lock:
            if k in self.reg:
                self.reg[k].update(kw)
                self._save()

    def status(self):
        with self.lock:
            return list(self.reg.values())

    def is_done(self, source, chapter_id):
        it = self.reg.get(did(source, chapter_id))
        return bool(it and it.get("status") == "done")

    def local_pages(self, source, chapter_id):
        k = did(source, chapter_id)
        it = self.reg.get(k)
        if not it or it.get("status") != "done":
            return None
        return [("/file?id=%s&n=%d" % (k, i)) for i in range(len(it["files"]))]

    def file_path(self, k, n):
        it = self.reg.get(k)
        if not it or n < 0 or n >= len(it.get("files", [])):
            return None
        p = os.path.normpath(os.path.join(self.dir, k, it["files"][n]))
        if not p.startswith(os.path.normpath(self.dir)):
            return None
        return p

    def delete(self, k):
        with self.lock:
            it = self.reg.pop(k, None)
            self._save()
        if it:
            folder = os.path.join(self.dir, k)
            try:
                for f in it.get("files", []):
                    fp = os.path.join(folder, f)
                    if os.path.exists(fp):
                        os.remove(fp)
                if os.path.isdir(folder):
                    os.rmdir(folder)
            except Exception:
                pass
        return True
