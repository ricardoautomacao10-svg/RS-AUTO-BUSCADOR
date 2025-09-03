# news_automation.py
# Coletor de notícias: Google News + GDELT, extração robusta (Schema.org, Readability, Trafilatura, fallback),
# AMP, debug detalhado, RSS/JSON, UI em /static.

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
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# extração
from readability import Document as ReadabilityDoc
from boilerpy3 import extractors as boiler_extractors

DB_PATH = os.getenv("DB_PATH", "/data/news.db")
APP_TITLE = "News Automation"

# slugify (fallback simples)
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

def from_pubdate_struct(tm: Any) -> Optional[datetime]:
    if not tm: return None
    try: return datetime(*tm[:6], tzinfo=timezone.utc)
    except Exception: return None

def stable_id(url: str) -> str:
    h = hashlib.sha256(url.encode("utf-8")).digest()[:9]
    return base64.urlsafe_b64encode(h).decode("ascii").rstrip("=")

def hostname_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc
        return re.sub(r"^www\.", "", host)
    except Exception:
        return "fonte"

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
    for img in soup.find_all("img")[:15]:
        src = (img.get("src") or "").strip()
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
                              "Chrome/124.0 Safari/537.36 NewsAutomation/1.3",
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
            def norm(d):
                if isinstance(d, list): return d[0] if d else {}
                return d
            d = norm(data)
            typ = (d.get("@type") or "").lower()
            if "article" in typ or "newsarticle" in typ or "blogposting" in typ:
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
        # tentar listas longas
        for ul in root.find_all(["ul","ol"]):
            txt = clean_paragraph(ul.get_text(" ", strip=True))
            if txt and len(txt) > 120: ps.append(txt)
            if len(ps) >= 8: break

    if len(ps) < 2:
        # Readability
        try:
            doc = ReadabilityDoc(html)
            content_html = doc.summary()
            csoup = BeautifulSoup(content_html, "html.parser")
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
        # Boilerpipe (artigos densos)
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
    return ps[:14]

def title_from_html(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    # Schema.org headline
    sc = parse_schema_org(html).get("headline")
    if sc: return sc
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


# ---------------------- Coleta (Google News + GDELT) ----------------------

def google_news_rss(keyword: str, lang="pt-BR", region="BR") -> str:
    q = quote_plus(keyword)  # sem when:12h; filtro por tempo é feito aqui
    return f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={region}&ceid=BR:pt-419"

async def gdelt_links(client: httpx.AsyncClient, keyword: str, hours: int) -> List[str]:
    try:
        url = f"https://api.gdeltproject.org/api/v2/doc/doc?query={quote_plus(keyword)}&timespan={hours}h&format=json"
        r = await client.get(url, timeout=20.0, headers={"User-Agent":"NewsAutomation/1.3"})
        if r.status_code != 200: return []
        js = r.json()
        arts = js.get("articles", [])
        return [a.get("url") for a in arts if a.get("url")]
    except Exception:
        return []

async def process_article(
    client: httpx.AsyncClient,
    url: str, keyword: str, pub_dt: datetime,
    feed_title: Optional[str], feed_source_name: Optional[str],
    require_h1: bool, require_img: bool,
    want_debug: bool = False,
):
    dbg: Dict[str, Any] = {"link": url, "amp_used": False}
    url = unwrap_special_links(url)

    html, info = await fetch_html_ex(client, url)
    dbg["fetch"] = info
    title = None; image = None; paragraphs: List[str] = []

    if html:
        title = title_from_html(html) or (feed_title or "")
        image = first_image_from_html(html)
        if not image:
            # tentar <meta> no HTML bruto antes de parse completo
            pass
        # schema.org body curto
        sc = parse_schema_org(html)
        if sc.get("paragraphs"):
            paragraphs = sc["paragraphs"]
            if not title and sc.get("headline"): title = sc["headline"]
            if not image and sc.get("image"): image = sc["image"]
        if not paragraphs:
            paragraphs = paragraphs_from_html(html)

    # AMP fallback
    if not paragraphs and html:
        soup = BeautifulSoup(html, "html.parser")
        amp = soup.find("link", rel=lambda v: v and "amphtml" in v)
        if amp and amp.get("href"):
            amp_url = urljoin(url, amp["href"])
            amp_html, amp_info = await fetch_html_ex(client, amp_url)
            dbg["amp_used"] = True; dbg["amp_fetch"] = amp_info
            if amp_html:
                if not title: title = title_from_html(amp_html) or title
                if not image: image = first_image_from_html(amp_html) or image
                paragraphs = paragraphs_from_html(amp_html)

    dbg["title_found"] = bool(title and title.strip())
    dbg["image_found"] = bool(image)
    dbg["p_count"] = len(paragraphs)

    if require_h1 and not dbg["title_found"]:
        if want_debug: return None, "no_h1", dbg
        return None, "no_h1"
    if require_img and not dbg["image_found"]:
        if want_debug: return None, "no_image", dbg
        return None, "no_image"
    if not paragraphs:
        if want_debug: return None, "no_paragraphs", dbg
        return None, "no_paragraphs"

    source_name = feed_source_name or hostname_from_url(url)
    item = {
        "id": stable_id(url), "url": url, "title": (title or "")[:220],
        "image": image, "paragraphs": paragraphs, "source_name": source_name,
        "published_at": iso(pub_dt), "keyword": slugify(keyword)
    }
    if want_debug:
        dbg["decision"] = "ok"
        return item, None, dbg
    return item, None

async def crawl_keyword(
    client: httpx.AsyncClient, keyword: str, hours_max: int,
    require_h1: bool, require_img: bool, want_debug: bool = False
):
    metrics = {"ok":0,"fetch_fail":0,"no_h1":0,"no_image":0,"no_paragraphs":0}
    details: List[Dict[str, Any]] = []
    links: List[str] = []

    # Google News
    try:
        r = await client.get(google_news_rss(keyword), timeout=20.0,
                             headers={"User-Agent":"NewsAutomation/1.3"})
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

    # normalizar + deduplicar
    seen = set()
    norm_links: List[str] = []
    for l in links:
        if not l: continue
        if l in seen: continue
        seen.add(l)
        norm_links.append(l)

    # limitar e processar
    now = now_utc()
    sem = min(120, len(norm_links))
    sem_links = norm_links[:sem]
    tasks: List[asyncio.Task] = []
    for link in sem_links:
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
        async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
            res = await process_article(client, url, keyword, now_utc(), None, None,
                                        require_h1=require_h1, require_img=require_image, want_debug=False)
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
        return {"items": db_list_by_keyword(keyword_slug, since_hours=hours)}

    @app.get("/rss/{keyword_slug}")
    def rss_feed(request: Request, keyword_slug: str, hours: int = Query(12, ge=1, le=72)):
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
        xml = "\n".join(parts)
        return Response(content=xml, media_type="application/rss+xml; charset=utf-8")

    @app.get("/item/{id}", response_class=HTMLResponse)
    def view_item(id: str):
        it = db_get(id)
        if not it:
            return HTMLResponse("<h1>Não encontrado</h1>", status_code=404)
        parts: List[str] = []
        parts.append(
            "<!doctype html><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'/>"
            "<style>"
            "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,Helvetica,Ubuntu;"
            "max-width:760px;margin:40px auto;padding:0 16px}"
            "h1{line-height:1.25;margin:0 0 12px}"
            "img{max-width:100%;height:auto;margin:16px 0;border-radius:6px}"
            "p{line-height:1.7;font-size:1.06rem;margin:14px 0}"
            "em a{color:#555;text-decoration:none}"
            "</style>"
        )
        parts.append(f"<h1>{(it['title'] or 'Sem título')}</h1>")
        if it.get("image"): parts.append(f"<img src='{it['image']}' alt='imagem'>")
        for p in it.get("paragraphs", []): parts.append(f"<p>{p}</p>")
        parts.append(f"<p><em>Fonte: <a href='{it['url']}' rel='nofollow noopener' target='_blank'>Matéria Original</a></em></p>")
        return HTMLResponse("".join(parts))

    @app.get("/q/{keyword_slug}", response_class=HTMLResponse)
    def view_keyword(keyword_slug: str, hours: int = 12):
        rows = db_list_by_keyword(keyword_slug, since_hours=hours)
        if not rows:
            return HTMLResponse("<h1>Nada encontrado</h1><p>Use POST /crawl ou /add.</p>", status_code=404)
        parts: List[str] = []
        parts.append(
            "<!doctype html><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'/>"
            "<style>"
            "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,Helvetica,Ubuntu;"
            "max-width:860px;margin:40px auto;padding:0 16px}"
            "li{margin:10px 0}a{text-decoration:none}"
            "</style>"
        )
        parts.append(f"<h1>Resultados: {keyword_slug}</h1><ul>")
        for r in rows: parts.append(f"<li><a href='/item/{r['id']}'>{r['title']}</a> — {r['source_name']}</li>")
        parts.append("</ul>")
        return HTMLResponse("".join(parts))

    @app.get("/", response_class=HTMLResponse)
    def root():
        idx = Path("static/index.html")
        if idx.exists():
            return HTMLResponse(idx.read_text(encoding="utf-8"))
        return HTMLResponse(
            "<p>UI não encontrada. Crie <code>static/index.html</code>. "
            "Endpoints: <code>/crawl</code>, <code>/crawl_debug</code>, <code>/debug_fetch</code>, "
            "<code>/add</code>, <code>/api/list</code>, <code>/api/json/{slug}</code>, "
            "<code>/rss/{slug}</code>, <code>/item/{id}</code>, <code>/q/{slug}</code>, <code>/healthz</code>.</p>"
        )

    return app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT","8000")))
