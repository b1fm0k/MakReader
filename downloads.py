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
import zipfile
import hashlib
import threading
import concurrent.futures

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


# ============================================================
#  Esportazione ZIP (Model C: scarica sempre da capo, ignora l'offline)
# ============================================================
def _sanitize(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(name or "")).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:120] or "manga"


def _int_width(values):
    """Larghezza (cifre) della parte intera massima fra i valori dati."""
    mx = 1
    for v in values:
        try:
            mx = max(mx, int(float(str(v))))
        except (ValueError, TypeError):
            pass
    return max(1, len(str(mx)))


def _pad(number, width):
    """Padding dell'intero conservando eventuali decimali: '34.5' -> '0034.5'."""
    s = str(number or "").strip()
    if not s:
        return "0".zfill(width)
    if "." in s:
        intp, dec = s.split(".", 1)
        try:
            intp = str(int(intp))
        except ValueError:
            intp = "0"
        return intp.zfill(width) + "." + dec
    try:
        return str(int(s)).zfill(width)
    except ValueError:
        return s.zfill(width)


class ExportManager:
    def __init__(self, base_dir):
        self.dir = os.path.join(base_dir, "exports")
        os.makedirs(self.dir, exist_ok=True)
        self.jobs = {}
        self.lock = threading.Lock()
        for f in os.listdir(self.dir):  # pulizia zip vecchi all'avvio
            if f.endswith(".zip"):
                try:
                    os.remove(os.path.join(self.dir, f))
                except Exception:
                    pass

    def start(self, title, chapters, source=""):
        jid = hashlib.sha1((str(title) + str(time.time())).encode("utf-8")).hexdigest()[:12]
        with self.lock:
            self.jobs[jid] = {"id": jid, "status": "preparing", "total": len(chapters),
                              "done": 0, "file": None, "error": "", "title": title, "source": source}
        threading.Thread(target=self._run, args=(jid, title, chapters), daemon=True).start()
        return jid

    def _set(self, jid, **kw):
        with self.lock:
            if jid in self.jobs:
                self.jobs[jid].update(kw)

    def status(self, jid):
        with self.lock:
            j = self.jobs.get(jid)
            return dict(j) if j else None

    def ready_file(self, jid):
        with self.lock:
            j = self.jobs.get(jid)
        if j and j.get("status") == "done" and j.get("file") and os.path.exists(j["file"]):
            nm = _sanitize(j["title"])
            if j.get("source"):
                nm += " (" + _sanitize(j["source"]) + ")"
            return j["file"], nm + ".zip"
        return None, None

    def _run(self, jid, title, chapters):
        try:
            cwidth = _int_width([c.get("number") for c in chapters])
            vols = [str(c.get("vol") or "").strip() for c in chapters]
            have_vol = any(vols)
            vwidth = _int_width([v for v in vols if v]) if have_vol else 1
            mtitle = _sanitize(title)
            zip_path = os.path.join(self.dir, jid + ".zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
                for i, ch in enumerate(chapters):
                    src = sources.REGISTRY.get(ch.get("source"))
                    if not src:
                        self._set(jid, done=i + 1)
                        continue
                    data = src.pages(ch["chapter_id"])
                    urls, ref = data["pages"], data.get("referer", "")
                    pwidth = max(2, len(str(len(urls))))
                    cnum = _pad(ch.get("number"), cwidth)
                    v = str(ch.get("vol") or "").strip()
                    vpref = ("v" + _pad(v, vwidth)) if v else ""
                    folder = "Cap. " + cnum
                    if ch.get("name"):
                        folder += " - " + _sanitize(ch["name"])
                    # scarica le pagine in parallelo (poche per le sorgenti "scraper",
                    # di più per API/CDN), poi le scrive nello zip in ordine
                    conc = 8 if ch.get("source") in ("mangadex", "mangadex-it", "comick", "comick-it") else 4

                    def _fetch(iu):
                        idx, u = iu
                        return idx, sources.http_get(u, referer=ref or None, binary=True), _ext(u)
                    blobs = [None] * len(urls)
                    with concurrent.futures.ThreadPoolExecutor(max_workers=min(conc, max(1, len(urls)))) as ex:
                        for idx, raw, ext in ex.map(_fetch, list(enumerate(urls))):
                            blobs[idx] = (raw, ext)
                    for p, (raw, ext) in enumerate(blobs):
                        fname = "%sc%sp%s%s" % (vpref, cnum, str(p + 1).zfill(pwidth), ext)
                        zf.writestr("%s/%s/%s" % (mtitle, folder, fname), raw)
                    self._set(jid, done=i + 1)
            self._set(jid, status="done", file=zip_path)
        except Exception as e:
            self._set(jid, status="error", error=str(e))
