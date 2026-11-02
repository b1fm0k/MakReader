#!/usr/bin/env python3
"""
MakReader - lettore manga locale, multi-sorgente.
Avvio:  python3 mangareader.py        (oppure: python3 mangareader.py 5599)
"""

import json
import os
import re
import sys
import time
import hmac
import hashlib
import secrets
import ipaddress
import subprocess
import gzip
import shutil
import socket
import threading
import webbrowser
import importlib.util
import concurrent.futures
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
import urllib.error

# Versione del BINARIO (questo file non si auto-aggiorna).
# Va tenuta allineata a "binaryVersion" di version.json a ogni release che
# tocca mangareader.py, altrimenti l'app continua a chiedere di riscaricarsi.
APP_VERSION = "1.5.0"
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

def mal_genres(d):
    """Lista di generi MAL (generi + temi + demografica) da una voce Jikan."""
    out = []
    for key in ("genres", "themes", "demographics"):
        for x in d.get(key, []):
            n = x.get("name")
            if n and n not in out:
                out.append(n)
    return out


def mal_genre_ids(d):
    """ID numerici dei generi MAL, nello stesso ordine di mal_genres().
    Gli ID sono stabili nel tempo, i nomi no: servono per i filtri futuri."""
    out = []
    for key in ("genres", "themes", "demographics"):
        for x in d.get(key, []):
            i = x.get("mal_id")
            if i and i not in out:
                out.append(i)
    return out


def _jikan(url):
    """GET sul servizio pubblico MAL (formato Jikan v4) con errori leggibili.
    Le riprove sugli errori transitori (incluso il 504) le fa sources.http_get."""
    try:
        return json.loads(sources.http_get(url, timeout=20))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        if e.code in (502, 503, 504):
            raise RuntimeError(
                "MyAnimeList non risponde al momento (errore %d). Riprova tra qualche istante." % e.code) from None
        if e.code == 429:
            raise RuntimeError(
                "Troppe richieste a MyAnimeList: attendi qualche secondo e riprova.") from None
        raise RuntimeError("Errore da MyAnimeList (HTTP %d)." % e.code) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        raise RuntimeError(
            "Impossibile contattare MyAnimeList. Controlla la connessione e riprova.") from None


def _mal_api_base():
    """Indirizzo del servizio pubblico MAL. Viene da sources.py, che si aggiorna da
    solo: così un cambio di servizio non richiede un binario nuovo."""
    return str(getattr(sources, "MAL_API_BASE", "") or "https://api.tenrai.org/v1").rstrip("/")


def _jikan_fetch(mid, title):
    """Voce manga dal servizio pubblico MAL (formato Jikan v4): per ID se c'è,
    altrimenti per titolo. None = non trovato.
    Nota: Jikan (api.jikan.moe) è stato chiuso il 1/10/2026; ora si usa Tenrai,
    che ne mantiene lo stesso formato."""
    base = _mal_api_base()
    if mid:
        return _jikan(base + "/manga/" + urllib.parse.quote(mid)).get("data") or None
    arr = _jikan(base + "/manga?limit=1&q=" + urllib.parse.quote(title)).get("data") or []
    return arr[0] if arr else None


# ---------- API ufficiale MyAnimeList (facoltativa, serve un Client ID) ----------
MAL_V2 = "https://api.myanimelist.net/v2"
MAL_FIELDS = ("id,title,synopsis,genres,num_volumes,num_chapters,status,start_date,end_date,"
              "mean,media_type,alternative_titles,main_picture,authors{first_name,last_name},"
              "serialization{name}")


_CID_CACHE = {"v": None, "ts": 0.0}


def _mal_client_id():
    """Client ID salvato dall'utente nelle Impostazioni (mai nel repo).
    Memorizzato per qualche secondo: durante una scansione evita di
    rileggere library.json a ogni singolo manga."""
    now = time.time()
    if _CID_CACHE["v"] is not None and now - _CID_CACHE["ts"] < 10:
        return _CID_CACHE["v"]
    try:
        v = str((load_data().get("settings") or {}).get("malClientId") or "").strip()
    except Exception:
        v = ""
    _CID_CACHE["v"], _CID_CACHE["ts"] = v, now
    return v


def _mal_official_get(url, cid):
    try:
        return json.loads(sources.http_get(url, timeout=20, headers={"X-MAL-CLIENT-ID": cid}))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        if e.code in (401, 403):
            raise RuntimeError(
                "Client ID di MyAnimeList non valido o non autorizzato: controllalo nelle Impostazioni.") from None
        raise RuntimeError("Errore dall'API ufficiale MyAnimeList (HTTP %d)." % e.code) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        raise RuntimeError("Impossibile contattare l'API ufficiale di MyAnimeList.") from None


def _mal_people(lst):
    out = []
    for x in lst or []:
        n = x.get("node") or {}
        nm = " ".join(p for p in (n.get("last_name"), n.get("first_name")) if p).strip() or n.get("name") or ""
        if nm:
            role = x.get("role") or ""
            out.append({"name": nm + (" (%s)" % role if role else "")})
    return out


_MAL_STATUS = {"finished": "Finished", "currently_publishing": "Publishing",
               "not_yet_published": "Not yet published", "on_hiatus": "On Hiatus",
               "discontinued": "Discontinued"}


def _mal_v2_to_jikan(d):
    """Converte la risposta dell'API ufficiale nella stessa forma usata per Jikan,
    così tutto il codice a valle (normalize_mal, mal_genres) resta invariato.
    Nota: MAL v2 restituisce generi, temi e demografica in un unico elenco."""
    alt = d.get("alternative_titles") or {}
    sd, ed = d.get("start_date") or "", d.get("end_date") or ""
    pub = (sd + (" to " + ed if ed else " to ?")) if sd else ""
    pic = d.get("main_picture") or {}
    return {
        "mal_id": d.get("id"),
        "url": "https://myanimelist.net/manga/%s" % (d.get("id") or ""),
        "title": d.get("title") or "",
        "title_english": alt.get("en") or "",
        "title_japanese": alt.get("ja") or "",
        "title_synonyms": alt.get("synonyms") or [],
        "synopsis": d.get("synopsis") or "",
        "type": (d.get("media_type") or "").replace("_", " ").title() or None,
        "volumes": d.get("num_volumes") or None,
        "chapters": d.get("num_chapters") or None,
        "status": _MAL_STATUS.get(d.get("status") or "",
                                  (d.get("status") or "").replace("_", " ").title()) or None,
        "published": {"string": pub},
        "score": d.get("mean"),
        "genres": [{"mal_id": g.get("id"), "name": g.get("name")}
                   for g in (d.get("genres") or []) if g.get("name")],
        "themes": [], "demographics": [],
        "authors": _mal_people(d.get("authors")),
        "serializations": [{"name": (s.get("node") or {}).get("name", "")}
                           for s in (d.get("serialization") or []) if (s.get("node") or {}).get("name")],
        "images": {"jpg": {"image_url": pic.get("large") or pic.get("medium") or ""}},
    }


def _mal_official_fetch(mid, title, cid):
    if mid:
        d = _mal_official_get("%s/manga/%s?fields=%s" % (MAL_V2, urllib.parse.quote(mid), MAL_FIELDS), cid)
        return _mal_v2_to_jikan(d) if d.get("id") else None
    r = _mal_official_get("%s/manga?limit=1&q=%s&fields=%s"
                          % (MAL_V2, urllib.parse.quote(title[:64]), MAL_FIELDS), cid)
    arr = r.get("data") or []
    node = (arr[0].get("node") if arr else None) or None
    return _mal_v2_to_jikan(node) if node else None


# ---------- cache locale dei metadati (diventa il catalogo offline) ----------
MAL_CACHE_FILE = os.path.join(DATA_DIR, "mal-cache.json")
MAL_CACHE_TTL = 30 * 24 * 3600      # i metadati cambiano di rado
_MAL_CACHE = None
_MAL_CACHE_DIRTY = False
_MAL_CACHE_LOCK = threading.Lock()


def _mal_cache():
    global _MAL_CACHE
    if _MAL_CACHE is None:
        try:
            with open(MAL_CACHE_FILE, "r", encoding="utf-8") as f:
                c = json.load(f)
            _MAL_CACHE = c if isinstance(c, dict) else {}
        except Exception:
            _MAL_CACHE = {}
    return _MAL_CACHE


def _mal_cache_get(key):
    """Ritorna (dati, fresca). dati=None se assente."""
    with _MAL_CACHE_LOCK:
        e = _mal_cache().get(key)
    if not isinstance(e, dict) or "data" not in e:
        return None, False
    return e["data"], (time.time() - float(e.get("ts") or 0)) < MAL_CACHE_TTL


def _mal_cache_put(key, data):
    global _MAL_CACHE_DIRTY
    with _MAL_CACHE_LOCK:
        _mal_cache()[key] = {"ts": time.time(), "data": data}
        _MAL_CACHE_DIRTY = True


def mal_cache_flush():
    global _MAL_CACHE_DIRTY
    with _MAL_CACHE_LOCK:
        if not _MAL_CACHE_DIRTY or _MAL_CACHE is None:
            return
        try:
            tmp = "%s.%d.tmp" % (MAL_CACHE_FILE, os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_MAL_CACHE, f, ensure_ascii=False)
            os.replace(tmp, MAL_CACHE_FILE)
            _MAL_CACHE_DIRTY = False
        except Exception:
            pass


def mal_fetch_ex(mid, title):
    """Risolve una voce MAL. Ritorna (dati|None, da_cache).
    Ordine: cache fresca -> API ufficiale (se c'è il Client ID) -> Jikan -> cache scaduta.
    Solleva un'eccezione solo se ogni strada fallisce e non c'è nulla in cache."""
    mid = str(mid or "").strip()
    title = str(title or "").strip()
    if not mid and not title:
        return None, False
    key = ("id:" + mid) if mid else ("q:" + title.lower())
    cached, fresh = _mal_cache_get(key)
    if cached is not None and fresh:
        return cached, True

    err = None
    cid = _mal_client_id()
    if cid:
        try:
            d = _mal_official_fetch(mid, title, cid)
            if d:
                _mal_cache_put(key, d)
                return d, False
            return None, False          # l'API ufficiale ha risposto: davvero non esiste
        except Exception as e:
            err = e                      # ufficiale ko: si prova Jikan
    try:
        d = _jikan_fetch(mid, title)
        if d:
            _mal_cache_put(key, d)
            return d, False
        if not err:
            return None, False          # Jikan ha risposto: davvero non esiste
    except Exception as e:
        err = err or e

    if cached is not None:
        return cached, True             # dato vecchio, meglio di un errore
    if err:
        raise err
    return None, False


def mal_fetch(mid, title):
    """Compatibilità: solo i dati (None se non trovato)."""
    return mal_fetch_ex(mid, title)[0]


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
        "genres": mal_genres(d),
        "genreIds": mal_genre_ids(d),
        "info": info,
    }


# 4005 = goroawase di "manga" (4=yo, 0=ma, 0=n, 5=ga)
PORT_CANDIDATES = [4005, 4456, 5577, 6680, 7788, 8123, 9123, 0]
CURRENT_PORT = 0
DATA_FILE = os.path.join(DATA_DIR, "library.json")   # dati condivisi
INDEX_FILE = os.path.join(HERE, "index.html")        # codice/app
VERSION_FILE = os.path.join(HERE, "version.json")    # codice/app
DL = downloads.Manager(DATA_DIR)                      # download condivisi
EXPORT = downloads.ExportManager(DATA_DIR)            # esportazione ZIP
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


# ============================================================
#  Accesso da altri dispositivi della rete di casa (es. iPhone)
# ============================================================
# Regole di sicurezza:
#  - SPENTO di base: il server ascolta solo su 127.0.0.1, quindi dalla rete non è
#    nemmeno raggiungibile (come sempre stato).
#  - ACCESO: ascolta sulla rete, ma accetta solo indirizzi privati (rete di casa),
#    solo con PIN, e le azioni delicate restano possibili solo dal computer.
#  - Le impostazioni di sicurezza (lan*) si cambiano SOLO da /lan sul computer:
#    POST /data non può modificarle e GET /data non espone hash né sale.
#  - Controllo dell'intestazione Host (anche a interruttore spento): blocca i siti
#    web che provano a parlare con l'app tramite "DNS rebinding".
LAN_KEYS = ("lanAccess", "lanPinHash", "lanSalt")
LAN_ACTIVE = False            # vero se il server è davvero in ascolto sulla rete
LAN_PIN_RE = r"^\d{4,8}$"
_LAN_LOCK = threading.Lock()
_LAN_FAILS = {}               # ip -> [tentativi falliti, bloccato fino a]
_LAN_CACHE = {"v": None, "ts": 0.0}

# operazioni consentite solo dal computer su cui gira l'app
LOCAL_ONLY = {"/update/apply", "/restart", "/quit", "/downloads/purge", "/download/delete",
              "/cache/clear", "/exports/clear", "/lan", "/lan/info"}
# raggiungibili dalla rete anche senza PIN (pagina di accesso e icona)
LAN_PUBLIC = {"/lan/login", "/icon", "/manifest.json"}


def lan_settings(fresh=False):
    """(acceso, hash del PIN, sale) letti da library.json, memorizzati per 3 s."""
    now = time.time()
    if not fresh and _LAN_CACHE["v"] is not None and now - _LAN_CACHE["ts"] < 3:
        return _LAN_CACHE["v"]
    try:
        s = load_data().get("settings") or {}
    except Exception:
        s = {}
    v = (bool(s.get("lanAccess")), str(s.get("lanPinHash") or ""), str(s.get("lanSalt") or ""))
    _LAN_CACHE["v"], _LAN_CACHE["ts"] = v, now
    return v


def _pin_hash(salt, pin):
    return hashlib.sha256((salt + ":" + pin).encode("utf-8")).hexdigest()


def _lan_token(pin_hash):
    """Valore del cookie di accesso: resta valido finché non cambi il PIN."""
    return hmac.new(pin_hash.encode("utf-8"), b"makreader-lan-v1", hashlib.sha256).hexdigest()


def _norm_ip(ip):
    ip = str(ip or "")
    return ip[7:] if ip.startswith("::ffff:") else ip


def _is_loopback(ip):
    try:
        return ipaddress.ip_address(_norm_ip(ip)).is_loopback
    except ValueError:
        return False


def _is_home_network(ip):
    try:
        a = ipaddress.ip_address(_norm_ip(ip))
    except ValueError:
        return False
    return (a.is_private or a.is_link_local) and not a.is_loopback


def _host_allowed(host_header):
    """Nomi ammessi nell'intestazione Host: localhost, indirizzi IP privati e nomi
    della rete locale (.local, .lan, .home, nomi senza punti). Un sito esterno che
    tenta il DNS rebinding usa per forza un dominio pubblico, che qui viene rifiutato."""
    h = str(host_header or "").strip().lower()
    if not h:
        return False
    if h.startswith("["):
        name = h[1:h.find("]")] if "]" in h else h
    elif h.count(":") == 1:
        name = h.split(":", 1)[0]
    else:
        name = h
    name = name.rstrip(".")
    if name in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        a = ipaddress.ip_address(name)
        return a.is_loopback or a.is_private or a.is_link_local
    except ValueError:
        pass
    if "." not in name:
        return True
    return name.endswith((".local", ".lan", ".home", ".home.arpa", ".internal", ".fritz.box"))


def lan_addresses(port):
    """Indirizzi da aprire sul telefono: nome .local del computer e IP di rete."""
    out = []
    name = ""
    if sys.platform == "darwin":
        try:
            name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True,
                                  text=True, timeout=3).stdout.strip()
        except Exception:
            name = ""
    if not name:
        name = (socket.gethostname() or "").split(".")[0]
    if name:
        out.append("http://%s.local:%d" % (name, port))
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))   # nessun pacchetto inviato: serve solo a
            ip = s.getsockname()[0]            # sapere quale scheda di rete si usa
        if ip and not ip.startswith("127."):
            out.append("http://%s:%d" % (ip, port))
    except Exception:
        pass
    return out


LOGIN_PAGE = """<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="MakReader">
<link rel="apple-touch-icon" href="/icon"><title>MakReader</title>
<style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f1115;color:#e7e9ee;font:16px -apple-system,system-ui,sans-serif}
.box{width:min(320px,88vw);text-align:center}
img{width:72px;height:72px;border-radius:16px}
h1{font-size:20px;margin:14px 0 4px}p{color:#9aa3b2;font-size:14px;margin:0 0 18px}
input{width:100%;box-sizing:border-box;font-size:24px;letter-spacing:8px;text-align:center;padding:12px;border-radius:12px;border:1px solid #2a2f3a;background:#171a21;color:#fff}
button{width:100%;margin-top:12px;padding:13px;border:0;border-radius:12px;background:#7c5cff;color:#fff;font-size:16px;font-weight:700}
.err{color:#e0a13a;font-size:14px;min-height:20px;margin-top:10px}
</style></head><body><form class="box" id="f">
<img src="/icon" alt="" onerror="this.style.display='none'">
<h1>MakReader</h1><p>Inserisci il PIN scelto nelle Impostazioni del computer.</p>
<input id="pin" type="password" inputmode="numeric" autocomplete="current-password" maxlength="8" autofocus>
<button type="submit">Entra</button><div class="err" id="e"></div></form>
<script>
document.getElementById('f').onsubmit=async function(ev){ev.preventDefault();
  var e=document.getElementById('e');e.textContent='';
  try{var r=await fetch('/lan/login',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({pin:document.getElementById('pin').value})});
      var d=await r.json();
      if(d.ok){location.replace('/');return;}
      e.textContent=d.error||'PIN errato';}
  catch(x){e.textContent='Computer non raggiungibile.';}
  document.getElementById('pin').value='';};
</script></body></html>"""


def _schedule_restart():
    """Riavvia il programma (serve per cambiare l'indirizzo su cui ascolta)."""
    def _restart():
        try:
            # "restarted" dice al nuovo processo di NON riaprire il browser
            if getattr(sys, "frozen", False):
                os.execv(sys.executable, [sys.executable, str(CURRENT_PORT), "restarted"])
            else:
                os.execv(sys.executable, [sys.executable, os.path.abspath(__file__),
                                          str(CURRENT_PORT), "restarted"])
        except Exception:
            os._exit(0)
    threading.Timer(0.5, _restart).start()


class Handler(BaseHTTPRequestHandler):
    def _gate(self, path):
        """Decide se la richiesta può passare. Se no, risponde già e ritorna False."""
        ip = _norm_ip(self.client_address[0] if self.client_address else "")
        if not _host_allowed(self.headers.get("Host")):
            self._send(403, "Indirizzo non ammesso.", "text/plain; charset=utf-8")
            return False
        if _is_loopback(ip):
            return True                       # dal computer stesso: nessun limite
        enabled, pin_hash, _salt = lan_settings()
        if not (enabled and LAN_ACTIVE and pin_hash) or not _is_home_network(ip):
            self._send(403, "Accesso non consentito.", "text/plain; charset=utf-8")
            return False
        if path in LOCAL_ONLY:
            self._json({"error": "Questa operazione si può fare solo dal computer su cui gira MakReader."}, 403)
            return False
        if path in LAN_PUBLIC:
            return True
        cookie = self.headers.get("Cookie") or ""
        token = _lan_token(pin_hash)
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "mkr_lan" and hmac.compare_digest(v.strip(), token):
                return True
        if self.command == "GET" and path in ("/", "/index.html"):
            self._send(200, LOGIN_PAGE, "text/html; charset=utf-8")
        else:
            self._json({"error": "PIN richiesto", "needPin": True}, 401)
        return False

    def _lan_login(self, raw):
        """Verifica il PIN da un altro dispositivo. Dopo 5 errori blocca quel
        dispositivo per 5 minuti, così il PIN non si può indovinare per tentativi."""
        ip = _norm_ip(self.client_address[0] if self.client_address else "")
        now = time.time()
        with _LAN_LOCK:
            rec = _LAN_FAILS.get(ip)
            if rec and rec[1] > now:
                minuti = max(1, int((rec[1] - now) // 60) + 1)
                self._json({"error": "Troppi tentativi: riprova fra %d minut%s."
                                     % (minuti, "o" if minuti == 1 else "i")}, 429)
                return
        try:
            pin = str((json.loads(raw) or {}).get("pin") or "").strip()
        except Exception:
            pin = ""
        _enabled, pin_hash, salt = lan_settings(fresh=True)
        ok = bool(pin_hash and pin) and hmac.compare_digest(_pin_hash(salt, pin), pin_hash)
        if ok:
            with _LAN_LOCK:
                _LAN_FAILS.pop(ip, None)
            cookie = ("mkr_lan=%s; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict"
                      % _lan_token(pin_hash))
            self._send(200, json.dumps({"ok": True}), extra={"Set-Cookie": cookie})
            return
        with _LAN_LOCK:
            rec = _LAN_FAILS.setdefault(ip, [0, 0.0])
            rec[0] += 1
            restano = 5 - rec[0]
            if rec[0] >= 5:
                rec[0], rec[1] = 0, now + 300
                self._json({"error": "PIN errato. Troppi tentativi: riprova fra 5 minuti."}, 401)
                return
        self._json({"error": "PIN errato (%d tentativ%s rimast%s)."
                             % (restano, "o" if restano == 1 else "i", "o" if restano == 1 else "i")}, 401)

    def _lan_set(self, raw):
        """Accende/spegne l'accesso dalla rete e imposta il PIN. Solo dal computer
        (il controllo è in _gate: '/lan' è fra le operazioni LOCAL_ONLY)."""
        try:
            body = json.loads(raw) or {}
        except Exception:
            body = {}
        enabled = bool(body.get("enabled"))
        pin = str(body.get("pin") or "").strip()
        d = load_data()
        st = d.setdefault("settings", {})
        if pin:
            if not re.match(LAN_PIN_RE, pin):
                self._json({"error": "Il PIN deve avere da 4 a 8 cifre."}, 400)
                return
            salt = secrets.token_hex(16)
            st["lanSalt"] = salt
            st["lanPinHash"] = _pin_hash(salt, pin)   # cambiare PIN scollega i telefoni
        if enabled and not st.get("lanPinHash"):
            self._json({"error": "Scegli prima un PIN."}, 400)
            return
        st["lanAccess"] = enabled
        save_data(d)
        lan_settings(fresh=True)
        riavvio = enabled != LAN_ACTIVE
        self._json({"ok": True, "restart": riavvio})
        if riavvio:
            _schedule_restart()   # per mettersi in ascolto (o smettere) sulla rete

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
        if not self._gate(path):
            return

        if path == "/lan/info":
            enabled, pin_hash, _ = lan_settings(fresh=True)
            self._json({"enabled": enabled, "active": LAN_ACTIVE, "hasPin": bool(pin_hash),
                        "port": CURRENT_PORT, "addresses": lan_addresses(CURRENT_PORT)})
            return

        if path == "/manifest.json":
            # permette di aggiungere l'app alla schermata Home del telefono
            self._json({"name": "MakReader", "short_name": "MakReader", "start_url": "/",
                        "display": "standalone", "background_color": "#0f1115",
                        "theme_color": "#0f1115",
                        "icons": [{"src": "/icon", "sizes": "512x512", "type": "image/png"}]})
            return

        if path in ("/", "/index.html"):
            try:
                with open(INDEX_FILE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(500, "index.html mancante", "text/plain")
            return

        if path == "/data":
            d = load_data()
            st = d.get("settings")
            if isinstance(st, dict):
                # hash e sale del PIN non escono mai dal computer
                st.pop("lanPinHash", None)
                st.pop("lanSalt", None)
            self._json(d)
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
                data = mal_fetch(mid, title)
                mal_cache_flush()
                if not data:
                    self._json({"found": False})
                else:
                    self._json({"found": True, "mal": normalize_mal(data)})
            except Exception as e:
                # errore != "non trovato": il client deve poterli distinguere
                self._json({"found": False, "error": str(e), "failed": True}, 502)
            return

        if path == "/feed":
            # ultime uscite: una fonte (source=id) o più (sources=id1,id2,...),
            # interrogate in parallelo e mischiate per orario (più recenti prima)
            one = q.get("source", [""])[0]
            multi = q.get("sources", [""])[0]
            ids = [one] if (one and one != "__all__") else [s for s in multi.split(",") if s]
            ids = [i for i in ids if i in sources.REGISTRY]

            def _feed(sid):
                try:
                    return [dict(it, source=sid) for it in sources.REGISTRY[sid].latest_feed(30)]
                except Exception:
                    return []
            out = []
            if ids:
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(ids))) as ex:
                    for res in ex.map(_feed, ids):
                        out.extend(res)
            out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
            self._json({"items": out[:80]})
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
                # "binaryVersion" = ultima versione che richiede un binario nuovo
                # (cioè in cui è cambiato mangareader.py, che NON si auto-aggiorna).
                # Se è più recente del binario installato, serve riscaricare l'app.
                binver = str(remote.get("binaryVersion", "0"))
                needs_binary = version_tuple(binver) > version_tuple(APP_VERSION)
                self._json({"configured": True, "current": cur, "latest": lat,
                            "update": newer, "needsBinary": needs_binary,
                            "appVersion": APP_VERSION, "notes": remote.get("notes", ""),
                            "releasesUrl": "https://github.com/%s/releases/latest" % UPDATE_REPO})
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

        if path == "/storage":
            # riepilogo dello spazio occupato in ~/MakReader-dati
            try:
                def fsize(p):
                    try:
                        return os.path.getsize(p)
                    except OSError:
                        return 0
                u = DL.usage()
                items = DL.status()
                data = load_data()
                follows = data.get("follows") or {}
                progress = data.get("progress") or {}
                letti = set()
                for k, pr in progress.items():
                    for cid in (pr.get("readChapters") or []):
                        letti.add(str(cid))
                # raggruppa per manga: quanto occupa ciascuno e quanto è già letto
                gruppi = {}
                for it in items:
                    k = it.get("id")
                    b = u["perItem"].get(k, 0)
                    mk = it.get("manga_key") or ""
                    g = gruppi.setdefault(mk, {"mangaKey": mk, "title": it.get("title") or "?",
                                               "source": it.get("source") or "",
                                               "chapters": 0, "bytes": 0,
                                               "readChapters": 0, "readBytes": 0,
                                               "inLibrary": mk in follows})
                    g["chapters"] += 1
                    g["bytes"] += b
                    if str(it.get("chapter_id")) in letti:
                        g["readChapters"] += 1
                        g["readBytes"] += b
                orfani_lib = [g for g in gruppi.values() if not g["inLibrary"]]
                cache_entries = 0
                try:
                    with _MAL_CACHE_LOCK:
                        cache_entries = len(_mal_cache())
                except Exception:
                    pass
                exp = EXPORT.usage()
                self._json({
                    "dataDir": DATA_DIR,
                    "library": {"bytes": fsize(DATA_FILE) + fsize(DATA_FILE + ".bak"),
                                "manga": len(follows)},
                    "malCache": {"bytes": fsize(MAL_CACHE_FILE), "entries": cache_entries},
                    "downloads": {"bytes": u["total"], "chapters": len(items),
                                  "groups": sorted(gruppi.values(),
                                                   key=lambda g: g["bytes"], reverse=True)},
                    "readBytes": sum(g["readBytes"] for g in gruppi.values()),
                    "readChapters": sum(g["readChapters"] for g in gruppi.values()),
                    "notInLibrary": {"bytes": sum(g["bytes"] for g in orfani_lib),
                                     "chapters": sum(g["chapters"] for g in orfani_lib)},
                    "orphanFiles": {"bytes": u["orphanBytes"], "folders": len(u["orphans"])},
                    "exports": {"bytes": exp["bytes"], "count": exp["count"]},
                    "total": (u["total"] + u["orphanBytes"] + exp["bytes"]
                              + fsize(DATA_FILE) + fsize(DATA_FILE + ".bak") + fsize(MAL_CACHE_FILE)),
                })
            except Exception as e:
                self._json({"error": str(e)}, 500)
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

        if path == "/export/status":
            j = EXPORT.status(q.get("job", [""])[0])
            if not j:
                self._json({"error": "job sconosciuto"}, 404)
            else:
                self._json({"status": j["status"], "done": j["done"],
                            "total": j["total"], "error": j["error"]})
            return

        if path == "/export/file":
            fp, fname = EXPORT.ready_file(q.get("job", [""])[0])
            if not fp:
                self._send(404, "zip non pronto", "text/plain")
                return
            with open(fp, "rb") as f:
                self._send(200, f.read(), "application/zip",
                           extra={"Content-Disposition": 'attachment; filename="%s"' % fname})
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
        if not self._gate(p.path):
            return
        if p.path == "/lan/login":
            self._lan_login(raw)
            return
        if p.path == "/lan":
            self._lan_set(raw)
            return
        if p.path == "/data":
            try:
                incoming = json.loads(raw)
                if not isinstance(incoming, dict):
                    raise ValueError("dati non validi")
                # le impostazioni di sicurezza si cambiano solo da /lan: qui si
                # conservano quelle già salvate, qualunque cosa arrivi dal browser
                cur = load_data().get("settings") or {}
                st = incoming.get("settings")
                if not isinstance(st, dict):
                    st = incoming["settings"] = {}
                for k in LAN_KEYS:
                    if k in cur:
                        st[k] = cur[k]
                    else:
                        st.pop(k, None)
                save_data(incoming)
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
        if p.path == "/check":
            # Controllo aggiornamenti in blocco: una sola richiesta dal browser,
            # il motore verifica i manga in parallelo (con un tetto per sorgente,
            # per non farsi bloccare). Molto più veloce di 1 richiesta per manga.
            try:
                body = json.loads(raw)
                items = body.get("items", []) if isinstance(body, dict) else \
                    (body if isinstance(body, list) else [])
                sems, sem_lock = {}, threading.Lock()

                def _sem(src):
                    with sem_lock:
                        s = sems.get(src)
                        if s is None:
                            s = sems[src] = threading.Semaphore(8)  # max 8 insieme per sorgente
                        return s

                def _one(it):
                    key = it.get("key")
                    src = sources.REGISTRY.get(it.get("source"))
                    if not src:
                        return key, None
                    with _sem(it.get("source")):
                        try:
                            return key, src.latest(it.get("id"))
                        except Exception:
                            return key, None  # errore: lato client si lascia invariato
                results = {}
                with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
                    for k, n in ex.map(_one, items):
                        if k is not None:
                            results[k] = n
                self._json({"results": results})
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
        if p.path == "/export/start":
            # avvia la preparazione di uno ZIP in background; restituisce il jobId
            try:
                body = json.loads(raw)
                title = body.get("title") or "manga"
                chapters = body.get("chapters") or []
                if not chapters:
                    self._json({"error": "nessun capitolo"}, 400)
                    return
                jid = EXPORT.start(title, chapters, body.get("source") or "")
                self._json({"jobId": jid})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return
        if p.path == "/mal/scan":
            # scarica i generi da MAL per un blocco di manga (uno per volta, con
            # una pausa: MAL/Jikan limita le richieste). Restituisce {key: [generi]}.
            try:
                body = json.loads(raw)
                items = body.get("items", []) if isinstance(body, dict) else []
                results = {}
                for it in items:
                    key = it.get("key")
                    mid = str(it.get("malId") or "").strip()
                    title = str(it.get("title") or "").strip()
                    if not key or (not mid and not title):
                        continue
                    # esito ESPLICITO: il client deve poter distinguere
                    # "nessun genere" da "chiamata fallita", altrimenti un errore
                    # di rete verrebbe scritto come dato definitivo.
                    try:
                        data, cached = mal_fetch_ex(mid, title)
                        if data:
                            results[key] = {"ok": True,
                                            "malId": str(data.get("mal_id") or ""),
                                            "genres": mal_genres(data),
                                            "genreIds": mal_genre_ids(data)}
                        else:
                            results[key] = {"notfound": True}
                    except Exception as e:
                        results[key] = {"error": str(e)}
                        cached = False
                    if not cached:
                        # rispetta il limite del servizio pubblico (60 richieste/min)
                        time.sleep(float(getattr(sources, "MAL_API_PAUSE", 1.05) or 1.05))
                mal_cache_flush()
                self._json({"results": results})
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
        if p.path == "/downloads/purge":
            # cancellazioni in blocco. La modalità è SEMPRE esplicita: nessuna
            # scorciatoia che possa cancellare più di quanto l'utente ha chiesto.
            try:
                body = json.loads(raw) if raw else {}
                mode = str(body.get("mode") or "")
                items = DL.status()
                data = load_data()
                follows = data.get("follows") or {}
                letti = set()
                for k, pr in (data.get("progress") or {}).items():
                    for cid in (pr.get("readChapters") or []):
                        letti.add(str(cid))
                ids, n_orf, freed_orf = [], 0, 0
                if mode == "ids":
                    voluti = set(str(x) for x in (body.get("ids") or []))
                    ids = [it["id"] for it in items if it["id"] in voluti]
                elif mode == "manga":
                    mk = str(body.get("mangaKey") or "")
                    if not mk:
                        self._json({"error": "manga non indicato"}, 400)
                        return
                    ids = [it["id"] for it in items if (it.get("manga_key") or "") == mk]
                elif mode == "read":
                    ids = [it["id"] for it in items if str(it.get("chapter_id")) in letti]
                elif mode == "notinlibrary":
                    ids = [it["id"] for it in items if (it.get("manga_key") or "") not in follows]
                elif mode == "errors":
                    ids = [it["id"] for it in items if it.get("status") == "error"]
                elif mode == "all":
                    ids = [it["id"] for it in items]
                elif mode == "orphanfiles":
                    n_orf, freed_orf = DL.delete_orphan_folders()
                else:
                    self._json({"error": "modalità non valida"}, 400)
                    return
                n, freed = DL.delete_many(ids)
                self._json({"ok": True, "deleted": n + n_orf, "freed": freed + freed_orf})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return
        if p.path == "/exports/clear":
            try:
                n, freed = EXPORT.clear()
                self._json({"ok": True, "deleted": n, "freed": freed})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return
        if p.path == "/cache/clear":
            # svuota la cache metadati: si ricostruisce da sola alla prossima apertura
            global _MAL_CACHE, _MAL_CACHE_DIRTY
            try:
                freed = 0
                try:
                    freed = os.path.getsize(MAL_CACHE_FILE)
                except OSError:
                    pass
                with _MAL_CACHE_LOCK:
                    _MAL_CACHE = {}
                    _MAL_CACHE_DIRTY = False
                    try:
                        if os.path.exists(MAL_CACHE_FILE):
                            os.remove(MAL_CACHE_FILE)
                    except OSError:
                        pass
                self._json({"ok": True, "freed": freed})
            except Exception as e:
                self._json({"error": str(e)}, 400)
            return
        if p.path == "/quit":
            self._json({"ok": True})
            threading.Timer(0.4, lambda: os._exit(0)).start()
            return
        if p.path == "/restart":
            self._json({"ok": True})
            _schedule_restart()
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


def is_free(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port)); return True
        except OSError:
            return False


def pick_port(host="127.0.0.1"):
    wanted = next((a for a in sys.argv[1:] if a.isdigit()), None)
    if wanted:
        p = int(wanted)
        # dopo un riavvio la vecchia porta può restare occupata un istante
        for _ in range(30):
            if is_free(p, host):
                return p
            time.sleep(0.1)
        print("  La porta %d è occupata, ne cerco un'altra…" % p)
    for p in PORT_CANDIDATES:
        if p == 0 or is_free(p, host):
            return p
    return 0


def main():
    global CURRENT_PORT, LAN_ACTIVE
    # Accesso da altri dispositivi: solo se acceso E con un PIN impostato si ascolta
    # sulla rete (0.0.0.0). Altrimenti solo sul computer, come sempre.
    enabled, pin_hash, _ = lan_settings(fresh=True)
    host = "0.0.0.0" if (enabled and pin_hash) else "127.0.0.1"
    try:
        server = ThreadingHTTPServer((host, pick_port(host)), Handler)
    except OSError:
        host = "127.0.0.1"   # se la rete non è disponibile si ripiega sul solo computer
        server = ThreadingHTTPServer((host, pick_port(host)), Handler)
    LAN_ACTIVE = host != "127.0.0.1"
    port = server.server_address[1]
    CURRENT_PORT = port
    url = "http://localhost:%d" % port
    print("\n  MakReader avviato!  Apri:  %s\n  (premi Ctrl+C per fermare)\n" % url, flush=True)
    # apri il browser solo all'avvio normale, NON dopo un riavvio (la scheda si ricarica già)
    if "restarted" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Arresto MakReader. A presto!")
        server.shutdown()


if __name__ == "__main__":
    main()
