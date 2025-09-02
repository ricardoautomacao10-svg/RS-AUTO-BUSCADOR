# news_automation.py
# Automação leve de notícias:
# - Busca por palavras-chave (Google News RSS) nas últimas X horas
# - Extrai H1, imagem principal e parágrafos "inteiros" (limpos)
# - Remove "leia mais/também", publicidade e CTAs
# - Gera permalink fixo /item/{id} e lista por /q/{slug}
# - Endpoint /add para ingerir 1 link específico
#
# Requisitos (requirements.txt):
# fastapi
# uvicorn[standard]
# httpx
# feedparser
# beautifulsoup4
# trafilatura
# python-slugify

import os
import re
import json
import base64
import hashlib
import sqlite3
import asyncio
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

# ===== Config do banco (Render: use Disk montado em /data) =====
DB_PATH = os.getenv("DB_PATH", "/data/news.db")

# slugify opcional; se não houver, fallback simples
try:
    from slugify import slugify
except Exception:  # pragma: no cover
    def slugify(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"\s+", "-", s)
        s = re.sub(r"[^a-z0-9\-]+", "", s)
        return s

# trafilatura é opcional (melhora extração quando HTML é ruim)
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
    """ID curto, estável, baseado no URL (bom para permalink)."""
    h = hashlib.sha256(url.encode("utf-8")).digest()[:9]
    return base64.urlsafe_b64encode(h).decode("ascii").rstrip("=")

def hostname_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc
        return re.sub(r"^www\.", "", host)
    except Exception:
        return "fonte"

BAD_SNIPPETS = [
    "leia mais", "leia também", "publicidade", "anúncio",
    "assine", "assinar", "clique aqui", "veja também",
    "continue lendo", "continue a ler", "compartilhe",
    "siga-nos", "newsletter", "inscreva-se", "oferta"
]

def clean_paragraph(p: str) -> Optional[str]:
    txt = re.sub(r"\s+", " ", p or "").strip()
    if not txt:
        return None
    low = txt.lower()
    if any(b in low for b in BAD_SNIPPETS):
        return None
    # descarta muito curtos (breadcrumbs/legendas/CTAs)
    if len(txt) < 25:
        return None
    return txt

def extract_og_image(soup: BeautifulSoup) -> Optional[str]:
    m = soup.find("meta", property="og:image")
    if m and m.get("content"):
        return m["content"].strip()
    m = soup.find("meta", attrs={"name": "twitter:image"})
    if m and m.get("content"):
        return m["content"].strip()
    # fallback: primeira imagem plausível
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
            url,
            timeout=15.0,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; NewsAutomation/1.0)",
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            },
            follow_redirects=True,
        )
        ctype = r.headers.get("Content-Type", "")
        if 200 <= r.status_code < 300 and "text/html" in ctype:
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
    # Foco em <article>; se não existir, cair para o <body>
    root = soup.find("article") or soup.body or soup
    ps: List[str] = []
    for p in root.find_all("p"):
        # ignora p dentro de scripts/nav/aside/footer/header/figcaption
        if p.find_parent(["script", "nav", "aside", "footer", "header", "noscript", "figure"]):
            continue
        txt = p.get_text(" ", strip=True)
        c = clean_paragraph(txt)
        if c:
            ps.append(c)

    # fallback: trafilatura, quando não achou nada com <p>
    if not ps and trafilatura:
        try:
            extracted = trafilatura.extract(
                html,
                include_comments=False,
                include_tables=False,
                include_images=False,
                favor_recall=True,
            )
            if extracted:
                parts = [clean_paragraph(t) for t in re.split(r"\n{2,}", extracted)]
                ps = [p for p in parts if p]
        except Exception:
            pass
    return ps

def first_image_from_html(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    return extract_og_image(soup)


# ========================== Banco de Dados ==========================

def db_init() -> None:
    dirpath = os.path.dirname(DB_PATH)
    if dirpath:
        os.makedirs(dirpath, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            url TEXT UNIQUE,
            title TEXT,
            image TEXT,
            paragraphs TEXT,   -- JSON array
            source_name TEXT,
            published_at TEXT, -- ISO
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
        item["id"],
        item["url"],
        item.get("title"),
        item.get("image"),
        json.dumps(item.get("paragraphs", []), ensure_ascii=False),
        item.get("source_name"),
        item.get("published_at"),
        item.get("keyword"),
        iso(now_utc()),
    ))
    con.commit()
    con.close()

def db_get(id_: str) -> Optional[Dict[str, Any]]:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("""
        SELECT id,url,title,image,paragraphs,source_name,published_at,keyword,created_at
        FROM items WHERE id=?
    """, (id_,))
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    return {
        "id": row[0],
        "url": row[1],
        "title": row[2],
        "image": row[3],
        "paragraphs": json.loads(row[4] or "[]"),
        "source_name": row[5],
        "published_at": row[6],
        "keyword": row[7],
        "created_at": row[8],
    }

def db_list_by_keyword(slug: str, since_hours: int = 12) -> List[Dict[str, Any]]:
    cutoff = now_utc() - timedelta(hours=since_hours)
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("""
        SELECT id,url,title,image,source_name,published_at,created_at
        FROM items
        WHERE keyword = ? AND created_at >= ?
        ORDER BY created_at DESC
    """, (slug, iso(cutoff)))
    out: List[Dict[str, Any]] = []
    for r in cur.fetchall():
        out.append({
            "id": r[0],
            "url": r[1],
            "title": r[2],
            "image": r[3],
            "source_name": r[4],
            "published_at": r[5],
            "created_at": r[6],
        })
    con.close()
    return out


# ========================== Coleta (RSS + extração) ==========================

def google_news_rss(keyword: str, lang: str = "pt-BR", region: str = "BR") -> str:
    # "when:12h" ajuda a focar período; ainda filtramos por data.
    q = quote_plus(f'{keyword} when:12h')
    return f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={region}&ceid=BR:pt-419"

async def process_article(
    client: httpx.AsyncClient,
    url: str,
    keyword: str,
    pub_dt: datetime,
    feed_title: Optional[str],
    feed_source_name: Optional[str],
) -> Dict[str, Any]:
    html = await fetch_html(client, url)
    if not html:
        return {}
    title = title_from_html(html) or (feed_title or "")
    paragraphs = paragraphs_from_html(html)
    image = first_image_from_html(html)
    source_name = feed_source_name or hostname_from_url(url)
    if not paragraphs:
        return {}
    return {
        "id": stable_id(url),
        "url": url,
        "title": title[:220] if title else "",
        "image": image,
        "paragraphs": paragraphs,
        "source_name": source_name,
        "published_at": iso(pub_dt),
        "keyword": slugify(keyword),
    }

async def crawl_keyword(client: httpx.AsyncClient, keyword: str, hours_max: int = 12) -> List[Dict[str, Any]]:
    rss_url = google_news_rss(keyword)
    try:
        r = await client.get(rss_url, timeout=15.0, headers={"User-Agent": "NewsAutomation/1.0"})
        if r.status_code != 200:
            return []
        feed_text = r.text
    except Exception:
        return []

    feed = feedparser.parse(feed_text)
    now = now_utc()
    cutoff = now - timedelta(hours=hours_max)
    limit = 20  # manter leve

    tasks: List[asyncio.Task] = []
    for entry in feed.entries[:limit]:
        link = entry.get("link")
        if not link:
            continue
        pub = from_pubdate_struct(entry.get("published_parsed")) or now
        if pub < cutoff:
            continue
        entry_source = None
        try:
            src_obj = entry.get("source", {})
            entry_source = getattr(src_obj, "title", None) or src_obj.get("title")
        except Exception:
            entry_source = None
        tasks.append(asyncio.create_task(
            process_article(client, link, keyword, pub, entry.get("title"), entry_source)
        ))

    out: List[Dict[str, Any]] = []
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for it in results:
        if isinstance(it, dict) and it.get("paragraphs"):
            out.append(it)
            db_upsert(it)
    return out


# ========================== App Factory + Rotas ==========================

def create_app() -> FastAPI:
    db_init()
    app = FastAPI(title=APP_TITLE)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"], allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "time": iso(now_utc()), "db": DB_PATH}

    @app.post("/crawl")
    async def crawl(
        keywords: List[str] = Body(default=["política", "economia"]),
        hours_max: int = Body(default=12),
    ):
        """
        Coleta notícias por palavras-chave, filtra por últimas 'hours_max' horas,
        salva no DB e retorna IDs/títulos coletados.
        """
        async with httpx.AsyncClient(follow_redirects=True) as client:
            results: Dict[str, Any] = {}
            for kw in keywords:
                items = await crawl_keyword(client, kw, hours_max=hours_max)
                results[slugify(kw)] = [
                    {"id": it["id"], "title": it["title"], "source": it["source_name"]}
                    for it in items
                ]
            return {"collected": results}

    @app.post("/add")
    async def add_link(
        url: str = Body(..., embed=True),
        keyword: str = Body("geral", embed=True),
    ):
        """
        Ingestão direta de um link específico (gera permalink fixo).
        """
        async with httpx.AsyncClient(follow_redirects=True) as client:
            pub_dt = now_utc()
            item = await process_article(client, url, keyword, pub_dt, None, None)
            if not item or not item.get("paragraphs"):
                raise HTTPException(status_code=400, detail="Não foi possível extrair conteúdo desse link.")
            db_upsert(item)
            return {
                "id": item["id"],
                "title": item["title"],
                "permalink": f"/item/{item['id']}",
                "keyword": item["keyword"],
            }

    @app.get("/item/{id}", response_class=HTMLResponse)
    def view_item(id: str):
        """
        Renderiza a matéria limpa (H1, imagem, <p>) e encerra com 'Fonte: Matéria Original'.
        """
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
        """
        Lista itens recentes por palavra-chave (slug), com links fixos para /item/{id}.
        """
        rows = db_list_by_keyword(keyword_slug, since_hours=hours)
        if not rows:
            return HTMLResponse(
                "<h1>Nada encontrado</h1><p>Faça POST /crawl ou /add para coletar.</p>",
                status_code=404,
            )
        parts: List[str] = []
        parts.append(
            "<!doctype html><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'/>"
            "<style>"
            "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,Helvetica,Ubuntu;"
            "max-width:860px;margin:40px auto;padding:0 16px}"
            "li{margin:10px 0}"
            "a{text-decoration:none}"
            "</style>"
        )
        parts.append(f"<h1>Resultados: {keyword_slug}</h1><ul>")
        for r in rows:
            parts.append(f"<li><a href='/item/{r['id']}'>{r['title']}</a> — {r['source_name']}</li>")
        parts.append("</ul>")
        return HTMLResponse("".join(parts))

    @app.get("/", response_class=PlainTextResponse)
    def root():
        return (
            "OK - Endpoints:\n"
            "POST /crawl {keywords: [\"sua palavra\"], hours_max: 12}\n"
            "POST /add {url: \"https://...\", keyword: \"slug-opcional\"}\n"
            "GET  /item/{id}\n"
            "GET  /q/{slug}\n"
            "GET  /healthz\n"
        )

    return app


# Exporte também um 'app' pronto (para import direto em ASGI)
app = create_app()

# Execução local (não usado no Render)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
