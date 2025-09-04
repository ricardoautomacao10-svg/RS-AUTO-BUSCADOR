# news_automation.py — coletor 24x7 com painel + filtros Ilhabela + títulos corrigidos
# Vars de ambiente úteis:
#   DEFAULT_KEYWORDS="Litoral Norte de São Paulo,Ilhabela"
#   DEFAULT_LIST_URLS="https://www.ilhabela.sp.gov.br/portal/noticias/3"
#   HOURS_MAX=12
#   REQUIRE_H1=true
#   REQUIRE_IMAGE=false
#   CRON_INTERVAL_MIN=15
#   DISABLE_BACKGROUND=0   (1 para desativar tarefa de fundo)
#   REWRITE_WITH_AI=0      (1 para reescrever título/parágrafos via OpenRouter se ai_rewriter.py existir)

import os, re, json, base64, hashlib, sqlite3, asyncio
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse, parse_qs, unquote, urljoin
from html import escape

import httpx, feedparser
from bs4 import BeautifulSoup
from fastapi import FastAPI, Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from readability import Document as ReadabilityDoc
from boilerpy3 import extractors as boiler_extractors

# ---------- IA opcional (fallback seguro)
try:
    from ai_rewriter import rewrite_with_openrouter
except Exception:
    async def rewrite_with_openrouter(title, paragraphs, *_args, **_kw):
        return title, paragraphs

DB_PATH = os.getenv("DB_PATH", "/data/news.db")

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name, str(default)).strip().lower()
    return v in ("1","true","yes","on")

DEFAULT_KEYWORDS = [s.strip() for s in os.getenv("DEFAULT_KEYWORDS", "Litoral Norte de São Paulo,Ilhabela").split(",") if s.strip()]
DEFAULT_LIST_URLS = [s.strip() for s in os.getenv("DEFAULT_LIST_URLS", "https://www.ilhabela.sp.gov.br/portal/noticias/3").split(",") if s.strip()]
HOURS_MAX = int(os.getenv("HOURS_MAX", "12"))
REQUIRE_H1 = _env_bool("REQUIRE_H1", True)
REQUIRE_IMAGE = _env_bool("REQUIRE_IMAGE", False)
CRON_INTERVAL_MIN = max(5, int(os.getenv("CRON_INTERVAL_MIN", "15")))
DISABLE_BACKGROUND = _env_bool("DISABLE_BACKGROUND", False)  # por padrão a tarefa roda

# ---------- slugify fallback
try:
    from slugify import slugify
except Exception:
    def slugify(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"\s+", "-", s)
        s = re.sub(r"[^a-z0-9\-]+", "", s)
        return s

# ---------- trafilatura opcional
try:
    import trafilatura
except Exception:
    trafilatura = None

# ---------- utils
def now_utc() -> datetime: return datetime.now(timezone.utc)
def iso(dt: datetime) -> str: return dt.astimezone(timezone.utc).isoformat()
def stable_id(url: str) -> str:
    h = hashlib.sha256(url.encode("utf-8")).digest()[:9]
    return base64.urlsafe_b64encode(h).decode("ascii").rstrip("=")
def hostname_from_url(url: str) -> str:
    try:
        return re.sub(r"^www\.", "", urlparse(url).netloc)
    except Exception:
        return "fonte"
def is_google_news(u: str) -> bool:
    try:
        return "news.google." in urlparse(u).netloc.lower()
    except Exception:
        return False
def absolutize_url(src: Optional[str], base: Optional[str]) -> Optional[str]:
    if not src: return None
    try:
        return urljoin(base or "", src)
    except Exception:
        return src

BAD_SNIPPETS = [
    "leia mais","leia também","saiba mais","veja também","veja mais",
    "continue lendo","continue a ler","clique aqui","acesse aqui","inscreva-se",
    "assine","newsletter","compartilhe","instagram","twitter","x.com",
    "facebook","publicidade","anúncio","voltar ao topo","cookies"
]
def clean_paragraph(p: str) -> Optional[str]:
    txt = re.sub(r"\s+", " ", p or "").strip()
    if not txt: return None
    low = txt.lower()
    if any(b in low for b in BAD_SNIPPETS): return None
    if len(txt) < 16: return None
    if re.match(r"^(?:leia|veja|saiba|assine|clique)\b", low): return None
    return txt

def extract_og_twitter_image(soup: BeautifulSoup) -> Optional[str]:
    prefs = [
        ("meta", {"property":"og:image"}),
        ("meta", {"property":"og:image:secure_url"}),
        ("meta", {"name":"twitter:image"}),
        ("meta", {"name":"twitter:image:src"}),
        ("link", {"rel":"image_src"}),
        ("meta", {"itemprop":"image"}),
    ]
    for tag, attrs in prefs:
        el = soup.find(tag, attrs=attrs)
        if not el: continue
        src = el.get("content") or el.get("href")
        if src: return src.strip()
    return None

def pick_content_root(soup: BeautifulSoup) -> BeautifulSoup:
    sel = [
        'article','[itemprop="articleBody"]','.article-body','.post-content','.entry-content',
        '.story-content','#article','.article__content','#content .post','.texto'
    ]
    for s in sel:
        el = soup.select_one(s)
        if el: return el
    return soup.body or soup

def extract_img_from_root(soup: BeautifulSoup) -> Optional[str]:
    root = pick_content_root(soup)
    for fig in root.find_all(["figure","picture"], limit=6):
        img = fig.find("img")
        if not img: continue
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src:
            srcset = img.get("srcset") or img.get("data-srcset") or ""
            if srcset: src = srcset.split(",")[0].strip().split(" ")[0].strip()
        if not src: continue
        if any(x in (src or "").lower() for x in [".svg","sprite","data:image"]): continue
        return src
    for img in root.find_all("img", limit=10):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src:
            srcset = img.get("srcset") or img.get("data-srcset") or ""
            if srcset: src = srcset.split(",")[0].strip().split(" ")[0].strip()
        if not src: continue
        if any(x in (src or "").lower() for x in [".svg","sprite","data:image","logo","icon"]): continue
        return src
    return None

async def fetch_html_ex(client: httpx.AsyncClient, url: str) -> Tuple[Optional[str], Dict[str, Any]]:
    info = {"request_url": url, "ok": False, "status": None, "ctype": "", "final_url": url, "error": None}
    try:
        r = await client.get(
            url, timeout=25.0, follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124 Safari/537.36 NewsAutomation/2.4",
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                "Referer": "https://news.google.com/",
            },
        )
        info["status"] = r.status_code
        info["ctype"] = r.headers.get("Content-Type","")
        info["final_url"] = str(r.url)
        if 200 <= r.status_code < 300 and (
            "text/html" in info["ctype"] or "application/xhtml" in info["ctype"] or info["ctype"].startswith("text/")
        ):
            info["ok"] = True
            return r.text, info
    except Exception as e:
        info["error"] = str(e)[:300]
    return None, info

def parse_schema_org(html: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all("script", type="application/ld+json"):
            try: data = json.loads(tag.string or "{}")
            except Exception: continue
            d = data[0] if isinstance(data, list) and data else data
            typ = (d.get("@type") or "").lower()
            if any(t in typ for t in ["article","newsarticle","blogposting"]):
                out["headline"] = d.get("headline") or d.get("name")
                img = d.get("image")
                if isinstance(img, list): img = img[0] if img else None
                if isinstance(img, dict): img = img.get("url")
                out["image"] = img
                body = d.get("articleBody")
                if body:
                    parts = [clean_paragraph(x) for x in re.split(r"\n{1,}", body)]
                    out["paragraphs"] = [p for p in parts if p]
                break
    except Exception:
        pass
    return out

def paragraphs_from_html(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    root = pick_content_root(soup)
    ps: List[str] = []
    for p in root.find_all("p"):
        if p.find_parent(["script","nav","aside","footer","header","noscript","figure","style"]): continue
        c = clean_paragraph(p.get_text(" ", strip=True))
        if c: ps.append(c)
    if len(ps) < 2:
        for ul in root.find_all(["ul","ol"]):
            txt = clean_paragraph(ul.get_text(" ", strip=True))
            if txt and len(txt) > 120: ps.append(txt)
            if len(ps) >= 8: break
    if len(ps) < 2:
        try:
            doc = ReadabilityDoc(html)
            csoup = BeautifulSoup(doc.summary(), "html.parser")
            for p in csoup.find_all("p"):
                c = clean_paragraph(p.get_text(" ", strip=True))
                if c: ps.append(c)
        except Exception:
            pass
    if len(ps) < 2 and trafilatura:
        try:
            extracted = trafilatura.extract(html, include_comments=False, include_tables=False,
                                           include_images=False, favor_recall=True)
            if extracted:
                parts = [clean_paragraph(t) for t in re.split(r"\n{1,}", extracted)]
                ps = [p for p in parts if p][:12]
        except Exception:
            pass
    if len(ps) < 2:
        try:
            extractor = boiler_extractors.ArticleExtractor()
            text = extractor.get_content(html)
            parts = [clean_paragraph(t) for t in re.split(r"\n{1,}", text)]
            ps = [p for p in parts if p][:12] or ps
        except Exception:
            pass
    if not ps:
        soup2 = BeautifulSoup(html, "html.parser")
        txt = soup2.get_text("\n", strip=True)
        chunks = [clean_paragraph(x) for x in re.split(r"\n{2,}", txt)]
        ps = [c for c in chunks if c][:8]
    out, seen = [], set()
    for p in ps:
        if p in seen: continue
        seen.add(p); out.append(p)
    return out[:14]

def title_from_html(html: str) -> Optional[str]:
    # ORDEM CORRIGIDA: schema -> og:title -> <title> -> <h1>
    sc = parse_schema_org(html).get("headline")
    if sc: return sc
    soup = BeautifulSoup(html, "html.parser")
    m = soup.find("meta", property="og:title")
    if m and m.get("content"): return m["content"].strip()
    t = soup.find("title")
    if t and t.get_text(strip=True): return t.get_text(" ", strip=True)
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True): return h1.get_text(" ", strip=True)
    return None

def image_from_html_best(html: str, base_for_abs: Optional[str]) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    img = extract_og_twitter_image(soup)
    if img: return absolutize_url(img, base_for_abs)
    img = extract_img_from_root(soup)
    if img: return absolutize_url(img, base_for_abs)
    for img_tag in soup.find_all("img", limit=10):
        src = img_tag.get("src") or img_tag.get("data-src") or img_tag.get("data-original")
        if not src:
            sset = img_tag.get("srcset") or img_tag.get("data-srcset") or ""
            if sset: src = sset.split(",")[0].strip().split(" ")[0].strip()
        if not src: continue
        if any(x in src.lower() for x in [".svg","sprite","data:image","logo","icon"]): continue
        return absolutize_url(src, base_for_abs)
    return None

def unwrap_special_links(url: str) -> str:
    try:
        u = urlparse(url); host = u.netloc.lower()
        if "l.facebook.com" in host and u.path.startswith("/l.php"):
            qs = parse_qs(u.query); target = qs.get("u", [None])[0]
            if target: return unquote(target)
        if "facebook.com" in host and "href=" in u.query:
            qs = parse_qs(u.query); target = qs.get("href", [None])[0]
            if target: return unquote(target)
    except Exception:
        pass
    return url

def extract_external_from_gnews(html: str, base_url: str) -> Optional[str]:
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        meta = soup.find("meta", attrs={"http-equiv": lambda v: v and v.lower()=="refresh"})
        if meta and meta.get("content"):
            m = re.search(r"url=(.+)", meta["content"], flags=re.I)
            if m: return urljoin(base_url, m.group(1).strip().strip('\'"'))
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a["href"])
            host = urlparse(href).netloc.lower()
            if "google" in host or "gstatic.com" in host: continue
            if href.startswith(("http://","https://")): return href
        for m in re.finditer(r'https?://[^\s\'"<>]+', html or ""):
            href = m.group(0); host = urlparse(href).netloc.lower()
            if "google" in host or "gstatic.com" in host: continue
            return href
    except Exception:
        return None
    return None

# ---------- DB
def db_init() -> None:
    global DB_PATH
    dirpath = os.path.dirname(DB_PATH)
    try:
        if dirpath: os.makedirs(dirpath, exist_ok=True)
    except PermissionError:
        DB_PATH = "./data/news.db"
        try: os.makedirs("./data", exist_ok=True)
        except Exception: DB_PATH = "./news.db"
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            url TEXT UNIQUE,
            title TEXT,
            image TEXT,
            paragraphs TEXT,
            source_name TEXT,
            published_at TEXT,
            keyword TEXT,
            created_at TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_items_keyword ON items(keyword)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_items_created ON items(created_at)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS rules (
            slug TEXT PRIMARY KEY,
            url_regex TEXT,
            list_selector TEXT,
            title_sel TEXT,
            image_sel TEXT,
            para_sel TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)
    con.close()

def db_upsert(item: Dict[str, Any]) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO items (id, url, title, image, paragraphs, source_name, published_at, keyword, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            title=excluded.title,
            image=excluded.image,
            paragraphs=excluded.paragraphs,
            source_name=excluded.source_name,
            published_at=excluded.published_at,
            keyword=excluded.keyword
    """, (
        item["id"], item["url"], item.get("title"), item.get("image"),
        json.dumps(item.get("paragraphs", []), ensure_ascii=False),
        item.get("source_name"), item.get("published_at"),
        item.get("keyword"), iso(now_utc())
    ))
    con.commit(); con.close()

def db_get(id_: str) -> Optional[Dict[str, Any]]:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("""SELECT id,url,title,image,paragraphs,source_name,published_at,keyword,created_at
                         FROM items WHERE id=?""", (id_,))
    r = cur.fetchone(); con.close()
    if not r: return None
    return {"id":r[0],"url":r[1],"title":r[2],"image":r[3],
            "paragraphs":json.loads(r[4] or "[]"),"source_name":r[5],
            "published_at":r[6],"keyword":r[7],"created_at":r[8]}

def db_list_by_keyword(slug: str, since_hours: int=12) -> List[Dict[str, Any]]:
    cutoff = iso(now_utc() - timedelta(hours=since_hours))
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("""
      SELECT id,url,title,image,source_name,published_at,created_at
      FROM items WHERE keyword=? AND created_at>=? ORDER BY created_at DESC
    """, (slug, cutoff))
    out = [{"id":r[0],"url":r[1],"title":r[2],"image":r[3],"source_name":r[4],
            "published_at":r[5],"created_at":r[6]} for r in cur.fetchall()]
    con.close(); return out

def db_rules_get(slug: str) -> Optional[Dict[str,str]]:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("SELECT slug,url_regex,list_selector,title_sel,image_sel,para_sel FROM rules WHERE slug=?", (slug,))
    r = cur.fetchone(); con.close()
    if not r: return None
    return {"slug":r[0], "url_regex":r[1], "list_selector":r[2], "title_sel":r[3], "image_sel":r[4], "para_sel":r[5]}

def db_rules_set(slug: str, url_regex: Optional[str], list_selector: Optional[str],
                 title_sel: Optional[str], image_sel: Optional[str], para_sel: Optional[str]) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO rules (slug,url_regex,list_selector,title_sel,image_sel,para_sel,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(slug) DO UPDATE SET
            url_regex=excluded.url_regex,
            list_selector=excluded.list_selector,
            title_sel=excluded.title_sel,
            image_sel=excluded.image_sel,
            para_sel=excluded.para_sel,
            updated_at=excluded.updated_at
    """, (slug, url_regex, list_selector, title_sel, image_sel, para_sel, iso(now_utc()), iso(now_utc())))
    con.commit(); con.close()

def db_rules_clear(slug: str) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM rules WHERE slug=?", (slug,))
    con.commit(); con.close()

# ---------- fontes (GNews + GDELT)
def google_news_rss(keyword: str, lang="pt-BR", region="BR") -> str:
    q = quote_plus(keyword)
    return f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={region}&ceid=BR:pt-419"

async def gdelt_links(client: httpx.AsyncClient, keyword: str, hours: int) -> List[str]:
    try:
        url = f"https://api.gdeltproject.org/api/v2/doc/doc?query={quote_plus(keyword)}&timespan={hours}h&format=json"
        r = await client.get(url, timeout=20.0, headers={"User-Agent":"NewsAutomation/2.4"})
        if r.status_code != 200: return []
        js = r.json()
        return [a["url"] for a in js.get("articles", []) if a.get("url")]
    except Exception:
        return []

# ---------- filtros específicos Ilhabela
def is_ilhabela_article(href: str) -> bool:
    return re.search(r"https?://(?:www\.)?ilhabela\.sp\.gov\.br/portal/noticias/0/3/\d+/", href, flags=re.I) is not None

# ---------- pipeline de artigo
async def process_article(
    client: httpx.AsyncClient,
    url: str, keyword: str, pub_dt: datetime,
    feed_title: Optional[str], feed_source_name: Optional[str],
    require_h1: bool, require_img: bool,
    want_debug: bool = False,
    selectors: Optional[Dict[str, Optional[str]]] = None,
):
    dbg: Dict[str, Any] = {"link": url, "amp_used": False}
    url = unwrap_special_links(url)

    html, info = await fetch_html_ex(client, url)
    dbg["fetch"] = info
    title = None; image = None; paragraphs: List[str] = []
    final_url_used = None

    # Seguir link externo do Google News antes de extrair
    if info.get("final_url") and is_google_news(info["final_url"]):
        ext = extract_external_from_gnews(html or "", info["final_url"])
        dbg["gnews_external"] = ext
        if ext:
            html2, info2 = await fetch_html_ex(client, ext)
            if html2:
                html = html2
                final_url_used = info2.get("final_url") or ext
                dbg["gnews_follow_fetch"] = info2

    base_for_abs = final_url_used or info.get("final_url") or url

    # seletores custom (regras)
    if html and selectors:
        soup_c = BeautifulSoup(html, "html.parser")
        if selectors.get("title_sel"):
            el = soup_c.select_one(selectors["title_sel"])
            if el: title = el.get_text(" ", strip=True)
        if selectors.get("image_sel"):
            el = soup_c.select_one(selectors["image_sel"])
            if el: image = el.get("content") or el.get("src") or el.get("data-src")
        if selectors.get("para_sel"):
            ps = []
            for el in soup_c.select(selectors["para_sel"]):
                t = clean_paragraph(el.get_text(" ", strip=True))
                if t: ps.append(t)
            if ps: paragraphs = ps[:14]

    # pipeline normal
    if html and not paragraphs:
        title = title or title_from_html(html) or (feed_title or "")
        image = image or image_from_html_best(html, base_for_abs)
        sc = parse_schema_org(html)
        if sc.get("paragraphs"): paragraphs = sc["paragraphs"]
        if not paragraphs: paragraphs = paragraphs_from_html(html)

    # fallback AMP se necessário
    if (not paragraphs) and (html or ""):
        soup = BeautifulSoup(html, "html.parser")
        amp = soup.find("link", rel=lambda v: v and "amphtml" in v)
        if amp and amp.get("href"):
            amp_url = urljoin(base_for_abs, amp["href"])
            amp_html, amp_info = await fetch_html_ex(client, amp_url)
            final_url_used = final_url_used or (amp_info.get("final_url") or amp_url)
            base_for_abs = final_url_used or base_for_abs
            dbg["amp_used"] = True; dbg["amp_fetch"] = amp_info
            if amp_html:
                if selectors:
                    soup3 = BeautifulSoup(amp_html, "html.parser")
                    if selectors.get("title_sel"):
                        el = soup3.select_one(selectors["title_sel"])
                        if el and not title: title = el.get_text(" ", strip=True)
                    if selectors.get("image_sel"):
                        el = soup3.select_one(selectors["image_sel"])
                        if el and not image:
                            image = el.get("content") or el.get("src") or el.get("data-src")
                if not title: title = title_from_html(amp_html) or title
                if not image: image = image_from_html_best(amp_html, base_for_abs)
                if not paragraphs: paragraphs = paragraphs_from_html(amp_html)

    image = absolutize_url(image, base_for_abs)

    if require_h1 and not (title and title.strip()):
        return (None, "no_h1", {"decision":"no_h1", **dbg}) if want_debug else (None, "no_h1")
    if require_img and not image:
        return (None, "no_image", {"decision":"no_image", **dbg}) if want_debug else (None, "no_image")
    if not paragraphs:
        return (None, "no_paragraphs", {"decision":"no_paragraphs", **dbg}) if want_debug else (None, "no_paragraphs")

    # IA opcional
    use_ai = _env_bool("REWRITE_WITH_AI", False)
    try:
        if use_ai:
            final_for_name = base_for_abs
            src_name = hostname_from_url(final_for_name)
            title, paragraphs = await rewrite_with_openrouter(title, paragraphs, src_name, final_for_name)
    except Exception:
        pass

    source_name = feed_source_name or hostname_from_url(base_for_abs)
    final_url = base_for_abs
    item = {
        "id": stable_id(final_url),
        "url": final_url,
        "title": (title or "")[:220],
        "image": image,
        "paragraphs": paragraphs,
        "source_name": source_name,
        "published_at": iso(pub_dt),
        "keyword": slugify(keyword)
    }
    return (item, None, {"decision":"ok", **dbg}) if want_debug else (item, None)

async def crawl_keyword(client: httpx.AsyncClient, keyword: str, hours_max: int,
                        require_h1: bool, require_img: bool, want_debug: bool=False):
    metrics = {"ok":0,"fetch_fail":0,"no_h1":0,"no_image":0,"no_paragraphs":0}
    details: List[Dict[str, Any]] = []
    links: List[str] = []

    # Google News
    try:
        r = await client.get(google_news_rss(keyword), timeout=20.0,
                             headers={"User-Agent":"NewsAutomation/2.4"})
        if r.status_code == 200:
            feed = feedparser.parse(r.text)
            for e in feed.entries[:80]:
                link = e.get("link")
                if link: links.append(link)
    except Exception:
        pass
    # GDELT
    try:
        links += await gdelt_links(client, keyword, hours_max)
    except Exception:
        pass

    seen = set(); norm = []
    for l in links:
        if not l or l in seen: continue
        # filtro Ilhabela se for domínio
        if "ilhabela.sp.gov.br" in l.lower():
            if not is_ilhabela_article(l):  # ignora listagens /3/
                continue
        seen.add(l); norm.append(l)

    ts = now_utc()
    tasks = []
    for link in norm[:150]:
        tasks.append(asyncio.create_task(
            process_article(client, link, keyword, ts, None, None,
                            require_h1, require_img, want_debug=want_debug)
        ))

    out: List[Dict[str, Any]] = []
    results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
    for res in results:
        if want_debug:
            if isinstance(res, tuple) and len(res) == 3:
                item, reason, dbg = res
                if item: out.append(item); db_upsert(item); metrics["ok"] += 1
                else: metrics[reason or "fetch_fail"] = metrics.get(reason or "fetch_fail", 0)+1
                details.append(dbg)
            else:
                metrics["fetch_fail"] += 1
        else:
            if isinstance(res, tuple):
                item, reason = res
                if item: out.append(item); db_upsert(item); metrics["ok"] += 1
                else: metrics[reason or "fetch_fail"] = metrics.get(reason or "fetch_fail", 0)+1
            else:
                metrics["fetch_fail"] += 1
    return (out, metrics, details) if want_debug else (out, metrics)

DEFAULT_ARTICLE_REGEX = re.compile(r"/(noticia|noticias|materia|/20\d{2}/)\b", re.I)
def looks_like_article_url(href: str, url_regex: Optional[str]) -> bool:
    if "ilhabela.sp.gov.br" in href.lower():
        return is_ilhabela_article(href)
    if url_regex:
        try: return re.search(url_regex, href, flags=re.I) is not None
        except Exception: pass
    return DEFAULT_ARTICLE_REGEX.search(href) is not None

def extract_links_from_listing(html: str, base_url: str, selector: Optional[str], url_regex: Optional[str]) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser"); links=[]
    if selector:
        for el in soup.select(selector):
            a = el if el.name == "a" else el.find("a", href=True)
            if not a or not a.get("href"): continue
            href = urljoin(base_url, a["href"])
            if href.startswith(("http://","https://")) and looks_like_article_url(href, url_regex):
                links.append(href)
    if not selector or not links:
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a["href"])
            if not href.startswith(("http://","https://")): continue
            if looks_like_article_url(href, url_regex): links.append(href)
    out, seen = [], set()
    for l in links:
        l = l.split("#")[0]
        if l in seen: continue
        seen.add(l); out.append(l)
    return out[:150]

async def crawl_listing_once(client: httpx.AsyncClient, list_url: str, keyword: str,
                             selector: Optional[str], url_regex: Optional[str],
                             require_h1: bool, require_img: bool, want_debug: bool,
                             selectors_article: Optional[Dict[str, Optional[str]]] = None):
    html, info = await fetch_html_ex(client, list_url)
    if not html:
        return ([], {"fetch_fail":1}, [{"link":list_url,"fetch":info,"decision":"fetch_fail"}]) if want_debug else ([], {"fetch_fail":1})
    article_links = extract_links_from_listing(html, info.get("final_url") or list_url, selector, url_regex)
    tasks = []
    for u in article_links:
        tasks.append(asyncio.create_task(
            process_article(client, u, keyword, now_utc(), None, None,
                            require_h1, require_img, want_debug=want_debug, selectors=selectors_article)
        ))
    metrics = {"ok":0,"fetch_fail":0,"no_h1":0,"no_image":0,"no_paragraphs":0}
    details: List[Dict[str, Any]] = []; out=[]
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if want_debug:
                if isinstance(res, tuple) and len(res)==3:
                    item, reason, dbg = res
                    if item: out.append(item); db_upsert(item); metrics["ok"]+=1
                    else: metrics[reason or "fetch_fail"]=metrics.get(reason or "fetch_fail",0)+1
                    details.append(dbg)
                else:
                    metrics["fetch_fail"]+=1
            else:
                if isinstance(res, tuple):
                    item, reason = res
                    if item: out.append(item); db_upsert(item); metrics["ok"]+=1
                    else: metrics[reason or "fetch_fail"]=metrics.get(reason or "fetch_fail",0)+1
                else:
                    metrics["fetch_fail"]+=1
    if want_debug:
        details.insert(0, {"link": list_url, "final_url": info.get("final_url"), "decision":"listing_fetched", "articles_found": len(article_links)})
        return out, metrics, details
    return out, metrics

# ---------- app & rotas
LAST_BG_RUN: Optional[str] = None
LAST_BG_SUMMARY: Dict[str, Any] = {}

def create_app() -> FastAPI:
    db_init()
    app = FastAPI(title="News Automation")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    app.mount("/static", StaticFiles(directory="static", html=True, check_dir=False), name="static")

    @app.head("/")
    def root_head(): return Response(status_code=200)

    # se houver UI em /static/index.html, redireciona pra ela; senão mostra link
    @app.get("/", response_class=HTMLResponse)
    def root_get():
        if os.path.exists("static/index.html"):
            return RedirectResponse(url="/static/index.html")
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'/>"
            "<style>body{font-family:system-ui,Segoe UI,Roboto,Arial;max-width:760px;margin:40px auto;padding:0 16px}</style>"
            "<h1>News Automation — online</h1>"
            "<p>Crie <code>static/index.html</code> para o painel. Enquanto isso, use:</p>"
            "<ul>"
            "<li><a href='/rss/litoral-norte-de-sao-paulo'>/rss/litoral-norte-de-sao-paulo</a></li>"
            "<li><a href='/healthz'>/healthz</a></li>"
            "</ul>"
        )

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "time": iso(now_utc()), "db": DB_PATH,
                "last_bg_run": LAST_BG_RUN, "last_bg_summary": LAST_BG_SUMMARY}

    # ---- Regras
    @app.get("/rules/get")
    def rules_get(slug: str = Query(...)):
        return db_rules_get(slugify(slug)) or {}
    @app.post("/rules/set")
    def rules_set(slug: str = Body(...), url_regex: Optional[str] = Body(default=None),
                  list_selector: Optional[str] = Body(default=None),
                  title_sel: Optional[str] = Body(default=None),
                  image_sel: Optional[str] = Body(default=None),
                  para_sel: Optional[str] = Body(default=None)):
        s = slugify(slug); db_rules_set(s, url_regex, list_selector, title_sel, image_sel, para_sel)
        return {"ok": True, "slug": s}
    @app.delete("/rules/clear")
    def rules_clear(slug: str = Query(...)):
        db_rules_clear(slugify(slug)); return {"ok": True}

    # ---- Palavra-chave
    @app.post("/crawl")
    async def crawl_post(keywords: List[str] = Body(default=["brasil"]),
                         hours_max: int = Body(default=12),
                         require_h1: bool = Body(default=True),
                         require_image: bool = Body(default=False),
                         debug: bool = Body(default=False)):
        async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
            res: Dict[str, Any] = {}; stats: Dict[str, Any] = {}
            dets: Dict[str, Any] = {}
            for kw in keywords:
                items, m, d = await crawl_keyword(client, kw, hours_max, require_h1, require_image, want_debug=debug)
                by_id = {it["id"]: it for it in items}
                clean_items = list(by_id.values())
                res[slugify(kw)] = [{"id":it["id"],"title":it["title"],"source":it["source_name"]} for it in clean_items]
                stats[slugify(kw)] = m
                if debug: dets[slugify(kw)] = d
            out = {"collected": res, "stats": stats}
            if debug: out["details"] = dets
            return out

    @app.get("/crawl")
    async def crawl_get(keywords: str = Query("brasil"),
                        hours_max: int = Query(12, ge=1, le=72),
                        require_h1: bool = Query(True),
                        require_image: bool = Query(False),
                        debug: bool = Query(False)):
        kws = [k.strip() for k in keywords.split(",") if k.strip()]
        return await crawl_post(kws, hours_max, require_h1, require_image, debug)

    # ---- Listagem
    @app.post("/crawl_site")
    async def crawl_site_post(url: str = Body(..., embed=True),
                              keyword: str = Body("geral", embed=True),
                              selector: Optional[str] = Body(default=None),
                              url_regex: Optional[str] = Body(default=None),
                              require_h1: bool = Body(default=True),
                              require_image: bool = Body(default=False),
                              debug: bool = Body(default=False)):
        slug = slugify(keyword)
        rule = db_rules_get(slug) or {}
        selector = selector or rule.get("list_selector")
        url_regex = url_regex or rule.get("url_regex")
        sels = {"title_sel": rule.get("title_sel"), "image_sel": rule.get("image_sel"), "para_sel": rule.get("para_sel")}
        async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
            items, m, d = await crawl_listing_once(client, url, keyword, selector, url_regex,
                                                   require_h1, require_image, want_debug=debug,
                                                   selectors_article=sels)
            out = {"ok": True, "found": len(items), "stats": m, "keyword": slug}
            if debug: out["details"] = d
            return out

    @app.get("/crawl_site")
    async def crawl_site_get(url: str = Query(...),
                             keyword: str = Query("geral"),
                             selector: Optional[str] = Query(default=None),
                             url_regex: Optional[str] = Query(default=None),
                             require_h1: bool = Query(True),
                             require_image: bool = Query(False),
                             debug: bool = Query(False)):
        return await crawl_site_post(url, keyword, selector, url_regex, require_h1, require_image, debug)

    # ---- Teste de fetch
    @app.get("/debug_fetch")
    async def debug_fetch(url: str = Query(...)):
        async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
            html, info = await fetch_html_ex(client, url)
            return JSONResponse({"info": info, "snippet": (html or "")[:400]})

    # ---- Link direto
    @app.post("/add")
    async def add_link_post(url: str = Body(..., embed=True),
                            keyword: str = Body("geral", embed=True),
                            require_h1: bool = Body(default=True),
                            require_image: bool = Body(default=False)):
        slug = slugify(keyword)
        rule = db_rules_get(slug) or {}
        sels = {"title_sel": rule.get("title_sel"), "image_sel": rule.get("image_sel"), "para_sel": rule.get("para_sel")}
        async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
            r = await process_article(client, url, keyword, now_utc(), None, None,
                                      require_h1=require_h1, require_img=require_image,
                                      want_debug=False, selectors=sels)
            item, reason = r if isinstance(r, tuple) else (None, "fetch_fail")
            if not item:
                raise HTTPException(status_code=400, detail=f"Não foi possível extrair conteúdo ({reason}).")
            db_upsert(item)
            return {"id": item["id"], "title": item["title"], "permalink": f"/item/{item['id']}", "keyword": item["keyword"]}

    @app.get("/add")
    async def add_link_get(url: str = Query(...),
                           keyword: str = Query("geral"),
                           require_h1: bool = Query(True),
                           require_image: bool = Query(False)):
        return await add_link_post(url, keyword, require_h1, require_image)

    # ---- APIs & páginas
    @app.get("/api/list")
    def api_list(keyword: str = Query(...), hours: int = Query(12, ge=1, le=72)):
        hours = max(5, hours)
        return {"items": db_list_by_keyword(slugify(keyword), since_hours=hours)}

    @app.get("/api/json/{keyword_slug}")
    def api_json(keyword_slug: str, hours: int = Query(12, ge=1, le=72)):
        hours = max(5, hours)
        return {"items": db_list_by_keyword(keyword_slug, since_hours=hours)}

    @app.get("/rss/{keyword_slug}")
    def rss_feed(request: Request, keyword_slug: str, hours: int = Query(12, ge=1, le=72)):
        hours = max(5, hours)
        rows = db_list_by_keyword(keyword_slug, since_hours=hours)
        base = f"{request.url.scheme}://{request.headers.get('host','')}".rstrip("/")
        chan_title = f"News Automation — {keyword_slug}"
        chan_link = f"{base}/q/{keyword_slug}"
        chan_desc = f"Itens recentes para '{keyword_slug}' (últimas {hours}h)."
        parts = [
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
            "<rss version=\"2.0\"><channel>",
            f"<title>{escape(chan_title)}</title>",
            f"<link>{escape(chan_link)}</link>",
            f"<description>{escape(chan_desc)}</description>",
        ]
        for r in rows:
            link = f"{base}/item/{r['id']}"
            title = escape(r.get("title") or "(sem título)")
            guid = r["id"]
            pub = r.get("published_at") or r.get("created_at") or iso(now_utc())
            img = r.get("image") or ""
            desc_html = f'<![CDATA[{"<img src=\'%s\' /><br/>" % img if img else ""}<a href="{escape(r["url"])}">Matéria Original</a>]]>'
            parts += [
                "<item>",
                f"<title>{title}</title>",
                f"<link>{escape(link)}</link>",
                f"<guid isPermaLink='false'>{guid}</guid>",
                f"<pubDate>{pub}</pubDate>",
                f"<description>{desc_html}</description>",
                "</item>",
            ]
        parts.append("</channel></rss>")
        return Response(content="\n".join(parts), media_type="application/rss+xml; charset=utf-8")

    @app.get("/item/{id}", response_class=HTMLResponse)
    def view_item(id: str):
        it = db_get(id)
        if not it:
            return HTMLResponse("<h1>Não encontrado</h1>", status_code=404)
        parts = [
            "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'/>",
            "<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,Helvetica,Ubuntu;max-width:760px;margin:40px auto;padding:0 16px}",
            "h1{line-height:1.25;margin:0 0 12px}img{max-width:100%;height:auto;margin:16px 0;border-radius:6px}",
            "p{line-height:1.7;font-size:1.06rem;margin:14px 0}em a{color:#555;text-decoration:none}</style>",
            f"<h1>{(it['title'] or 'Sem título')}</h1>",
        ]
        if it.get("image"): parts.append(f"<img src='{it['image']}' alt='imagem'>")
        for p in it.get("paragraphs", []): parts.append(f"<p>{p}</p>")
        parts.append(f"<p><em>Fonte: <a href='{it['url']}' rel='nofollow noopener' target='_blank'>Matéria Original</a></em></p>")
        return HTMLResponse("".join(parts))

    @app.get("/q/{keyword_slug}", response_class=HTMLResponse)
    def view_keyword(keyword_slug: str, hours: int = 12):
        hours = max(5, hours)
        rows = db_list_by_keyword(keyword_slug, since_hours=hours)
        if not rows:
            return HTMLResponse("<h1>Nada encontrado</h1><p>Use /crawl, /crawl_site ou /add.</p>", status_code=404)
        parts = [
            "<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'/>",
            "<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,Helvetica,Ubuntu;max-width:860px;margin:40px auto;padding:0 16px}",
            "li{margin:10px 0}a{text-decoration:none}</style>",
            f"<h1>Resultados: {keyword_slug}</h1><ul>"
        ]
        for r in rows: parts.append(f"<li><a href='/item/{r['id']}'>{r['title']}</a> — {r['source_name']}</li>")
        parts.append("</ul>")
        return HTMLResponse("".join(parts))

    # ---------- tarefa de fundo 24x7
    @app.on_event("startup")
    async def _bg_start():
        if DISABLE_BACKGROUND:
            return
        async def _runner():
            global LAST_BG_RUN, LAST_BG_SUMMARY
            while True:
                try:
                    summary = {"time": iso(now_utc()), "keywords": {}, "lists": []}
                    async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
                        for kw in DEFAULT_KEYWORDS:
                            _, m = await crawl_keyword(client, kw, HOURS_MAX, REQUIRE_H1, REQUIRE_IMAGE, want_debug=False)
                            summary["keywords"][kw] = m
                        kslug = slugify(DEFAULT_KEYWORDS[0]) if DEFAULT_KEYWORDS else "geral"
                        for u in DEFAULT_LIST_URLS:
                            _, m = await crawl_listing_once(client, u, kslug,
                                                            selector=None, url_regex=None,
                                                            require_h1=REQUIRE_H1, require_img=REQUIRE_IMAGE,
                                                            want_debug=False, selectors_article=None)
                            summary["lists"].append({"url": u, "stats": m})
                    LAST_BG_RUN = iso(now_utc())
                    LAST_BG_SUMMARY = summary
                except Exception as e:
                    LAST_BG_RUN = iso(now_utc())
                    LAST_BG_SUMMARY = {"error": str(e)[:300]}
                await asyncio.sleep(CRON_INTERVAL_MIN * 60)
        asyncio.create_task(_runner())

    return app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT","8000")))
