# news_automation.py - Completo com busca web, busca por palavra-chave e visualização conteúdo completa com template HTML

import os
import re
import json
import base64
import hashlib
import sqlite3
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse, unquote
from html import escape

import httpx
import feedparser
from fastapi import FastAPI, Body, Query, Request, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from readability import Document as ReadabilityDoc

DB_PATH = os.getenv("DB_PATH", "/data/news.db")

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

def stable_id(url: str) -> str:
    h = hashlib.sha256(url.encode("utf-8")).digest()[:9]
    return base64.urlsafe_b64encode(h).decode("ascii").rstrip("=")

def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r'\s+', '-', text)
    text = re.sub(r'[^a-z0-9\-]', '', text)
    return text

def db_init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            url TEXT UNIQUE,
            title TEXT,
            paragraphs TEXT,
            published_at TEXT,
            keyword TEXT,
            created_at TEXT
        )
    """)
    con.commit()
    con.close()

def db_upsert(item: Dict[str, Any]) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO items (id, url, title, paragraphs, published_at, keyword, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            paragraphs=excluded.paragraphs,
            published_at=excluded.published_at,
            keyword=excluded.keyword,
            created_at=excluded.created_at
    """, (
        item['id'], item['url'], item['title'], json.dumps(item.get('paragraphs', []), ensure_ascii=False),
        item['published_at'], item['keyword'], iso(now_utc())
    ))
    con.commit()
    con.close()

def db_list_by_keyword(keyword_slug: str, hours: int = 12) -> List[Dict[str, Any]]:
    cutoff = iso(now_utc() - timedelta(hours=hours))
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("""
        SELECT id, url, title, paragraphs, published_at
        FROM items WHERE keyword=? AND created_at > ? ORDER BY created_at DESC
    """, (keyword_slug, cutoff))
    rows = cur.fetchall()
    con.close()
    out = []
    for r in rows:
        out.append({
            "id": r[0], "url": r[1], "title": r[2],
            "paragraphs": json.loads(r[3] or "[]"),
            "published_at": r[4]
        })
    return out

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
def startup():
    db_init()

@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <h1>RS-AUTO-BUSCADOR Online</h1>
    <p>Use /crawl?keywords=palavra e /rss/palavra para testar</p>
    <p>Use <a href="/search_news">/search_news</a> para busca avançada com visualizador completo.</p>
    """

@app.get("/healthz")
async def healthz():
    return {"ok": True, "time": iso(now_utc()), "db": DB_PATH}

@app.get("/crawl")
async def crawl(keywords: str = Query(...)):
    kw_slug = slugify(keywords)
    url = f"https://news.google.com/rss/search?q={quote_plus(keywords)}&hl=pt-BR&gl=BR&ceid=BR:pt-419"

    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        feed = feedparser.parse(r.text)

        for entry in feed.entries[:20]:
            paragraphs = [entry.summary] if hasattr(entry, "summary") else []
            published = entry.get("published", iso(now_utc()))
            item = {
                "id": stable_id(entry.link),
                "url": entry.link,
                "title": entry.title,
                "paragraphs": paragraphs,
                "published_at": published,
                "keyword": kw_slug
            }
            db_upsert(item)

    return {"message": f"Coleta realizada para '{keywords}'"}

@app.get("/rss/{keyword}")
async def rss(keyword: str, hours: int = 12):
    kw_slug = slugify(keyword)
    rows = db_list_by_keyword(kw_slug, hours)
    if not rows:
        raise HTTPException(status_code=404, detail="Nenhuma notícia encontrada para essa palavra-chave.")
    items_xml = ""
    for r in rows:
        desc_html = ""
        for p in r.get("paragraphs", []):
            desc_html += f"<p>{escape(p)}</p>"
        items_xml += f"""
        <item>
            <title>{escape(r['title'])}</title>
            <link>{escape(r['url'])}</link>
            <guid>{r['id']}</guid>
            <pubDate>{r['published_at']}</pubDate>
            <description><![CDATA[{desc_html}]]></description>
            <content:encoded><![CDATA[{desc_html}]]></content:encoded>
        </item>
        """
    rss_content = f"""<?xml version="1.0" encoding="UTF-8" ?>
    <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
    <channel>
        <title>RS-AUTO-BUSCADOR — {escape(kw_slug)}</title>
        <link>https://rs-auto-buscador.onrender.com/rss/{kw_slug}</link>
        <description>Feed de notícias para {escape(kw_slug)}</description>
        {items_xml}
    </channel>
    </rss>"""
    return Response(content=rss_content, media_type="application/rss+xml; charset=utf-8")

@app.get("/search_news", response_class=HTMLResponse)
async def search_news_form(request: Request,
        keyword: Optional[str] = Query(None),
        hours: int = Query(12, ge=1, le=72)):

    results = []
    error = None
    if keyword:
        kw_slug = slugify(keyword)
        rows = db_list_by_keyword(kw_slug, hours)
        if not rows:
            error = f"Nenhuma notícia encontrada para '{keyword}' nas últimas {hours} horas."
        else:
            for r in rows:
                content_text = "\n\n".join(r.get("paragraphs", []))
                results.append({
                    "title": r["title"],
                    "url": r["url"],
                    "published_at": r["published_at"],
                    "content": content_text
                })

    return templates.TemplateResponse("search_news.html", {
        "request": request,
        "keyword": keyword or "",
        "hours": hours,
        "results": results,
        "error": error
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
