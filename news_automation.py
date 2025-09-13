import os
import hashlib
import base64
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from html import escape

import feedparser
import httpx
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, Response

DB_PATH = "./news.db"

def stable_id(url: str) -> str:
    h = hashlib.sha256(url.encode()).digest()[:9]
    return base64.urlsafe_b64encode(h).decode().rstrip("=")

def br_now():
    return datetime.now(timezone(timedelta(hours=-3)))

def db_init():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            url TEXT,
            title TEXT,
            content TEXT,
            published_at TEXT
        )
    """)
    con.commit()
    con.close()

def db_upsert(item):
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT OR REPLACE INTO items (id, url, title, content, published_at)
        VALUES (?, ?, ?, ?, ?)
    """, (item['id'], item['url'], item['title'], item['content'], item['published_at']))
    con.commit()
    con.close()

def db_list_recent(hours: int):
    cutoff = (br_now() - timedelta(hours=hours)).isoformat()
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("SELECT id, url, title, content, published_at FROM items WHERE published_at >= ? ORDER BY published_at DESC", (cutoff,))
    rows = cur.fetchall()
    con.close()
    return [{
        "id": r[0],
        "url": r[1],
        "title": r[2],
        "content": r[3],
        "published_at": r[4]
    } for r in rows]

def scrape_content(url: str):
    try:
        r = requests.get(url, timeout=12, headers={'User-Agent': 'Mozilla/5.0'})
        soup = BeautifulSoup(r.text, "html.parser")

        # Tenta título h1 ou title
        title_tag = soup.find("h1") or soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else "Sem título"

        # Conteúdo simples concatenando parágrafos
        paragraphs = soup.find_all("p")
        content = "\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))

        return title, content
    except:
        return "Sem título", ""

app = FastAPI()

@app.on_event("startup")
def startup():
    db_init()

@app.get("/", response_class=HTMLResponse)
def homepage():
    return """
    <h1>Busca simplificada de notícias para RSS urgente</h1>
    <form action="/rss" method="get">
      Palavra-chave: <input name="keyword" required style="width:300px"/>
      Últimas horas para buscar: <input name="hours" type="number" min="1" max="72" value="12"/>
      <button type="submit">Buscar e gerar RSS</button>
    </form>
    """

@app.get("/rss")
def rss(keyword: str, hours: int = Query(12)):
    url = f"https://news.google.com/rss/search?q={quote_plus(keyword)}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    r = httpx.get(url)
    feed = feedparser.parse(r.text)
    unique_links = set()
    for entry in feed.entries:
        link = entry.link
        if link in unique_links:
            continue
        unique_links.add(link)

        title, content = scrape_content(link)
        published_at = br_now().isoformat()

        item = {
            "id": stable_id(link),
            "url": link,
            "title": title,
            "content": content,
            "published_at": published_at
        }
        db_upsert(item)

    items = db_list_recent(hours)
    if not items:
        return Response(f"Nenhuma notícia encontrada nas últimas {hours} horas para '{keyword}'.", media_type="text/plain")

    items_xml = ""
    for i in items:
        description = escape(i['content']).replace('\n', '<br>')
        items_xml += f"""
        <item>
            <title>{escape(i['title'])}</title>
            <link>{escape(i['url'])}</link>
            <guid>{i['id']}</guid>
            <pubDate>{i['published_at']}</pubDate>
            <description><![CDATA[{description}]]></description>
        </item>
        """

    rss = f"""<?xml version="1.0" encoding="utf-8" ?>
    <rss version="2.0">
        <channel>
            <title>RSS Notícias Simplificado: {escape(keyword)}</title>
            <link>/rss?keyword={quote_plus(keyword)}</link>
            <description>Notícias recentes coletadas via scraping - últimas {hours}h</description>
            {items_xml}
        </channel>
    </rss>"""

    return Response(rss, media_type="application/rss+xml")
