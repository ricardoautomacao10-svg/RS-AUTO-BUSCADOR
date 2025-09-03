# news_automation.py
# Coletor de notícias com:
# - Palavra-chave (Google News + GDELT) com "desenrolar" do link do GNews
# - Raspar página de LISTAGEM (/crawl_site) usando regex/seletores salvos por slug
# - Extração robusta (Schema.org, Readability, Trafilatura, Boilerpipe) + fallback AMP
# - Regras persistentes por slug (regex + seletores CSS)
# - Modo DEBUG, RSS/JSON e páginas HTML de visualização

import os, re, json, base64, hashlib, sqlite3, asyncio
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse, parse_qs, unquote, urljoin
from pathlib import Path
from html import escape

import feedparser
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from readability import Document as ReadabilityDoc
from boilerpy3 import extractors as boiler_extractors

DB_PATH = os.getenv("DB_PATH", "/data/news.db")
APP_TITLE = "News Automation"

# slugify fallback
try:
    from slugify import slugify
except Exception:
    def slugify(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"\s+", "-", s)
        s = re.sub(r"[^a-z0-9\-]+", "", s)
        return s

# trafilatura opcional
try:
    import trafilatura  # type: ignore
except Exception:
    trafilatura = None


# ----------------------------- Utils -----------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

def stable_id(url: str) -> str:
    h = hashlib.sha256(url.encode("utf-8")).digest()[:9]
    return base64.urlsafe_b64encode(h).decode("ascii").rstrip("=")

def hostname_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc
        return re.sub(r"^www\.", "", host)
    except Exception:
        return "fonte"

def is_google_news(u: str) -> bool:
    try:
        return "news.google." in urlparse(u).netloc.lower()
    except Exception:
        return False

BAD_SNIPPETS = [
    "leia mais","leia também","saiba mais","veja também","veja mais","continue lendo","continue a ler",
    "clique aqui","acesse aqui","inscreva-se","assine","assinar","newsletter","compartilhe","siga-nos",
    "siga no instagram","siga no twitter","siga no x","siga no facebook","acompanhe nas redes",
    "publicidade","anúncio","publieditorial","conteúdo patrocinado","oferta","voltar ao topo","voltar para o início",
    "cookies","aceitar cookies",
]

def clean_paragraph(p: str) -> Optional[str]:
    txt = re.sub(r"\s+", " ", p or "").strip()
    if not txt: return None
    low = txt.lower()
    if any(b in low for b in BAD_SNIPPETS): return None
    if len(txt) < 16: return None
    if re.match(r"^(?:leia|veja|saiba|assine|clique)\b", low): return None
    return txt

def extract_og_image(soup: BeautifulSoup) -> Optional[str]:
    m = soup.find("meta", property="og:image")
    if m and m.get("content"): return m["content"].strip()
    m = soup.find("meta", attrs={"name": "twitter:image"})
    if m and m.get("content"): return m["content"].strip()
    for img in soup.find_all("img")[:20]:
        src = (img.get("src") or img.get("data-src") or img.get("data-original") or "").strip()
        if not src: continue
        ls = src.lower()
        if any(x in ls for x in [".svg","sprite","data:image"]): continue
        return src
    return None

async def fetch_html_ex(client: httpx.AsyncClient, url: str) -> Tuple[Optional[str], Dict[str, Any]]:
    info = {"request_url": url, "ok": False, "status": None, "ctype": "", "final_url": url, "error": None}
    try:
        r = await client.get(
            url, timeout=25.0, follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124 Safari/537.36 NewsAutomation/1.7",
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


# ----------------------------- Extração -----------------------------

def parse_schema_org(html: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "{}")
            except Exception:
                continue
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

def pick_content_root(soup: BeautifulSoup) -> BeautifulSoup:
    sel = [
        'article','[itemprop="articleBody"]','.article-body','.post-content','.entry-content','.story-content',
        '.content__article-body','.content-article','.materia-conteudo','#article','.article__content',
        '#content .post','.texto','section.article',
    ]
    for s in sel:
        el = soup.select_one(s)
        if el: return el
    return soup.body or soup

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
        txt = soup.get_text("\n", strip=True)
        chunks = [clean_paragraph(x) for x in re.split(r"\n{2,}", txt)]
        ps = [c for c in chunks if c][:8]
    # dedupe leve
    out, seen = [], set()
    for p in ps:
        if p in seen: continue
        seen.add(p); out.append(p)
    return out[:14]

def title_from_html(html: str) -> Optional[str]:
    sc = parse_schema_org(html).get("headline")
    if sc: return sc
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True): return h1.get_text(" ", strip=True)
    m = soup.find("meta", property="og:title")
    if m and m.get("content"): return m["content"].strip()
    t = soup.find("title")
    if t and t.get_text(strip=True): return t.get_text(" ", strip=True)
    return None

def image_from_html(html: str) -> Optional[str]:
    sc = parse_schema_org(html).get("image")
    if sc: return sc
    return extract_og_image(BeautifulSoup(html, "html.parser"))

def first_image_from_html(html: str) -> Optional[str]:
    return image_from_html(html)

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


# ----------------------------- DB -----------------------------

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
    # regras por slug
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

# regras
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


# ---------------------- Coleta por PALAVRA-CHAVE ----------------------

def google_news_rss(keyword: str, lang="pt-BR", region="BR") -> str:
    q = quote_plus(keyword)
    return f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={region}&ceid=BR:pt-419"

async def gdelt_links(client: httpx.AsyncClient, keyword: str, hours: int) -> List[str]:
    try:
        url = f"https://api.gdeltproject.org/api/v2/doc/doc?query={quote_plus(keyword)}&timespan={hours}h&format=json"
        r = await client.get(url, timeout=20.0, headers={"User-Agent":"NewsAutomation/1.7"})
        if r.status_code != 200: return []
        js = r.json()
        return [a["url"] for a in js.get("articles", []) if a.get("url")]
    except Exception:
        return []

async def process_article(
    client: httpx.AsyncClient,
    url: str, keyword: str, pub_dt: datetime,
    feed_title: Optional[str], feed_source_name: Optional[str],
    require_h1: bool, require_img: bool,
    want_debug: bool = False,
    selectors: Optional[Dict[str, Optional[str]]] = None,
):
    # (definição continua mais abaixo, reaproveitada em listagem)
    ...

# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
# F U N Ç Ã O   Q U E   F A L T A V A
# <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

async def crawl_keyword(
    client: httpx.AsyncClient, keyword: str, hours_max: int,
    require_h1: bool, require_img: bool, want_debug: bool = False
):
    """Coleta por palavra-chave (Google News + GDELT) e processa cada link."""
    metrics = {"ok":0,"fetch_fail":0,"no_h1":0,"no_image":0,"no_paragraphs":0}
    details: List[Dict[str, Any]] = []
    links: List[str] = []

    # Google News
    try:
        r = await client.get(google_news_rss(keyword), timeout=20.0,
                             headers={"User-Agent":"NewsAutomation/1.7"})
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

    # dedup
    seen = set(); norm_links: List[str] = []
    for l in links:
        if not l: continue
        if l in seen: continue
        seen.add(l); norm_links.append(l)

    now = now_utc()
    tasks: List[asyncio.Task] = []
    for link in norm_links[:120]:
        if want_debug:
            tasks.append(asyncio.create_task(
                process_article(client, link, keyword, now, None, None, require_h1, require_img, want_debug=True)
            ))
        else:
            tasks.append(asyncio.create_task(
                process_article(client, link, keyword, now, None, None, require_h1, require_img, want_debug=False)
            ))

    out: List[Dict[str, Any]] = []
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if want_debug:
                if isinstance(res, tuple) and len(res) == 3:
                    item, reason, dbg = res
                    if item:
                        out.append(item); db_upsert(item); metrics["ok"] += 1
                    else:
                        metrics[reason or "fetch_fail"] = metrics.get(reason or "fetch_fail", 0) + 1
                        dbg["decision"] = reason or "fetch_fail"
                    details.append(dbg)
                else:
                    metrics["fetch_fail"] += 1
            else:
                if isinstance(res, tuple):
                    item, reason = res
                    if item:
                        out.append(item); db_upsert(item); metrics["ok"] += 1
                    else:
                        metrics[reason or "fetch_fail"] = metrics.get(reason or "fetch_fail", 0) + 1
                else:
                    metrics["fetch_fail"] += 1

    return (out, metrics, details) if want_debug else (out, metrics)

# ---------------------- Seletores customizados ----------------------

def extract_with_selectors(html: str, sels: Dict[str, Optional[str]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not any(sels.values()): return out
    soup = BeautifulSoup(html, "html.parser")

    tsel = sels.get("title_sel") or ""
    if tsel:
        el = soup.select_one(tsel)
        if el: out["title"] = el.get_text(" ", strip=True)

    isel = sels.get("image_sel") or ""
    if isel:
        el = soup.select_one(isel)
        if el:
            src = el.get("content") or el.get("src") or el.get("data-src")
            if src: out["image"] = src

    psel = sels.get("para_sel") or ""
    if psel:
        ps: List[str] = []
        for el in soup.select(psel):
            txt = clean_paragraph(el.get_text(" ", strip=True))
            if txt: ps.append(txt)
        if ps: out["paragraphs"] = ps[:14]

    return out


# ----------------------------- Processamento de artigo (continuação) -----------------------------

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

    # 0) Seletor customizado (se existir)
    if html and selectors:
        custom = extract_with_selectors(html, selectors)
        title = custom.get("title")
        image = custom.get("image")
        paragraphs = custom.get("paragraphs") or []

    # 1) Pipeline normal
    if html and not paragraphs:
        title = title or title_from_html(html) or (feed_title or "")
        image = image or first_image_from_html(html)
        sc = parse_schema_org(html)
        if sc.get("paragraphs"): paragraphs = sc["paragraphs"]
        if not paragraphs: paragraphs = paragraphs_from_html(html)

    # 2) Se for Google News, seguir para o link externo real
    if (not paragraphs) and info.get("final_url") and is_google_news(info["final_url"]):
        ext = extract_external_from_gnews(html or "", info["final_url"])
        dbg["gnews_external"] = ext
        if ext:
            html2, info2 = await fetch_html_ex(client, ext)
            dbg["gnews_follow_fetch"] = info2
            if html2:
                if selectors and not paragraphs:
                    custom2 = extract_with_selectors(html2, selectors)
                    title = custom2.get("title") or title
                    image = custom2.get("image") or image
                    paragraphs = custom2.get("paragraphs") or paragraphs
                if not title: title = title_from_html(html2) or title
                if not image: image = first_image_from_html(html2) or image
                sc2 = parse_schema_org(html2)
                if sc2.get("paragraphs"): paragraphs = sc2["paragraphs"]
                if not paragraphs: paragraphs = paragraphs_from_html(html2)

    # 3) AMP fallback
    if (not paragraphs) and (html or ""):
        soup = BeautifulSoup(html, "html.parser")
        amp = soup.find("link", rel=lambda v: v and "amphtml" in v)
        if amp and amp.get("href"):
            amp_url = urljoin(info.get("final_url") or url, amp["href"])
            amp_html, amp_info = await fetch_html_ex(client, amp_url)
            dbg["amp_used"] = True; dbg["amp_fetch"] = amp_info
            if amp_html:
                if selectors:
                    custom3 = extract_with_selectors(amp_html, selectors)
                    title = custom3.get("title") or title
                    image = custom3.get("image") or image
                    paragraphs = custom3.get("paragraphs") or paragraphs
                if not title: title = title_from_html(amp_html) or title
                if not image: image = first_image_from_html(amp_html) or image
                if not paragraphs: paragraphs = paragraphs_from_html(amp_html)

    dbg["title_found"] = bool(title and title.strip())
    dbg["image_found"] = bool(image)
    dbg["p_count"] = len(paragraphs)

    if require_h1 and not dbg["title_found"]:
        return (None, "no_h1", dbg) if want_debug else (None, "no_h1")
    if require_img and not dbg["image_found"]:
        return (None, "no_image", dbg) if want_debug else (None, "no_image")
    if not paragraphs:
        return (None, "no_paragraphs", dbg) if want_debug else (None, "no_paragraphs")

    source_name = feed_source_name or hostname_from_url(info.get("final_url") or url)
    final_url = info.get("final_url") or url
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
    if want_debug:
        dbg["decision"] = "ok"
        return item, None, dbg
    return item, None


# ---------------------- Coleta por LISTAGEM ----------------------

DEFAULT_ARTICLE_REGEX = re.compile(
    r"/(noticia|notícias|noticias|materia|post|/20\d{2}/|/portal/noticias/)\b", re.I
)

def looks_like_article_url(href: str, url_regex: Optional[str]) -> bool:
    if url_regex:
        try:
            return re.search(url_regex, href, flags=re.I) is not None
        except Exception:
            pass
    return DEFAULT_ARTICLE_REGEX.search(href) is not None

def extract_links_from_listing(html: str, base_url: str,
                               selector: Optional[str],
                               url_regex: Optional[str]) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    links: List[str] = []

    # 1) seletor CSS
    if selector:
        for el in soup.select(selector):
            a = el if el.name == "a" else el.find("a", href=True)
            if not a or not a.get("href"): continue
            href = urljoin(base_url, a["href"])
            if href.startswith(("http://","https://")) and looks_like_article_url(href, url_regex):
                links.append(href)

    # 2) heurística geral
    if not selector or not links:
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a["href"])
            if not href.startswith(("http://","https://")): continue
            if looks_like_article_url(href, url_regex):
                links.append(href)

    # limpar duplicatas/âncora
    out, seen = [], set()
    for l in links:
        l = l.split("#")[0]
        if l in seen: continue
        seen.add(l); out.append(l)
    return out[:150]

async def crawl_listing_once(
    client: httpx.AsyncClient, list_url: str, keyword: str,
    selector: Optional[str], url_regex: Optional[str],
    require_h1: bool, require_img: bool, want_debug: bool,
    selectors_article: Optional[Dict[str, Optional[str]]] = None,
):
    html, info = await fetch_html_ex(client, list_url)
    if not html:
        return ([], {"fetch_fail":1}, [{"link":list_url,"fetch":info,"decision":"fetch_fail"}]) if want_debug else ([], {"fetch_fail":1})

    article_links = extract_links_from_listing(html, info.get("final_url") or list_url, selector, url_regex)
    tasks: List[asyncio.Task] = []
    now = now_utc()
    for url in article_links:
        if want_debug:
            tasks.append(asyncio.create_task(
                process_article(client, url, keyword, now, None, None, require_h1, require_img, want_debug=True, selectors=selectors_article)
            ))
        else:
            tasks.append(asyncio.create_task(
                process_article(client, url, keyword, now, None, None, require_h1, require_img, want_debug=False, selectors=selectors_article)
            ))

    metrics = {"ok":0,"fetch_fail":0,"no_h1":0,"no_image":0,"no_paragraphs":0}
    details: List[Dict[str, Any]] = []
    out: List[Dict[str, Any]] = []
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if want_debug:
                if isinstance(res, tuple) and len(res) == 3:
                    item, reason, dbg = res
                    if item:
                        out.append(item); db_upsert(item); metrics["ok"] += 1
                    else:
                        metrics[reason or "fetch_fail"] = metrics.get(reason or "fetch_fail", 0) + 1
                        dbg["decision"] = reason or "fetch_fail"
                    details.append(dbg)
                else:
                    metrics["fetch_fail"] += 1
            else:
                if isinstance(res, tuple):
                    item, reason = res
                    if item:
                        out.append(item); db_upsert(item); metrics["ok"] += 1
                    else:
                        metrics[reason or "fetch_fail"] = metrics.get(reason or "fetch_fail", 0) + 1
                else:
                    metrics["fetch_fail"] += 1

    if want_debug:
        details.insert(0, {"link": list_url, "final_url": info.get("final_url"), "decision":"listing_fetched", "articles_found": len(article_links)})
        return out, metrics, details
    return out, metrics


# ----------------------- App & Rotas ----------------------------

def create_app() -> FastAPI:
    db_init()
    app = FastAPI(title=APP_TITLE)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    app.mount("/static", StaticFiles(directory="static", html=True, check_dir=False), name="static")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "time": iso(now_utc()), "db": DB_PATH}

    # evita 405 no HEAD /
    @app.head("/")
    def root_head():
        return PlainTextResponse("", status_code=200)

    # --------- Regras por slug ----------
    @app.get("/rules/get")
    def rules_get(slug: str = Query(...)):
        r = db_rules_get(slugify(slug))
        return r or {}

    @app.post("/rules/set")
    def rules_set(
        slug: str = Body(...),
        url_regex: Optional[str] = Body(default=None),
        list_selector: Optional[str] = Body(default=None),
        title_sel: Optional[str] = Body(default=None),
        image_sel: Optional[str] = Body(default=None),
        para_sel: Optional[str] = Body(default=None),
    ):
        s = slugify(slug)
        db_rules_set(s, url_regex, list_selector, title_sel, image_sel, para_sel)
        return {"ok": True, "slug": s}

    @app.delete("/rules/clear")
    def rules_clear(slug: str = Query(...)):
        db_rules_clear(slugify(slug))
        return {"ok": True}

    # ----- PALAVRA-CHAVE -----
    @app.post("/crawl")
    async def crawl(
        keywords: List[str] = Body(default=["brasil"]),
        hours_max: int = Body(default=12),
        require_h1: bool = Body(default=True),
        require_image: bool = Body(default=False),
    ):
        async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
            res: Dict[str, Any] = {}; stats: Dict[str, Any] = {}
            for kw in keywords:
                items, m = await crawl_keyword(client, kw, hours_max, require_h1, require_image, want_debug=False)
                res[slugify(kw)] = [{"id":it["id"],"title":it["title"],"source":it["source_name"]} for it in items]
                stats[slugify(kw)] = m
            return {"collected": res, "stats": stats}

    @app.post("/crawl_debug")
    async def crawl_debug(
        keywords: List[str] = Body(default=["brasil"]),
        hours_max: int = Body(default=12),
        require_h1: bool = Body(default=True),
        require_image: bool = Body(default=False),
    ):
        async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
            all_details: Dict[str, Any] = {}
            res: Dict[str, Any] = {}; stats: Dict[str, Any] = {}
            for kw in keywords:
                items, m, det = await crawl_keyword(client, kw, hours_max, require_h1, require_image, want_debug=True)
                slug = slugify(kw)
                res[slug] = [{"id":it["id"],"title":it["title"],"source":it["source_name"]} for it in items]
                stats[slug] = m
                all_details[slug] = det
            return {"collected": res, "stats": stats, "details": all_details}

    # ----- LISTAGEM DE SITE -----
    @app.post("/crawl_site")
    async def crawl_site(
        url: str = Body(..., embed=True),
        keyword: str = Body("geral", embed=True),
        selector: Optional[str] = Body(default=None),
        url_regex: Optional[str] = Body(default=None),
        require_h1: bool = Body(default=True),
        require_image: bool = Body(default=False),
    ):
        slug = slugify(keyword)
        rule = db_rules_get(slug) or {}
        selector = selector or rule.get("list_selector")
        url_regex = url_regex or rule.get("url_regex")
        sels_article = {
            "title_sel": rule.get("title_sel"),
            "image_sel": rule.get("image_sel"),
            "para_sel":  rule.get("para_sel"),
        }
        async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
            items, m = await crawl_listing_once(client, url, keyword, selector, url_regex,
                                                require_h1, require_image, want_debug=False,
                                                selectors_article=sels_article)
            return {"ok": True, "found": len(items), "stats": m, "keyword": slug}

    @app.post("/crawl_site_debug")
    async def crawl_site_debug(
        url: str = Body(..., embed=True),
        keyword: str = Body("geral", embed=True),
        selector: Optional[str] = Body(default=None),
        url_regex: Optional[str] = Body(default=None),
        require_h1: bool = Body(default=True),
        require_image: bool = Body(default=False),
    ):
        slug = slugify(keyword)
        rule = db_rules_get(slug) or {}
        selector = selector or rule.get("list_selector")
        url_regex = url_regex or rule.get("url_regex")
        sels_article = {
            "title_sel": rule.get("title_sel"),
            "image_sel": rule.get("image_sel"),
            "para_sel":  rule.get("para_sel"),
        }
        async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
            items, m, det = await crawl_listing_once(client, url, keyword, selector, url_regex,
                                                     require_h1, require_image, want_debug=True,
                                                     selectors_article=sels_article)
            return {"ok": True, "found": len(items), "stats": m, "details": det, "keyword": slug}

    # utilitários
    @app.get("/debug_fetch")
    async def debug_fetch(url: str = Query(...)):
        async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
            html, info = await fetch_html_ex(client, url)
            sample = (html or "")[:400]
            return JSONResponse({"info": info, "snippet": sample})

    @app.post("/add")
    async def add_link(
        url: str = Body(..., embed=True),
        keyword: str = Body("geral", embed=True),
        require_h1: bool = Body(default=True),
        require_image: bool = Body(default=False),
    ):
        slug = slugify(keyword)
        rule = db_rules_get(slug) or {}
        sels_article = {
            "title_sel": rule.get("title_sel"),
            "image_sel": rule.get("image_sel"),
            "para_sel":  rule.get("para_sel"),
        }
        async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
            res = await process_article(client, url, keyword, now_utc(), None, None,
                                        require_h1=require_h1, require_img=require_image,
                                        want_debug=False, selectors=sels_article)
            if isinstance(res, tuple):
                item, reason = res
            else:
                item, reason = None, "fetch_fail"
            if not item:
                raise HTTPException(status_code=400, detail=f"Não foi possível extrair conteúdo ({reason}).")
            db_upsert(item)
            return {"id": item["id"], "title": item["title"],
                    "permalink": f"/item/{item['id']}", "keyword": item["keyword"]}

    @app.get("/api/list")
    def api_list(keyword: str = Query(...), hours: int = Query(12, ge=1, le=72)):
        return {"items": db_list_by_keyword(slugify(keyword), since_hours=hours)}

    @app.get("/api/json/{keyword_slug}")
    def api_json(keyword_slug: str, hours: int = Query(12, ge=1, le=72)):
       
