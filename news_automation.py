import os
import re
import json
import base64
import hashlib
import sqlite3
from typing import List, Dict, Optional
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from html import escape

import httpx
import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser
from fastapi import FastAPI, Query, Request, Form
from fastapi.responses import HTMLResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates

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

def db_upsert(item: Dict):
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
    """, (item['id'], item['url'], item['title'], json.dumps(item['paragraphs'], ensure_ascii=False),
          item['published_at'], item['keyword'], iso(now_utc())))
    con.commit()
    con.close()

def db_list_by_keyword(keyword: str, hours: int = 12) -> List[Dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("""
        SELECT id, url, title, paragraphs, published_at FROM items
        WHERE keyword = ? AND published_at >= ?
        ORDER BY published_at DESC
    """, (keyword, cutoff))
    rows = cur.fetchall()
    con.close()
    out = []
    for r in rows:
        out.append({
            "id": r[0],
            "url": r[1],
            "title": r[2],
            "paragraphs": json.loads(r[3]),
            "published_at": r[4]
        })
    return out

def get_clean_title_content_date(url: str):
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        title_tag = soup.find("title")
        title = title_tag.text.strip() if title_tag else ""

        p_tag = soup.find("p")
        content = p_tag.text.strip() if p_tag else ""

        date_str = None
        for tag in soup.find_all(["time", "meta"]):
            if tag.name == "time" and tag.has_attr("datetime"):
                date_str = tag["datetime"]
                break
            elif tag.name == "meta" and tag.get("property") in ["article:published_time", "og:published_time"]:
                date_str = tag.get("content") or tag.get("value")
                break

        pub_date = None
        if date_str:
            try:
                pub_date = parser.parse(date_str)
            except:
                pub_date = None

        return title, content, pub_date
    except Exception:
        return "", "", None

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
def startup():
    db_init()

@app.get("/", response_class=HTMLResponse)
async def root():
    return """<h1>RS-AUTO-BUSCADOR Online</h1><p>Use <a href='/generate'>/generate</a> para gerar RSS com notícias.</p>"""

@app.get("/generate", response_class=HTMLResponse)
async def generate_form(request: Request):
    return templates.TemplateResponse("generate.html", {"request": request})

@app.post("/generate", response_class=HTMLResponse)
async def generate_result(request: Request,
                          keyword: Optional[str] = Form(None),
                          link: Optional[str] = Form(None),
                          hours: int = Form(12)):
    if not keyword and not link:
        return templates.TemplateResponse("generate.html", {
            "request": request,
            "error": "Informe uma palavra-chave ou um link.",
            "keyword": "",
            "link": "",
            "hours": hours,
            "results": []
        })

    kw_slug = slugify(link if link else keyword)
    if link:
        title, content, pub_date = get_clean_title_content_date(link)
        if not title:
            title = "Link adicionado manualmente"
        if not pub_date:
            pub_date = now_utc()
        item = {
            "id": stable_id(link),
            "url": link,
            "title": title,
            "paragraphs": [content],
            "published_at": iso(pub_date),
            "keyword": kw_slug
        }
        db_upsert(item)
    else:
        url = f"https://news.google.com/rss/search?q={quote_plus(keyword)}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
        async with httpx.AsyncClient() as client:
            r = await client.get(url)
            feed = feedparser.parse(r.text)
            seen_titles = set()
            for entry in feed.entries[:50]:
                l = entry.link
                title, content, pub_date = get_clean_title_content_date(l)
                if not title:
                    continue
                clean_title = title.lower().replace("'", "").replace("\"", "")
                if clean_title in seen_titles:
                    continue
                seen_titles.add(clean_title)
                if not pub_date:
                    pub_date = now_utc()
                item = {
                    "id": stable_id(l),
                    "url": l,
                    "title": title,
                    "paragraphs": [content],
                    "published_at": iso(pub_date),
                    "keyword": kw_slug
                }
                db_upsert(item)

    results = db_list_by_keyword(kw_slug, hours)
    rss_url = f"/rss/{kw_slug}?hours={hours}"
    return templates.TemplateResponse("generate.html", {
        "request": request,
        "keyword": keyword or "",
        "link": link or "",
        "hours": hours,
        "results": results,
        "rss_url": rss_url,
        "count": len(results),
        "error": None
    })

@app.get("/rss/{keyword}")
async def rss(keyword: str, hours: int = 12):
    rows = db_list_by_keyword(keyword, hours)
    if not rows:
        return Response(content="Nenhuma notícia encontrada.", media_type="text/plain", status_code=404)
    items_xml = ""
    for r in rows:
        desc_html = "".join(f"<p>{escape(p)}</p>" for p in r['paragraphs'])
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
        <title>RS-AUTO-BUSCADOR — {escape(keyword)}</title>
        <link>https://rs-auto-buscador.onrender.com/rss/{keyword}</link>
        <description>Feed de notícias para {escape(keyword)}</description>
        {items_xml}
    </channel>
    </rss>"""
    return Response(content=rss_content, media_type="application/rss+xml; charset=utf-8")
