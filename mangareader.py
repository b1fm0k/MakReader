#!/usr/bin/env python3
"""
MakReader - lettore manga locale, multi-sorgente.
Avvio:  python3 mangareader.py        (oppure: python3 mangareader.py 5599)
"""

import json
import os
import sys
import time
import gzip
import shutil
import socket
import threading
import webbrowser
import importlib.util
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse

APP_VERSION = "1.1.0"
# Dopo aver creato il repository su GitHub, scrivi qui "tuo-utente/nome-repo":
UPDATE_REPO = "b1fm0k/MakReader"
UPDATE_BRANCH = "main"

# Cartella dati scrivibile (funziona sia come script sia come app compilata .exe/.app)
if getattr(sys, "frozen", False):
    _BUNDLE = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    HERE = os.path.join(os.path.expanduser("~"), "MakReader-dati")
    os.makedirs(HERE, exist_ok=True)

    def _ver_tuple(path):
        try:
            with open(path, encoding="utf-8") as f:
                return tuple(int(x) for x in str(json.load(f).get("version", "0")).split("."))
        except Exception:
            return None
    _bundle_ver = _ver_tuple(os.path.join(_BUNDLE, "version.json"))
    _data_ver = _ver_tuple(os.path.join(HERE, "version.json"))
    # rinfresca i file dell'app se questo programma è più recente di quelli già in cartella
    # (così un .exe/.app nuovo porta davvero le sue novità; libreria e download restano intatti)
    _refresh = (_data_ver is None) or (_bundle_ver is not None and _bundle_ver > _data_ver)
    for _fn in ("index.html", "sources.py", "downloads.py", "version.json", "makreader.png"):
        _dst, _src = os.path.join(HERE, _fn), os.path.join(_BUNDLE, _fn)
        if os.path.exists(_src) and (_refresh or not os.path.exists(_dst)):
            try:
                shutil.copy(_src, _dst)
            except Exception:
                pass
else:
    HERE = os.path.dirname(os.path.abspath(__file__))

# I DATI dell'utente (libreria, download, impostazioni) stanno SEMPRE in
# ~/MakReader-dati, indipendentemente da come avvii l'app. Così la versione
# "da sorgente" (per testare) e l'app installata condividono la stessa
# libreria e gli stessi progressi di lettura: niente più travasi a mano.
# Il CODICE invece resta in HERE (cartella sorgenti da script, bundle se .app/.exe).
DATA_DIR = os.path.join(os.path.expanduser("~"), "MakReader-dati")
os.makedirs(DATA_DIR, exist_ok=True)


def _load_mod(name):
    path = os.path.join(HERE, name + ".py")
    if os.path.exists(path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    return __import__(name)


sources = _load_mod("sources")
downloads = _load_mod("downloads")


def _safe_xml_fromstring(raw):
    """Parsa XML rifiutando DTD/entità a livello di parser (anti 'XML bomb').

    Usa direttamente il motore expat con gli handler di DTD/ENTITY impostati a
    sollevare un errore: appena incontra un <!DOCTYPE> o una dichiarazione
    <!ENTITY> interrompe, così l'espansione di entità ricorsive (billion laughs)
    è impossibile per costruzione. Costruisce un albero ElementTree standard.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")

    def _forbid(*_a, **_k):
        raise ValueError("File XML non ammesso (contiene DOCTYPE/ENTITY).")

    builder = ET.TreeBuilder()
    p = expat.ParserCreate()
    p.StartDoctypeDeclHandler = _forbid
    p.EntityDeclHandler = _forbid
    p.UnparsedEntityDeclHandler = _forbid
    p.ExternalEntityRefHandler = lambda *a, **k: False
    p.StartElementHandler = lambda name, attrs: builder.start(name, attrs)
    p.EndElementHandler = lambda name: builder.end(name)
    p.CharacterDataHandler = lambda data: builder.data(data)
    p.Parse(raw, True)
    return builder.close()


def parse_mal(raw):
    """Legge un export MyAnimeList (XML o XML gzippato) e restituisce le voci manga."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    if len(raw) > 30 * 1024 * 1024:
        raise ValueError("File troppo grande.")
    # sicurezza: rifiuta XML con DOCTYPE/ENTITY (vettore della 'XML bomb').
    # Doppia difesa: controllo sui byte + parser che rifiuta DTD/entità.
    head = raw[:8192].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise ValueError("File XML non ammesso (contiene DOCTYPE/ENTITY).")
    root = _safe_xml_fromstring(raw)
    entries = []
    for m in root.findall("manga"):
        title = (m.findtext("manga_title") or m.findtext("series_title") or "").strip()
        if not title:
            continue
        entries.append({
            "title": title,
            "status": (m.findtext("my_status") or "").strip(),
            "read": (m.findtext("my_read_chapters") or "0").strip(),
            "malId": (m.findtext("manga_mangadb_id") or "").strip(),
        })
    return entries

def normalize_mal(d):
    """Estrae i campi utili dalla risposta Jikan (MyAnimeList)."""
    def names(key):
        return ", ".join(x.get("name", "") for x in d.get(key, []) if x.get("name"))
    pub = (d.get("published") or {}).get("string") or ""
    info_raw = [
        ("Type", d.get("type")), ("Volumes", d.get("volumes")),
        ("Chapters", d.get("chapters")), ("Status", d.get("status")),
        ("Published", pub), ("Genres", names("genres")),
        ("Themes", names("themes")), ("Demographic", names("demographics")),
        ("Serialization", names("serializations")), ("Authors", names("authors")),
        ("Score", d.get("score")),
    ]
    info = [[k, str(v)] for k, v in info_raw if v not in (None, "", "None")]
    return {
        "mal_id": d.get("mal_id"), "url": d.get("url"),
        "english": d.get("title_english") or "",
        "japanese": d.get("title_japanese") or "",
        "synonyms": d.get("title_synonyms") or [],
        "synopsis": d.get("synopsis") or "",
        "info": info,
    }


# 4005 = goroawase di "manga" (4=yo, 0=ma, 0=n, 5=ga)
PORT_CANDIDATES = [4005, 4456, 5577, 6680, 7788, 8123, 9123, 0]
CURRENT_PORT = 0
DATA_FILE = os.path.join(DATA_DIR, "library.json")   # dati condivisi
INDEX_FILE = os.path.join(HERE, "index.html")        # codice/app
VERSION_FILE = os.path.join(HERE, "version.json")    # codice/app
DL = downloads.Manager(DATA_DIR)                      # download condivisi
UPDATE_FILES = ["index.html", "sources.py", "downloads.py", "version.json"]


def raw_url(fn):
    return "https://raw.githubusercontent.com/%s/%s/%s" % (UPDATE_REPO, UPDATE_BRANCH, fn)


def local_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return str(json.load(f).get("version", "0"))
    except Exception:
        return "0"


def version_tuple(v):
    parts = []
    for p in str(v).split("."):
        num = ""
        for ch in p:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)


_SAVE_LOCK = threading.Lock()


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # file troncato/duplicato: recupera il primo oggetto JSON valido
        obj, _ = json.JSONDecoder().raw_decode(raw)
        return obj


def load_data():
    d = None
    if os.path.exists(DATA_FILE):
        try:
            d = _read_json(DATA_FILE)
        except Exception:
            # prova il backup automatico
            if os.path.exists(DATA_FILE + ".bak"):
                try:
                    d = _read_json(DATA_FILE + ".bak")
                except Exception:
                    d = None
    if not isinstance(d, dict):
        d = {}
    d.setdefault("follows", {})
    d.setdefault("progress", {})
    d.setdefault("history", [])
    d.setdefault("sources", {"order": sources.DEFAULT_ORDER,
                             "enabled": {s: True for s in sources.DEFAULT_ORDER}})
    # integra eventuali sorgenti nuove non ancora presenti nella config salvata
    order = d["sources"].setdefault("order", [])
    enabled = d["sources"].setdefault("enabled", {})
    for sid in sources.DEFAULT_ORDER:
        if sid not in order:
            order.append(sid)
        enabled.setdefault(sid, True)
    d.setdefault("settings", {"dir": "ltr", "width": "M", "autoHours": 6, "notify": False})
    d.setdefault("suggestions", [])
    return d


def save_data(data):
    # un solo salvataggio per volta (evita scritture concorrenti che corrompono il file)
    with _SAVE_LOCK:
        tmp = "%s.%d.tmp" % (DATA_FILE, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # backup della versione precedente prima di sostituire
        if os.path.exists(DATA_FILE):
            try:
                shutil.copy(DATA_FILE, DATA_FILE + ".bak")
            except Exception:
                pass
        os.replace(tmp, DATA_FILE)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path, q = p.path, urllib.parse.parse_qs(p.query)

        if path in ("/", "/index.html"):
            try:
                with open(INDEX_FILE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, "index.html mancante", "text/plain")
            return

        if path == "/data":
            self._json(load_data())
            return

        if path == "/icon":
            ip = os.path.join(HERE, "makreader.png")
            if os.path.exists(ip):
                with open(ip, "rb") as f:
                    self._send(200, f.read(), "image/png", extra={"Cache-Control": "max-age=86400"})
            else:
                self._send(404, "no icon", "text/plain")
            return

        if path == "/sources":
            d = load_data()
            self._json({"meta": sources.list_meta(), "config": d["sources"]})
            return

        if path == "/search":
            sid = q.get("source", [""])[0]
            query = q.get("q", [""])[0]
            by = q.get("by", ["title"])[0]
            try:
                self._json({"results": sources.REGISTRY[sid].search(query, by)})
            except KeyError:
                self._json({"error": "sorgente sconosciuta"}, 400)
            except Exception as e:
                self._json({"error": str(e)}, 502)
            return

        if path == "/details":
            sid = q.get("source", [""])[0]
            url = q.get("url", [""])[0]
            try:
                self._json(sources.REGISTRY[sid].details(url))
            except Exception as e:
                self._json({"error": str(e)}, 502)
            return

        if path == "/pages":
            sid = q.get("source", [""])[0]
            url = q.get("url", [""])[0]
            local = DL.local_pages(sid, url)
            if local:
                self._json({"pages": local, "referer": "", "offline": True})
                return
            try:
                self._json(sources.REGISTRY[sid].pages(url))
            except Exception as e:
                self._json({"error": str(e)}, 502)
            return

        if path == "/mal":
            mid = q.get("id", [""])[0]
            title = q.get("title", [""])[0]
            try:
                if mid:
                    data = json.loads(sources.http_get(
                        "https://api.jikan.moe/v4/manga/" + urllib.parse.quote(mid))).get("data")
                else:
                    arr = json.loads(sources.http_get(
                        "https://api.jikan.moe/v4/manga?limit=1&q=" + urllib.parse.quote(title))).get("data") or []
                    data = arr[0] if arr else None
                if not data:
                    self._json({"found": False})
                else:
                    self._json({"found": True, "mal": normalize_mal(data)})
            except Exception as e:
                self._json({"found": False, "error": str(e)}, 502)
            return

        if path == "/update/check":
            if not UPDATE_REPO:
                self._json({"configured": False, "current": local_version()})
                return
            try:
                remote = json.loads(sources.http_get(raw_url("version.json")))
                cur = local_version()
                lat = str(remote.get("version", "?"))
                newer = version_tuple(lat) > version_tuple(cur)
                self._json({"configured": True, "current": cur, "latest": lat,
                            "update": newer, "notes": remote.get("notes", "")})
            except Exception as e:
                self._json({"configured": True, "current": local_version(), "error": str(e)}, 502)
            return

        if path == "/latest":
            sid = q.get("source", [""])[0]
            url = q.get("url", [""])[0]
            try:
                self._json({"number": sources.REGISTRY[sid].latest(url)})
            except Exception as e:
                self._json({"number": "", "error": str(e)}, 502)
            return

        if path == "/downloads":
            self._json({"items": DL.status()})
            return

        if path == "/file":
            fid = q.get("id", [""])[0]
            n = int(q.get("n", ["0"])[0])
            fp = DL.file_path(fid, n)
            if not fp or not os.path.exists(fp):
                self._send(404, "non trovato", "text/plain")
                return
            ct = "image/jpeg"
            low = fp.lower()
            if low.endswith(".png"): ct = "image/png"
            elif low.endswith(".webp"): ct = "image/webp"
            elif low.endswith(".gif"): ct = "image/gif"
            with open(fp, "rb") as f:
                self._send(200, f.read(), ct, extra={"Cache-Control": "max-age=604800"})
            return

        if path == "/img":
            url = q.get("url", [""])[0]
            ref = q.get("ref", [""])[0] or None
            if not url:
                self._send(400, "url mancante", "text/plain")
                return
            try:
                body = sources.http_get(url, referer=ref, binary=True)
                ct = "image/jpeg"
                low = url.lower()
                if ".png" in low: ct = "image/png"
                elif ".webp" in low: ct = "image/webp"
                elif ".gif" in low: ct = "image/gif"
                self._send(200, body, ct, extra={"Cache-Control": "max-age=86400"})
            except Exception as e:
                self._send(502, str(e), "text/plain")
            return

        self._send(404, "non trovato", "text/plain")

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        if p.path == "/data":
            try:
                save_data(json.loads(raw))
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return
        if p.path == "/sources":
            try:
                d = load_data()
                d["sources"] = json.loads(raw)
                save_data(d)
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return
        if p.path == "/import/mal":
            try:
                self._json({"entries": parse_mal(raw)})
            except Exception as e:
                self._json({"error": "File non valido: " + str(e)}, 400)
            return
        if p.path == "/download":
            try:
                body = json.loads(raw)
                items = body if isinstance(body, list) else [body]
                ids = [DL.enqueue(it) for it in items]
                self._json({"ok": True, "ids": ids})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return
        if p.path == "/download/delete":
            try:
                DL.delete(json.loads(raw).get("id"))
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return
        if p.path == "/quit":
            self._json({"ok": True})
            threading.Timer(0.4, lambda: os._exit(0)).start()
            return
        if p.path == "/restart":
            self._json({"ok": True})

            def _restart():
                try:
                    if getattr(sys, "frozen", False):
                        os.execv(sys.executable, [sys.executable, str(CURRENT_PORT)])
                    else:
                        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), str(CURRENT_PORT)])
                except Exception:
                    os._exit(0)
            threading.Timer(0.5, _restart).start()
            return
        if p.path == "/update/apply":
            if not UPDATE_REPO:
                self._json({"error": "repository non configurato"}, 400)
                return
            if not getattr(sys, "frozen", False):
                # Modalità sorgente (sviluppatore): NON sovrascrivere i file locali
                # con quelli di GitHub, altrimenti si perdono le modifiche non caricate.
                self._json({"error": "Aggiornamento disabilitato in modalità sorgente: "
                                     "stai girando dai file locali, non li sovrascrivo con GitHub."}, 400)
                return
            try:
                for fn in UPDATE_FILES:
                    data = sources.http_get(raw_url(fn), binary=True)
                    tmp = os.path.join(HERE, fn + ".new")
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, os.path.join(HERE, fn))
                self._json({"ok": True})
            except Exception as e:
                self._json({"error": str(e)}, 502)
            return
        self._send(404, "non trovato", "text/plain")


def is_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port)); return True
        except OSError:
            return False


def pick_port():
    wanted = next((a for a in sys.argv[1:] if a.isdigit()), None)
    if wanted:
        p = int(wanted)
        # dopo un riavvio la vecchia porta può restare occupata un istante
        for _ in range(20):
            if is_free(p):
                return p
            time.sleep(0.1)
        print("  La porta %d è occupata, ne cerco un'altra…" % p)
    for p in PORT_CANDIDATES:
        if p == 0 or is_free(p):
            return p
    return 0


def main():
    global CURRENT_PORT
    server = ThreadingHTTPServer(("127.0.0.1", pick_port()), Handler)
    port = server.server_address[1]
    CURRENT_PORT = port
    url = "http://localhost:%d" % port
    print("\n  MakReader avviato!  Apri:  %s\n  (premi Ctrl+C per fermare)\n" % url, flush=True)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Arresto MakReader. A presto!")
        server.shutdown()


if __name__ == "__main__":
    main()
