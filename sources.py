"""
MakReader - sorgenti manga (sistema a plugin).

Ogni sorgente espone:
  search(query)      -> [{"id":<manga-url>, "title":..., "cover":..., "status":...}]
  details(manga_id)  -> {"title","cover","description","chapters":[{"id","name","number"}]}
  pages(chapter_id)  -> {"pages":[img_url,...], "referer": <str>}

"id" è sempre l'URL/identificatore che la sorgente sa risolvere.
Le immagini vengono servite dal proxy del server (con Referer corretto).
"""

import re
import ssl
import json
import html as _html
import urllib.request
import urllib.parse

# ---- SSL robusto (risolve il classico problema certificati di Python su macOS) ----
try:
    _CTX = ssl.create_default_context()
except Exception:
    _CTX = None
_CTX_INSECURE = ssl._create_unverified_context()

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


def http_get(url, referer=None, cookie=None, timeout=25, binary=False):
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9",
               "Accept": "text/html,application/xhtml+xml,application/json,*/*"}
    if referer:
        headers["Referer"] = referer
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    last = None
    for ctx in (_CTX, _CTX_INSECURE):
        if ctx is None:
            continue
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                data = r.read()
                return data if binary else data.decode("utf-8", "replace")
        except ssl.SSLError as e:
            last = e
            continue
        except Exception as e:
            raise
    raise last if last else RuntimeError("richiesta fallita")


def _abs(u, base):
    if not u:
        return ""
    u = u.strip()
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http"):
        return u
    if u.startswith("/"):
        return base.rstrip("/") + u
    return base.rstrip("/") + "/" + u


def _clean(t):
    return _html.unescape(re.sub(r"\s+", " ", t or "")).strip()


# ============================================================
#  Dean Edwards p.a.c.k.e.r unpacker (serve a fanfox/mangahere)
# ============================================================
def _unbase(val, radix):
    alpha = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if radix <= 36:
        try:
            return int(val, radix)
        except ValueError:
            return 0
    table = {c: i for i, c in enumerate(alpha)}
    n = 0
    for ch in val:
        n = n * radix + table.get(ch, 0)
    return n


def unpack_js(source):
    m = re.search(r"\}\('(.*?)',\s*(\d+),\s*(\d+),\s*'(.*?)'\.split\('\|'\)",
                  source, re.DOTALL)
    if not m:
        return source
    payload, radix, count, symtab = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|")
    payload = payload.replace("\\'", "'").replace("\\\\", "\\")

    def repl(mt):
        word = mt.group(0)
        idx = _unbase(word, radix)
        if 0 <= idx < len(symtab) and symtab[idx]:
            return symtab[idx]
        return word
    return re.sub(r"\b\w+\b", repl, payload)


# ============================================================
#  Sorgente base
# ============================================================
class Source:
    id = ""
    name = ""
    base = ""
    lang = "en"
    note = ""

    def search(self, query):
        raise NotImplementedError

    def details(self, manga_id):
        raise NotImplementedError

    def pages(self, chapter_id):
        raise NotImplementedError


# ============================================================
#  WeebCentral  (successore di MangaSee) - completo e affidabile
# ============================================================
class WeebCentral(Source):
    id = "weebcentral"
    name = "WeebCentral"
    base = "https://weebcentral.com"
    note = "Successore di MangaSee. Stabile."

    def search(self, query):
        url = (self.base + "/search/data?author=&text=" + urllib.parse.quote(query) +
               "&sort=Best+Match&order=Descending&official=Any&anime=Any&adult=Any"
               "&display_mode=Full+Display")
        html = http_get(url, referer=self.base + "/")
        out, seen = [], set()
        for m in re.finditer(r'href="(https://weebcentral\.com/series/([0-9A-Za-z]+)/[^"]+)"', html):
            surl, sid = m.group(1), m.group(2)
            if sid in seen:
                continue
            seen.add(sid)
            block = html[m.start():m.start() + 1800]
            ta = re.search(r'alt="([^"]+?)\s+cover"', block)
            title = _clean(ta.group(1)) if ta else surl.rsplit("/", 1)[-1].replace("-", " ")
            cm = re.search(r'src="(https?://[^"]+/cover/[^"]+)"', block)
            cover = cm.group(1) if cm else "https://temp.compsci88.com/cover/normal/%s.webp" % sid
            out.append({"id": surl, "title": title, "cover": cover, "status": ""})
        return out

    def details(self, manga_id):
        page = http_get(manga_id, referer=self.base + "/")
        sid = re.search(r"/series/([0-9A-Za-z]+)", manga_id).group(1)
        title = self._og(page, "title") or _clean(re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S).group(1)) if re.search(r"<h1", page) else ""
        title = re.sub(r"\s*\|.*$", "", title or "")
        cover = self._og(page, "image") or "https://temp.compsci88.com/cover/normal/%s.webp" % sid
        dm = re.search(r'<p class="whitespace-pre-wrap[^"]*">(.*?)</p>', page, re.S)
        desc = _clean(re.sub(r"<[^>]+>", "", dm.group(1))) if dm else ""
        if not desc:
            desc = self._og(page, "description") or ""
        chap_html = http_get("%s/series/%s/full-chapter-list" % (self.base, sid), referer=manga_id)
        chapters = []
        for m in re.finditer(r'<a[^>]+href="(https://weebcentral\.com/chapters/[^"]+)"[^>]*>(.*?)</a>',
                             chap_html, re.S):
            curl, txt = m.group(1), _clean(re.sub(r"<[^>]+>", " ", m.group(2)))
            nm = re.search(r"([\d.]+)", txt)
            name = re.sub(r"\s*Last Read.*$", "", txt) or txt
            chapters.append({"id": curl, "name": name, "number": nm.group(1) if nm else ""})
        chapters.reverse()  # dal primo all'ultimo

        def field(label):
            m = re.search(r'<strong>\s*' + re.escape(label) + r'\s*</strong>(.*?)</li>', page, re.S)
            return _clean(re.sub(r"<[^>]+>", " ", m.group(1))) if m else ""
        author = field("Author(s):") or field("Author:")
        status = field("Status:")
        year = field("Released:") or field("Year:")
        gtxt = field("Tag(s):") or field("Tags:")
        genres = [g.strip() for g in re.split(r"[,\n]", gtxt) if g.strip()] if gtxt else []
        atxt = field("Associated Name(s):") or field("Associated Names:")
        alts = [a.strip() for a in re.split(r"\s{2,}|;", atxt) if a.strip()] if atxt else []
        return {"title": _clean(title), "cover": cover, "description": desc, "chapters": chapters,
                "author": author, "status": status, "year": year, "genres": genres,
                "altTitles": alts[:6]}

    def latest(self, manga_id):
        sid = re.search(r"/series/([0-9A-Za-z]+)", manga_id).group(1)
        h = http_get("%s/series/%s/full-chapter-list" % (self.base, sid), referer=manga_id)
        m = re.search(r'/chapters/[^"]+"[^>]*>(.*?)</a>', h, re.S)
        if m:
            nm = re.search(r"([\d.]+)", re.sub(r"<[^>]+>", " ", m.group(1)))
            return nm.group(1) if nm else ""
        return ""

    def pages(self, chapter_id):
        url = chapter_id + "/images?is_prev=False&current_page=1&reading_style=long_strip"
        html = http_get(url, referer=chapter_id)
        pages = []
        for m in re.finditer(r'<img[^>]+src="([^"]+)"', html):
            src = m.group(1)
            if re.search(r"\.(jpg|jpeg|png|webp|gif)", src, re.I) and "/cover/" not in src:
                pages.append(src)
        return {"pages": pages, "referer": self.base + "/"}

    @staticmethod
    def _og(page, prop):
        m = re.search(r'<meta[^>]+property="og:%s"[^>]+content="([^"]*)"' % prop, page)
        if not m:
            m = re.search(r'<meta[^>]+name="%s"[^>]+content="([^"]*)"' % prop, page)
        return _clean(m.group(1)) if m else ""


# ============================================================
#  Famiglia "FMcDN" (fanfox / mangahere) - stesso motore
# ============================================================
class _FMcDN(Source):
    cookie = "isAdult=1"
    mirrors = []

    def _get(self, path_or_url, referer=None):
        url = _abs(path_or_url, self.base)
        return http_get(url, referer=referer or (self.base + "/"), cookie=self.cookie)

    def search(self, query):
        html = self._get("/search?title=" + urllib.parse.quote(query) + "&page=1")
        # Restringi al contenitore dei risultati (evita sidebar / "manga popolari")
        lists = re.findall(r'<ul class="manga-list-\d-list[^"]*">(.*?)</ul>', html, re.S)
        scope = max(lists, key=len) if lists else ""
        out, seen = [], set()
        for li in re.split(r'<li', scope):
            m = re.search(r'href="((?:https?://[^"]*?)?/manga/[A-Za-z0-9_]+/)"[^>]*title="([^"]+)"', li)
            if not m:
                continue
            url = _abs(m.group(1), self.base)
            if url in seen:
                continue
            seen.add(url)
            title = _clean(m.group(2))
            cm = re.search(r'<img[^>]+src="([^"]+\.(?:jpg|jpeg|png|webp)[^"]*)"', li)
            cover = _abs(cm.group(1), self.base) if cm else ""
            out.append({"id": url, "title": title, "cover": cover, "status": ""})
        return out

    def details(self, manga_id):
        page = self._get(manga_id, referer=self.base + "/")
        title = self._meta(page, "og:title") or ""
        if not title or title.lower().startswith("read manga") or title.lower().startswith("manga"):
            h = re.search(r'class="detail-info-right-title-font"[^>]*>([^<]+)<', page) or \
                re.search(r'<h1[^>]*>([^<]+)</h1>', page)
            if h:
                title = h.group(1)
        if not title:
            tt = re.search(r"<title>(.*?)</title>", page, re.S)
            if tt:
                title = tt.group(1)
        title = _clean(re.sub(r"\s*(Manga\b.*|-\s*Read.*|\|.*)$", "", title, flags=re.I))
        cm = re.search(r'<img[^>]+class="detail-info-cover-img"[^>]+src="([^"]+)"', page) or \
             re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', page)
        cover = _abs(cm.group(1), self.base) if cm else ""
        dm = re.search(r'<p class="fullcontent">(.*?)</p>', page, re.S)
        desc = _clean(re.sub(r"<[^>]+>", "", dm.group(1))) if dm else ""
        if not desc:
            desc = self._meta(page, "og:description")
        alts = []
        gm = re.search(r'detail-info-right-title-tip2"[^>]*>(.*?)</p>', page, re.S)
        if gm:
            alts = [_clean(x) for x in re.split(r"[;,]", re.sub(r"<[^>]+>", " ", gm.group(1))) if _clean(x)]
        # Restringi alla lista capitoli reale (esclude "ultime uscite" della sidebar)
        sc = re.search(r'<ul class="detail-main-list[^"]*">(.*?)</ul>', page, re.S)
        scope = sc.group(1) if sc else page
        seen, chapters = set(), []
        for m in re.finditer(r'href="((?:https?://[^"]*?)?/manga/[^"]+?/(?:v\d+/)?c[\d.]+/1\.html)"[^>]*title="([^"]+)"',
                             scope):
            curl = _abs(m.group(1), self.base)
            if curl in seen:
                continue
            seen.add(curl)
            name = _clean(m.group(2))
            nm = re.search(r"/c([\d.]+)/", curl)
            chapters.append({"id": curl, "name": name, "number": nm.group(1) if nm else ""})
        # ordina per numero crescente (1, 2, 3, ...)
        def _num(c):
            try:
                return (0, float(c["number"]))
            except (ValueError, TypeError):
                return (1, 0.0)
        chapters.sort(key=_num)

        authors = re.findall(r'/search/author/[^"]+"[^>]*>([^<]+)</a>', page)
        author = ", ".join(dict.fromkeys(_clean(a) for a in authors)) if authors else ""
        genres = [_clean(g) for g in re.findall(r'class="tag-box"[^>]*>([^<]+)</a>', page)]
        if not genres:
            genres = [_clean(g) for g in re.findall(r'/search/genres/[^"]+"[^>]*>([^<]+)</a>', page)]
        sm = re.search(r'detail-info-right-title-tip">\s*([^<]+?)\s*<', page)
        status = _clean(sm.group(1)) if sm else ""
        return {"title": _clean(title), "cover": cover, "description": desc, "chapters": chapters,
                "author": author, "status": status, "year": "", "genres": genres[:12],
                "altTitles": alts[:6]}

    def latest(self, manga_id):
        page = self._get(manga_id, referer=self.base + "/")
        sc = re.search(r'<ul class="detail-main-list[^"]*">(.*?)</ul>', page, re.S)
        scope = sc.group(1) if sc else page
        m = re.search(r"/c([\d.]+)/1\.html", scope)
        return m.group(1) if m else ""

    def pages(self, chapter_id):
        page = self._get(chapter_id, referer=self.base + "/")
        cid = re.search(r"var\s+chapterid\s*=\s*(\d+)", page)
        if not cid:
            raise RuntimeError("chapterid non trovato (la pagina potrebbe richiedere un browser)")
        cid = cid.group(1)
        tp = re.search(r"var\s+imagecount\s*=\s*(\d+)", page) or \
             re.search(r"var\s+total_pages\s*=\s*(\d+)", page)
        total = int(tp.group(1)) if tp else 1
        km = re.search(r"var\s+(?:dm5_)?key\s*=\s*['\"]([^'\"]*)['\"]", page)
        key = km.group(1) if km else ""
        base_dir = chapter_id.rsplit("/", 1)[0] + "/"
        pages = []
        for i in range(1, total + 1):
            ashx = "%schapterfun.ashx?cid=%s&page=%d&key=%s" % (base_dir, cid, i, key)
            try:
                packed = http_get(ashx, referer=chapter_id, cookie=self.cookie)
                js = unpack_js(packed)
                pix = re.search(r'pix\s*=\s*"([^"]*)"', js)
                vals = re.search(r'pvalue\s*=\s*\[([^\]]*)\]', js)
                if not (pix and vals):
                    continue
                pixv = pix.group(1)
                first = re.findall(r'"([^"]+)"', vals.group(1))
                if first:
                    pages.append(_abs(pixv + first[0], self.base).split("?")[0] +
                                 ("?" + first[0].split("?", 1)[1] if "?" in first[0] else ""))
            except Exception:
                continue
        if not pages:
            raise RuntimeError("impossibile decodificare le pagine (formato cambiato)")
        return {"pages": pages, "referer": self.base + "/"}

    @staticmethod
    def _meta(page, prop):
        m = re.search(r'<meta[^>]+property="%s"[^>]+content="([^"]*)"' % re.escape(prop), page)
        return _clean(m.group(1)) if m else ""


class MangaFox(_FMcDN):
    id = "mangafox"
    name = "MangaFox"
    base = "https://fanfox.net"
    mirrors = ["https://fanfox.net", "https://mangafox.la"]
    note = "Ricerca/capitoli ok. Lettura pagine: sperimentale."


class MangaHere(_FMcDN):
    id = "mangahere"
    name = "MangaHere"
    base = "https://www.mangahere.cc"
    note = "Stesso motore di MangaFox. Lettura pagine: sperimentale."


# ============================================================
#  MangaDex (API ufficiale) - opzionale
# ============================================================
class MangaDex(Source):
    id = "mangadex"
    name = "MangaDex"
    base = "https://api.mangadex.org"
    note = "API ufficiale, legale. Multilingua."
    lang = "en"

    def search(self, query):
        url = (self.base + "/manga?title=" + urllib.parse.quote(query) +
               "&limit=24&includes[]=cover_art&order[relevance]=desc"
               "&contentRating[]=safe&contentRating[]=suggestive&contentRating[]=erotica")
        data = json.loads(http_get(url))
        out = []
        for m in data.get("data", []):
            a = m["attributes"]
            t = a["title"]
            title = t.get("en") or t.get("ja-ro") or (list(t.values())[0] if t else "?")
            cover = ""
            for r in m.get("relationships", []):
                if r["type"] == "cover_art" and r.get("attributes"):
                    cover = "https://uploads.mangadex.org/covers/%s/%s.256.jpg" % (
                        m["id"], r["attributes"]["fileName"])
            out.append({"id": m["id"], "title": title, "cover": cover, "status": a.get("status", "")})
        return out

    def details(self, manga_id):
        d = json.loads(http_get("%s/manga/%s?includes[]=cover_art&includes[]=author&includes[]=artist"
                                % (self.base, manga_id)))["data"]
        a = d["attributes"]
        t = a["title"]
        title = t.get("en") or (list(t.values())[0] if t else "?")
        desc = (a.get("description") or {}).get("en", "")
        cover = ""
        authors = []
        for r in d.get("relationships", []):
            if r["type"] == "cover_art" and r.get("attributes"):
                cover = "https://uploads.mangadex.org/covers/%s/%s.512.jpg" % (
                    manga_id, r["attributes"]["fileName"])
            if r["type"] in ("author", "artist") and r.get("attributes"):
                nm = r["attributes"].get("name")
                if nm and nm not in authors:
                    authors.append(nm)
        genres = [tg["attributes"]["name"].get("en", "") for tg in a.get("tags", [])
                  if tg.get("attributes", {}).get("group") in ("genre", "theme")]
        chapters, off = [], 0
        while True:
            feed = json.loads(http_get(
                "%s/manga/%s/feed?translatedLanguage[]=en&order[chapter]=asc&limit=100&offset=%d"
                "&contentRating[]=safe&contentRating[]=suggestive&contentRating[]=erotica"
                % (self.base, manga_id, off)))
            for c in feed.get("data", []):
                ca = c["attributes"]
                chapters.append({"id": c["id"], "name": "Chapter " + (ca.get("chapter") or "?"),
                                 "number": ca.get("chapter") or ""})
            off += 100
            if off >= feed.get("total", 0) or not feed.get("data"):
                break
        alts = []
        for at in a.get("altTitles", []):
            for v in at.values():
                if v and v not in alts:
                    alts.append(v)
        return {"title": title, "cover": cover, "description": desc, "chapters": chapters,
                "author": ", ".join(authors), "status": a.get("status", "") or "",
                "year": a.get("year") or "", "genres": [g for g in genres if g][:12],
                "altTitles": alts[:6]}

    def latest(self, manga_id):
        d = json.loads(http_get(
            "%s/manga/%s/feed?order[chapter]=desc&limit=1&translatedLanguage[]=en"
            "&contentRating[]=safe&contentRating[]=suggestive&contentRating[]=erotica"
            % (self.base, manga_id)))
        arr = d.get("data") or []
        return arr[0]["attributes"].get("chapter", "") if arr else ""

    def pages(self, chapter_id):
        at = json.loads(http_get("%s/at-home/server/%s" % (self.base, chapter_id)))
        b, h = at["baseUrl"], at["chapter"]["hash"]
        pages = ["%s/data/%s/%s" % (b, h, f) for f in at["chapter"]["data"]]
        return {"pages": pages, "referer": ""}


# ---- registry ----
_ALL = [WeebCentral(), MangaFox(), MangaHere(), MangaDex()]
REGISTRY = {s.id: s for s in _ALL}
DEFAULT_ORDER = [s.id for s in _ALL]


def list_meta():
    return [{"id": s.id, "name": s.name, "lang": s.lang, "note": s.note,
             "base": s.base} for s in _ALL]
