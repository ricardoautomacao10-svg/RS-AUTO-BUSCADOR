# news_automation.py
# Automação de notícias + UI + RSS/JSON:
# - Busca por palavras-chave (Google News RSS) nas últimas X horas (/crawl)
# - Ingestão de link direto (/add)
# - Limpeza (H1, IMG, <p>), com modo estrito opcional
# - Saídas: /api/list (JSON), /api/json/{slug} (JSON), /rss/{slug} (RSS 2.0)
# - Painel web em / (static/index.html)
#
# Requisitos (requirements.txt no fim)

import os
import re
import json
import base64
import hashlib
import sqlite3
import asyncio
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse, parse_qs, unquote
from pathlib import Path
from html import escape

import feedparser
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# ===== Caminho do banco (fallback automático) =====
DB_PATH = os.getenv("DB_PATH", "/data/news.db")

# ===== Regras padrão (podem ser flexibilizadas por requisição) =====
REQUIRE_H1_DEFAULT = True
REQUIRE_IMAGE_DEFAULT = True

# slugify opcional; fallback simples se lib não estiver instalada
try:
    from slugify import slugify
except Exception:  # pragma: no cover
    def slugify(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"\s+", "-", s)
        s = re.sub(r"[^a-z0-9\-]+", "", s)
        return s

# trafilatura opcional (melhora extração quando HTML é ruim)
try:
    import trafilatura  # type: ignore
except Exception:  # pragma: no cover
    trafilatura = None

APP_TITLE = "News Automation"

# ========================== Utilidades ==========================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

def from_pubdate_struct(tm: Any) -> Optional[datetime]:
    if not tm:
        return None
    try:
        return datetime(*tm[:6], tzinfo=timezone.utc)
    except Exception:
        return None

def stable_id(url: str) -> str:
    h = hashlib.sha256(url.encode("utf-8")).digest()[:9]
    return base64.urlsafe_b64encode(h).decode("ascii").rstrip("=")

def hostname_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc
        return re.sub(r"^www\.", "", host)
    except Exception:
        return "fonte"

# Lista de "lixo" reforçada
BAD_SNIPPETS = [
    # chamadas e CTAs
    "leia mais", "leia também", "saiba mais", "veja também", "veja mais",
    "continue lendo", "continue a ler", "clique aqui", "acesse aqui",
    "inscreva-se", "assine", "assinar", "newsletter",
    # redes/compartilhamento
    "compartilhe", "siga-nos", "siga no instagram", "siga no twitter", "siga no x",
    "siga no facebook", "acompanhe nas redes",
    # publicidade/comercial
    "publicidade", "anúncio", "publieditorial", "conteúdo patrocinado", "oferta",
    # navegação/site
    "voltar ao topo", "voltar para o início", "cookies", "aceitar cookies"
]

def clean_paragraph(p: str) -> Optional[str]:
    txt = re.sub(r"\s+", " ", p or "").strip()
    if not txt:
        return None
    low = txt.lower()
    if any(b in low for b in BAD_SNIPPETS):
        return None
    if len(txt) < 25:
        return None
    if re.search(r"https?://\S+", txt):
        return None
    if re.match(r"^(?:leia|veja|saiba|assine|clique)\b", low):
        return None
    return txt

def extract_og_image(soup: BeautifulSoup) -> Optional[str]:
    m = soup.find("meta", property="og:image")
    if m and m.get("content"):
        return m["content"].strip()
    m = soup.find("meta", attrs={"name": "twitter:image"})
    if m and m.get("content"):
        return m["content"].strip()
    for img in soup.find_all("img")[:10]:
        src = (img.get("src") or "").strip()
        if not src:
            continue
        ls = src.lower()
        if any(x in ls for x in [".svg", "sprite", "data:image"]):
            continue
        return src
    return None

async def fetch_html(client: httpx.AsyncClient, url: str) -> Optional[str]:
    try:
        r = await client.get(
            url, timeout=20.0, follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; NewsAutomation/1.0)",
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            },
        )
        ctype = r.headers.get("Content-Type", "")
        if 200 <= r.status_code < 300 and ("text/html" in ctype or "application/xhtml" in ctype):
            return r.text
    except Exception:
        return None
    return None

def title_from_html(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(" ", strip=True)
    t = soup.find("title")
    if t and t.get_text(strip=True):
        return t.get_text(" ", strip=True)
    return None

def paragraphs_from_html(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("article") or soup.body or soup
    ps: List[str] = []
    for p in root.find_all("p"):
        if p.find_parent(["script", "nav", "aside", "footer", "header", "noscript", "figure"]):
            continue
        c = clean_paragraph(p.get_text(" ", strip=True))
        if c:
            ps.append(c)
    if not ps and trafilatura:
        try:
            extracted = trafilatura.extract(
                html, include_comments=False, include_tables=False,
                include_images=False, favor_recall=True
            )
            if extracted:
                parts = [clean_paragraph(t) for t in re.split(r"\n{2,}", extracted)]
                ps = [p for p in parts if p]
        except Exception:
            pass
    return ps

def first_image_from_html(html: str) -> Optional[str]:
    return extract_og_image(BeautifulSoup(html, "html.parser"))

# ===== Expansão de links "wrapper" (Facebook / t.co) =====
def unwrap_special_links(url: str) -> str:
    """Retorna URL de destino quando o link é um wrapper (l.facebook.com, t.co).
       OBS: páginas comuns de Facebook/Instagram/X que exigem login não são suportadas."""
    try:
        u = urlparse(url)
        host = u.netloc.lower()
        # Facebook redirecionador: https://l.facebook.com/l.php?u=<url-escapada>&h=...
        if "l.facebook.com" in host and u.path.startswith("/l.php"):
            qs = parse_qs(u.query)
            target = qs.get("u", [None])[0]
            if target:
                return unquote(target)
        # Alguns links no facebook usam "facebook.com/plugins/post.php?href=<url>"
        if "facebook.com" in host and ("href=" in u.query):
            qs = parse_qs(u.query)
            target = qs.get("href", [None])[0]
            if target:
                return unquote(target)
        # t.co
        if host == "t.co":
            # não dá pra expandir sem requisição; deixamos httpx seguir redirects (já está ligado)
            return url
    except Exception:
        pass
    return url

# ========================== Banco de Dados ==========================

def db_init() -> None:
    global DB_PATH
    dirpath = os.path.dirname(DB_PATH)
    try:
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
    except PermissionError:
        DB_PATH = "./data/news.db"
        dirpath = os.path.dirname(DB_PATH)
        try:
            if dirpath:
                os.makedirs(dirpath, exist_ok=True)
        except Exception:
            DB_PATH = "./news.db"

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

# ========================== Coleta (RSS + extração) ==========================

def google_news_rss(keyword: str, lang="pt-BR", region="BR") -> str:
    q = quote_plus(f'{keyword} when:12h')
    return f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={region}&ceid=BR:pt-419"

async def process_article(
    client: httpx.AsyncClient,
    url: str,
    keyword: str,
    pub_dt: datetime,
    feed_title: Optional[str],
    feed_source_name: Optional[str],
    strict_h1: bool,
    strict_img: bool,
) -> Dict[str, Any]:
    # expande wrappers de Facebook/t.co quando possível
    url = unwrap_special_links(url)

    html = await fetch_html(client, url)
    if not html:
        return {}

    title = title_from_html(html) or (feed_title or "")
    image = first_image_from_html(html)
    paragraphs = paragraphs_from_html(html)

    if strict_h1 and (not title or not title.strip()):
        return {}
    if strict_img and (not image or not str(image).strip()):
        return {}
    if not paragraphs:
        return {}

    source_name = feed_source_name or hostname_from_url(url)
    return {
        "id": stable_id(url), "url": url, "title": title[:220] if title else "",
        "image": image, "paragraphs": paragraphs, "source_name": source_name,
        "published_at": iso(pub_dt), "keyword": slugify(keyword)
    }

async def crawl_keyword(client: httpx.AsyncClient, keyword: str, hours_max: int,
                        strict_h1: bool, strict_img: bool) -> List[Dict[str, Any]]:
    try:
        r = await client.get(google_news_rss(keyword), timeout=20.0,
                             headers={"User-Agent":"NewsAutomation/1.0"})
        if r.status_code != 200: return []
        feed = feedparser.parse(r.text)
    except Exception:
        return []
    now = now_utc(); cutoff = now - timedelta(hours=hours_max)
    tasks = []
    for entry in feed.entries[:30]:
        link = entry.get("link"); if not link: continue
        pub = from_pubdate_struct(entry.get("published_parsed")) or now
        if pub < cutoff: continue
        src = None
        try: src = entry.get("source", {}).get("title")
        except Exception: pass
        tasks.append(process_article(client, link, keyword, pub, entry.get("title"), src,
                                     strict_h1, strict_img))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: List[Dict[str, Any]] = []
    for it in results:
        if isinstance(it, dict) and it.get("paragraphs"):
            out.append(it); db_upsert(it)
    return out

# ========================== App Factory + Rotas ==========================

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
        keywords: List[str] = Body(default=["política", "economia"]),
        hours_max: int = Body(default=12),
        strict: bool = Body(default=True),
        require_image: bool = Body(default=True),
    ):
        """Coleta por palavras-chave. 'strict' exige H1; 'require_image' exige IMG."""
        async with httpx.AsyncClient(follow_redirects=True) as client:
            res: Dict[str, Any] = {}
            for kw in keywords:
                items = await crawl_keyword(client, kw, hours_max,
                                            strict_h1=strict, strict_img=require_image)
                res[slugify(kw)] = [{"id":it["id"],"title":it["title"],"source":it["source_name"]} for it in items]
            return {"collected": res}

    @app.post("/add")
    async def add_link(
        url: str = Body(..., embed=True),
        keyword: str = Body("geral", embed=True),
        strict: bool = Body(default=True),
        require_image: bool = Body(default=True),
    ):
        """Ingestão de um link único (precisa ser página pública)."""
        async with httpx.AsyncClient(follow_redirects=True) as client:
            item = await process_article(client, url, keyword, now_utc(), None, None,
                                         strict_h1=strict, strict_img=require_image)
            if not item or not item.get("paragraphs"):
                raise HTTPException(status_code=400, detail="Não foi possível extrair conteúdo desse link.")
            db_upsert(item)
            return {"id": item["id"], "title": item["title"], "permalink": f"/item/{item['id']}", "keyword": item["keyword"]}

    @app.get("/api/list")
    def api_list(keyword: str = Query(...), hours: int = Query(12, ge=1, le=72)):
        """JSON simples para integração."""
        return {"items": db_list_by_keyword(slugify(keyword), since_hours=hours)}

    @app.get("/api/json/{keyword_slug}")
    def api_json(keyword_slug: str, hours: int = Query(12, ge=1, le=72)):
        """Rota curta para JSON."""
        return {"items": db_list_by_keyword(keyword_slug, since_hours=hours)}

    @app.get("/rss/{keyword_slug}")
    def rss_feed(request: Request, keyword_slug: str, hours: int = Query(12, ge=1, le=72)):
        """RSS 2.0 para cada palavra-chave."""
        rows = db_list_by_keyword(keyword_slug, since_hours=hours)
        base = f"{request.url.scheme}://{request.headers.get('host','')}".rstrip("/")
        chan_title = f"News Automation — {keyword_slug}"
        chan_link = f"{base}/q/{keyword_slug}"
        chan_desc = f"Itens recentes para '{keyword_slug}' (últimas {hours}h)."
        parts = [f'<?xml version="1.0" encoding="UTF-8"?>',
                 f'<rss version="2.0"><channel>',
                 f'<title>{escape(chan_title)}</title>',
                 f'<link>{escape(chan_link)}</link>',
                 f'<description>{escape(chan_desc)}</description>']
        for r in rows:
            link = f"{base}/item/{r['id']}"
            title = escape(r.get("title") or "(sem título)")
            guid = r["id"]
            pub = r.get("published_at") or r.get("created_at") or iso(now_utc())
            # descrição curta com imagem
            img = r.get("image") or ""
            desc_html = f'<![CDATA[{"<img src=\'%s\' /><br/>" % img if img else ""}<a href="{escape(r["url"])}">Matéria Original</a>]]>'
            parts += [f"<item><title>{title}</title><link>{escape(link)}</link><guid isPermaLink='false'>{guid}</guid><pubDate>{pub}</pubDate><description>{desc_html}</description></item>"]
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
        if it.get("image"):
            parts.append(f"<img src='{it['image']}' alt='imagem'>")
        for p in it.get("paragraphs", []):
            parts.append(f"<p>{p}</p>")
        parts.append(
            f"<p><em>Fonte: <a href='{it['url']}' rel='nofollow noopener' target='_blank'>Matéria Original</a></em></p>"
        )
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
        for r in rows:
            parts.append(f"<li><a href='/item/{r['id']}'>{r['title']}</a> — {r['source_name']}</li>")
        parts.append("</ul>")
        return HTMLResponse("".join(parts))

    @app.get("/", response_class=HTMLResponse)
    def root():
        idx = Path("static/index.html")
        if idx.exists():
            return HTMLResponse(idx.read_text(encoding="utf-8"))
        return HTMLResponse(
            "<p>UI não encontrada. Crie <code>static/index.html</code> no projeto. "
            "Endpoints: <code>/crawl</code>, <code>/add</code>, <code>/api/list</code>, <code>/api/json/{slug}</code>, "
            "<code>/rss/{slug}</code>, <code>/item/{id}</code>, <code>/q/{slug}</code>, <code>/healthz</code>.</p>"
        )

    return app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
