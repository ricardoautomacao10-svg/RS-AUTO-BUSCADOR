from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
import httpx
import feedparser
import hashlib
import base64
import sqlite3
import json
from datetime import datetime, timedelta, timezone
from html import escape
import re

app = FastAPI()
DB_PATH = "/data/news.db"

def now_utc():
    return datetime.now(timezone.utc)

def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()

def stable_id(url):
    h = hashlib.sha256(url.encode("utf-8")).digest()[:9]
    return base64.urlsafe_b64encode(h).decode("ascii").rstrip("=")

def slugify(text):
    text = text.lower()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^a-z0-9\-]", "", text)
    return text

def db_init():
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

def db_upsert(item):
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
        item['id'], item['url'], item['title'], json.dumps(item['paragraphs'], ensure_ascii=False),
        item['published_at'], item['keyword'], iso(now_utc())
    ))
    con.commit()
    con.close()

def db_list_by_keyword(keyword, hours=12):
    cutoff = iso(now_utc() - timedelta(hours=hours))
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("""
        SELECT id, url, title, paragraphs, published_at FROM items
        WHERE keyword = ? AND created_at > ?
        ORDER BY created_at DESC
    """, (keyword, cutoff))
    rows = cur.fetchall()
    con.close()
    res = []
    for r in rows:
        res.append({
            "id": r[0],
            "url": r[1],
            "title": r[2],
            "paragraphs": json.loads(r[3]),
            "published_at": r[4]
        })
    return res

@app.on_event("startup")
def startup():
    db_init()

@app.get("/", response_class=HTMLResponse)
async def form_get():
    return """
    <html><head><title>RS-AUTO-BUSCADOR</title></head>
    <body>
    <h1>Digite palavra-chave para gerar RSS</h1>
    <form method="post" action="/">
      Palavra-chave: <input type="text" name="keyword" required size="40"><br>
      Horas atrás (1-72): <input type="number" name="hours" value="12" min="1" max="72"><br>
      <input type="submit" value="Gerar RSS">
    </form>
    </body></html>
    """

@app.post("/", response_class=HTMLResponse)
async def form_post(keyword: str = Form(...), hours: int = Form(12)):
    kw_slug = slugify(keyword)
    url = f"https://news.google.com/rss/search?q={keyword}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
        feed = feedparser.parse(r.text)
        for e in feed.entries[:20]:
            paras = [e.summary] if hasattr(e, "summary") else []
            pub = e.get("published", iso(now_utc()))
            db_upsert({
                "id": stable_id(e.link),
                "url": e.link,
                "title": e.title,
                "paragraphs": paras,
                "published_at": pub,
                "keyword": kw_slug
            })
    results = db_list_by_keyword(kw_slug, hours)
    rss_url = f"/rss/{kw_slug}?hours={hours}"
    html_results = "".join([
        f"<div><b><a href='{escape(r['url'])}'>{escape(r['title'])}</a></b> ({r['published_at']})<br><pre>{escape('\\n\\n'.join(r['paragraphs']))}</pre></div><hr>" for r in results
    ])
    return f"""
    <html><head><title>Resultados - {escape(keyword)}</title></head><body>
    <h1>Resultados para: {escape(keyword)}</h1>
    <p><a href='{rss_url}' target='_blank'>Link RSS gerado (clique para abrir)</a></p>
    {html_results or '<p>Nenhum resultado encontrado.</p>'}
    <a href='/'>Nova busca</a>
    </body></html>
    """

@app.get("/rss/{keyword}")
async def rss(keyword: str, hours: int = 12):
    kw_slug = slugify(keyword)
    rows = db_list_by_keyword(kw_slug, hours)
    if not rows:
        return Response(content="Nenhuma notícia encontrada.", media_type="text/plain", status_code=404)
    items = ""
    for r in rows:
        content_html = "".join(f"<p>{escape(p)}</p>" for p in r["paragraphs"])
        items += f"""
        <item>
            <title>{escape(r['title'])}</title>
            <link>{escape(r['url'])}</link>
            <guid>{r['id']}</guid>
            <pubDate>{r['published_at']}</pubDate>
            <description><![CDATA[{content_html}]]></description>
            <content:encoded><![CDATA[{content_html}]]></content:encoded>
        </item>
        """
    rss = f"""<?xml version="1.0" encoding="UTF-8" ?>
    <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
    <channel>
      <title>RS-AUTO-BUSCADOR — {escape(kw_slug)}</title>
      <link>/rss/{kw_slug}</link>
      <description>Feed de notícias para {escape(kw_slug)}</description>
      {items}
    </channel>
    </rss>"""
    return Response(content=rss, media_type="application/rss+xml; charset=utf-8")
