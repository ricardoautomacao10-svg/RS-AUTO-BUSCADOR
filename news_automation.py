import os
import hashlib
import base64
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from html import escape

import httpx
import feedparser
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, Response

DB_PATH = "./news.db"

def stable_id(url: str) -> str:
    h = hashlib.sha256(url.encode()).digest()[:9]
    return base64.urlsafe_b64encode(h).decode().rstrip("=")

def db_init():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            url TEXT UNIQUE,
            title TEXT,
            img TEXT,
            content TEXT,
            published_at TEXT
        )
    """)
    con.commit()
    con.close()

def br_now():
    # Horário Brasil/São Paulo (GMT-3)
    return datetime.now(timezone(timedelta(hours=-3)))

def db_upsert(item):
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO items (id, url, title, img, content, published_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            img=excluded.img,
            content=excluded.content,
            published_at=excluded.published_at
    """, (item['id'], item['url'], item['title'], item['img'], item['content'], item['published_at']))
    con.commit()
    con.close()

def db_list_recent(hours: int):
    cutoff = (br_now() - timedelta(hours=hours)).isoformat()
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("SELECT id, url, title, img, content, published_at FROM items WHERE published_at >= ? ORDER BY published_at DESC", (cutoff,))
    rows = cur.fetchall()
    con.close()
    return [{
        "id": r[0],
        "url": r[1],
        "title": r[2],
        "img": r[3],
        "content": r[4],
        "published_at": r[5]
    } for r in rows]

def is_ad_tag(tag):
    # Remove tags que costumam ser anúncios ou widgets
    if tag.name in ["script", "style", "iframe"]:
        return True
    c = tag.get("class", [])
    if isinstance(c, str):
        c = [c]
    for cl in c:
        if "ad" in cl or "ads" in cl or "sponsored" in cl:
            return True
    return False

def scrape_content(url: str):
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        # Remove possíveis anúncios
        for tag in soup.find_all(is_ad_tag):
            tag.decompose()

        title_tag = soup.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else ""

        img_tag = soup.find("img")
        img = img_tag["src"] if img_tag and img_tag.has_attr("src") else ""
        if not img:
            return "", "", "", ""

        paragraphs = soup.find_all("p")
        content = "\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))

        return title, img, content, br_now().isoformat()
    except:
        return "", "", "", br_now().isoformat()

app = FastAPI()

@app.on_event("startup")
def startup():
    db_init()

@app.get("/", response_class=HTMLResponse)
def home():
    return """
    <h1>Digitar palavra-chave</h1>
    <form action="/rss" method="get">
      Palavra-chave: <input name="keyword" required>
      Últimas horas para buscar: <input name="hours" type="number" min="1" max="72" value="12">
      <button type="submit">Gerar RSS</button>
    </form>
    """

@app.get("/rss")
def rss(keyword: str, hours: int = Query(12)):
    # Busca no Google News RSS
    url = f"https://news.google.com/rss/search?q={quote_plus(keyword)}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    r = httpx.get(url)
    feed = feedparser.parse(r.text)
    unique_links = set()
    for entry in feed.entries:
        link = entry.link
        if link in unique_links: continue
        unique_links.add(link)
        title, img, content, published_at = scrape_content(link)
        # Só salva se tiver título, imagem e texto e for recente
        if not title or not img or not content: continue
        item = {
            "id": stable_id(link),
            "url": link,
            "title": title,
            "img": img,
            "content": content,
            "published_at": published_at
        }
        db_upsert(item)

    items = db_list_recent(hours)
    if not items:
        return Response(f"Nenhuma notícia encontrada nas últimas {hours} horas.", media_type="text/plain")

    items_xml = ""
    for i in items:
        description = f"<img src='{escape(i['img'])}'/><br><h1>{escape(i['title'])}</h1><br>{escape(i['content']).replace('\n','<br>')}"
        items_xml += f"""
        <item>
            <title>{escape(i['title'])}</title>
            <link>{escape(i['url'])}</link>
            <guid>{i['id']}</guid>
            <pubDate>{i['published_at']}</pubDate>
            <description><![CDATA[{description}]]></description>
        </item>
        """

    rss = f"""<?xml version="1.0" encoding="UTF-8" ?>
    <rss version="2.0">
        <channel>
            <title>RSS Notícias: {escape(keyword)}</title>
            <link>/rss?keyword={quote_plus(keyword)}</link>
            <description>Notícias filtradas por palavra-chave, últimas {hours}h</description>
            {items_xml}
        </channel>
    </rss>"""

    return Response(rss, media_type="application/rss+xml")
