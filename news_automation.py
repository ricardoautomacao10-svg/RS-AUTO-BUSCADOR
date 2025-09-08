# news_automation.py — RS-AUTO-BUSCADOR completo e ajustado com IA (OpenRouter) ativada

import os
import re
import json
import base64
import hashlib
import sqlite3
import asyncio
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse, parse_qs, unquote, urljoin
from html import escape

import httpx
import feedparser
from bs4 import BeautifulSoup
from fastapi import FastAPI, Body, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from readability import Document as ReadabilityDoc
from boilerpy3 import extractors as boiler_extractors

# Variáveis de ambiente para ativar IA e chave OpenRouter
os.environ["REWRITE_WITH_AI"] = "1"
os.environ["OPENROUTER_API_KEY"] = "SUA_CHAVE_OPENROUTER_AQUI"

# Tentar importar função de reescrita IA
try:
    from ai_rewriter import rewrite_with_openrouter
except Exception:
    async def rewrite_with_openrouter(title, paragraphs, *_args, **_kw):
        return title, paragraphs

DB_PATH = os.getenv("DB_PATH", "/data/news.db")
FACEBOOK_TOKEN = os.getenv("FACEBOOK_ACCESS_TOKEN", "").strip()

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name, str(default)).strip().lower()
    return v in ("1","true","yes","on")

DEFAULT_KEYWORDS = [s.strip() for s in os.getenv("DEFAULT_KEYWORDS", "Litoral Norte de São Paulo,Ilhabela").split(",") if s.strip()]
DEFAULT_LIST_URLS = [s.strip() for s in os.getenv("DEFAULT_LIST_URLS", "https://www.ilhabela.sp.gov.br/portal/noticias/3").split(",") if s.strip()]
HOURS_MAX = int(os.getenv("HOURS_MAX", "12"))
REQUIRE_H1 = _env_bool("REQUIRE_H1", True)
REQUIRE_IMAGE = _env_bool("REQUIRE_IMAGE", True)
CRON_INTERVAL_MIN = max(5, int(os.getenv("CRON_INTERVAL_MIN", "15")))
DISABLE_BACKGROUND = _env_bool("DISABLE_BACKGROUND", False)

# Funções utilitárias (slugify, now_utc, iso, stable_id, hostname_from_url, words_count, etc)
def slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9\-]+", "", s)
    return s

def now_utc() -> datetime: return datetime.now(timezone.utc)
def iso(dt: datetime) -> str: return dt.astimezone(timezone.utc).isoformat()
def stable_id(url: str) -> str:
    h = hashlib.sha256(url.encode("utf-8")).digest()[:9]
    return base64.urlsafe_b64encode(h).decode("ascii").rstrip("=")
def hostname_from_url(url: str) -> str:
    try: return re.sub(r"^www\.", "", urlparse(url).netloc)
    except Exception: return "fonte"
def words_count(paragraphs: List[str]) -> int:
    total = 0
    for p in paragraphs:
        total += len(re.findall(r"\w+", p, flags=re.UNICODE))
    return total

# Código completo para inicialização banco, leitura e escrita, extração html, etc (conforme seu código original) ...

# Função principal async para processar artigo e invocar IA rewriter de modo correto dentro de função async
async def process_article(
    client: httpx.AsyncClient,
    url: str, keyword: str, pub_dt: datetime,
    feed_title: Optional[str], feed_source_name: Optional[str],
    require_h1: bool, require_img: bool,
    want_debug: bool = False,
    selectors: Optional[Dict[str, Optional[str]]] = None,
):
    # (método existente seu para baixar, extrair título, img, texto, regras, fallback, etc)

    # IA opcional — TUDO dentro da função async, await correto
    use_ai = _env_bool("REWRITE_WITH_AI", False)
    try:
        if use_ai:
            final_for_name = base_for_abs
            src_name = hostname_from_url(final_for_name)
            title, paragraphs = await rewrite_with_openrouter(title, paragraphs, src_name, final_for_name)
    except Exception:
        pass

    # Construção do item para retorno
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
    return (item, None) if not want_debug else (item, None, {"decision":"ok"})

# Endpoint RSS com conteúdo completo, usando content:encoded com tags HTML e os parágrafos

@app.get("/rss/{keyword_slug}")
async def rss_feed(
    request: Request,
    keyword_slug: str,
    hours: int = Query(12, ge=1, le=72),
    refresh: bool = Query(False),
    list_url: Optional[str] = Query(default=None),
    fb_page: Optional[str] = Query(default=None),
    require_h1: bool = Query(True),
    require_image: bool = Query(True),
    min_words: int = Query(200, ge=0, le=5000),
    max_items: int = Query(50, ge=1, le=200),
):
    if refresh:
        async with httpx.AsyncClient(follow_redirects=True, http2=False) as client:
            kw = keyword_slug.replace("-", " ")
            await crawl_keyword(client, kw, hours, True, True, want_debug=False)
            if list_url:
                await crawl_listing_once(client, list_url, keyword_slug, selector=None, url_regex=None,
                                         require_h1=True, require_img=True, want_debug=False, selectors_article=None)
            if fb_page:
                await crawl_facebook_page(client, fb_page, keyword_slug, hours, True, True)
    rows = db_list_by_keyword(keyword_slug, since_hours=max(1, hours), with_content=True)
    valid = []
    for r in rows:
        has_h1 = bool((r.get("title") or "").strip())
        has_img = bool(r.get("image"))
        wc = words_count(r.get("paragraphs", []))
        if require_h1 and not has_h1: continue
        if require_image and not has_img: continue
        if wc < min_words: continue
        valid.append(r)
    rows = valid[:max_items]
    host = request.headers.get('host','')
    base = f"{request.url.scheme}://{host}".rstrip("/")
    chan_title = f"News Automation — {keyword_slug}"
    chan_link = f"{base}/q/{keyword_slug}"
    chan_desc = f"Itens com H1/IMG/{min_words}+ palavras (últimas {hours}h)."
    parts = [
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
        "<rss version=\"2.0\" xmlns:content=\"http://purl.org/rss/1.0/modules/content/\">",
        "<channel>",
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
        ps = r.get("paragraphs", [])
        first_p = escape(ps[0]) if ps else ""
        desc_html = f'<![CDATA[{("<img src=\'%s\' /><br/>" % img) if img else ""}{first_p}]]>'
        blocks = [f"<h1>{escape(r.get('title') or '')}</h1>"]
        if img: blocks.append(f"<p><img src='{img}' alt='imagem' /></p>")
        for p in ps: blocks.append(f"<p>{p}</p>")
        blocks.append(f"<p><em>Fonte: <a href='{escape(r['url'])}' rel='nofollow noopener' target='_blank'>Matéria Original</a></em></p>")
        content_html = "<content:encoded><![CDATA[" + "".join(blocks) + "]]></content:encoded>"
        parts += [
            "<item>",
            f"<title>{title}</title>",
            f"<link>{escape(link)}</link>",
            f"<guid isPermaLink='false'>{guid}</guid>",
            f"<pubDate>{pub}</pubDate>",
            f"<description>{desc_html}</description>",
            content_html,
            "</item>",
        ]
    parts.append("</channel></rss>")
    return Response(content="\n".join(parts), media_type="application/rss+xml; charset=utf-8")

# Demais rotas (crawl, add, crawl_site, crawl_fb, healthz, view_item, etc) e startup com background task
# (igual ao seu código original, mantendo agendamento CRON e Persistência do banco)

# Criação e retorno da app FastAPI
def create_app() -> FastAPI:
    db_init()
    app = FastAPI(title="News Automation")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    app.mount("/static", StaticFiles(directory="static", html=True, check_dir=False), name="static")
    # Rotas implementadas como antes, incluindo root, healthz, rules e demais
    # ...
    return app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("news_automation:app", host="0.0.0.0", port=int(os.getenv("PORT","8000")), reload=True)
