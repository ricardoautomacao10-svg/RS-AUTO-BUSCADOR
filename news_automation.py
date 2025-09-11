import os
import hashlib
import base64
import sqlite3
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus
from html import escape

import httpx
import feedparser
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form, Query
from fastapi.responses import HTMLResponse, Response

# === CONFIGURE SUA CHAVE OPENROUTER AQUI ===
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "SUA_CHAVE_OPENROUTER_AQUI")

DB_PATH = "./news.db"

def stable_id(url: str) -> str:
    h = hashlib.sha256(url.encode()).digest()[:9]
    return base64.urlsafe_b64encode(h).decode().rstrip("=")

def br_now():
    # Horário Brasil/São Paulo (GMT-3)
    return datetime.now(timezone(timedelta(hours=-3)))

def db_init():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            url TEXT,
            titulo TEXT,
            subtitulo TEXT,
            meta TEXT,
            texto TEXT,
            tags TEXT,
            img TEXT,
            published_at TEXT
        )
    """)
    con.commit()
    con.close()

def db_upsert(item):
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT OR REPLACE INTO items
        (id, url, titulo, subtitulo, meta, texto, tags, img, published_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (item['id'], item['url'], item['titulo'], item['subtitulo'], item['meta'],
          item['texto'], item['tags'], item['img'], item['published_at']))
    con.commit()
    con.close()

def db_list_recent(hours: int):
    cutoff = (br_now() - timedelta(hours=hours)).isoformat()
    con = sqlite3.connect(DB_PATH)
    cur = con.execute("""
        SELECT id, url, titulo, subtitulo, meta, texto, tags, img, published_at
        FROM items WHERE published_at >= ? ORDER BY published_at DESC
    """, (cutoff,))
    rows = cur.fetchall()
    con.close()
    return [{
        "id": r[0],
        "url": r[1],
        "titulo": r[2],
        "subtitulo": r[3],
        "meta": r[4],
        "texto": r[5],
        "tags": r[6],
        "img": r[7],
        "published_at": r[8]
    } for r in rows]

def is_ad_tag(tag):
    # Remove anúncios/widget
    if tag.name in ["script", "style", "iframe"]:
        return True
    cls = tag.get("class", [])
    for cl in cls if isinstance(cls, list) else [cls]:
        if "ad" in cl or "ads" in cl or "sponsor" in cl:
            return True
    return False

def scrape_content(url: str):
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        for ad in soup.find_all(is_ad_tag):
            ad.decompose()
        title_tag = soup.find("h1")
        title = title_tag.get_text(strip=True) if title_tag else ""
        img_tag = soup.find("img")
        img = img_tag["src"] if img_tag and img_tag.has_attr("src") else ""
        if not img:
            return "", "", ""
        paragraphs = soup.find_all("p")
        content = "\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))
        return title, img, content
    except:
        return "", "", ""

def enrich_news_with_ai(title, img_url, content):
    # Prompt pedindo todos campos em JSON
    prompt = f"""Reescreva o texto abaixo criando um artigo inédito, objetivo, fluido:
Título: {title}
Imagem: {img_url}
Conteúdo: {content}
Retorne no seguinte formato JSON:
{{
  "titulo": "...",
  "subtitulo": "...",
  "meta": "...",
  "texto": "...",
  "tags": "tag1, tag2, tag3",
  "img": "URL"
}}"""
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}
    body = {
        "model": "gpt-3.5-turbo",  # ou outro modelo que você tem acesso
        "messages": [{"role": "user", "content": prompt}]
    }
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=body,
            timeout=60
        )
        result = resp.json()
        msg = result["choices"][0]["message"]["content"]
        data = json.loads(msg)
        return data
    except Exception:
        return None

app = FastAPI()

@app.on_event("startup")
def startup():
    db_init()

@app.get("/", response_class=HTMLResponse)
def homepage():
    return """
    <h1>Gerar RSS IA - Notícias</h1>
    <form action="/rss" method="get">
      Palavra-chave: <input name="keyword" required style="width:300px"/>
      Últimas horas para buscar: <input name="hours" type="number" min="1" max="72" value="12"/>
      <button type="submit">Buscar Notícias IA e Gerar RSS</button>
    </form>
    """

@app.get("/rss")
def rss(keyword: str, hours: int = Query(12)):
    # Busca e processa automaticamente
    url = f"https://news.google.com/rss/search?q={quote_plus(keyword)}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    r = httpx.get(url)
    feed = feedparser.parse(r.text)
    unique_links = set()
    for entry in feed.entries:
        link = entry.link
        if link in unique_links: continue
        unique_links.add(link)
        title, img, content = scrape_content(link)
        if not title or not img or not content: continue
        ai_data = enrich_news_with_ai(title, img, content)
        if not ai_data or not ai_data.get("titulo") or not ai_data.get("img"):
            continue  # só salva se a IA retornou artigo válido com imagem
        item = {
            "id": stable_id(link),
            "url": link,
            "titulo": ai_data["titulo"],
            "subtitulo": ai_data.get("subtitulo", ""),
            "meta": ai_data.get("meta", ""),
            "texto": ai_data["texto"],
            "tags": ai_data.get("tags", ""),
            "img": ai_data["img"],
            "published_at": br_now().isoformat()
        }
        db_upsert(item)

    items = db_list_recent(hours)
    if not items:
        return Response(f"Nenhuma notícia encontrada nas últimas {hours} horas.", media_type="text/plain")

    items_xml = ""
    for i in items:
        description = f"""
        <img src='{escape(i['img'])}'/><br>
        <h1>{escape(i['titulo'])}</h1><br>
        <h2>{escape(i['subtitulo'])}</h2><br>
        <div>{escape(i['texto']).replace('\n','<br>')}</div><br>
        <i>Meta: {escape(i['meta'])}</i><br>
        <b>Tags:</b> {escape(i['tags'])}
        """
        items_xml += f"""
        <item>
            <title>{escape(i['titulo'])}</title>
            <link>{escape(i['url'])}</link>
            <guid>{i['id']}</guid>
            <pubDate>{i['published_at']}</pubDate>
            <description><![CDATA[{description}]]></description>
        </item>
        """

    rss = f"""<?xml version="1.0" encoding="UTF-8" ?>
    <rss version="2.0">
        <channel>
            <title>RSS Notícias IA: {escape(keyword)}</title>
            <link>/rss?keyword={quote_plus(keyword)}</link>
            <description>Notícias IA, últimas {hours}h</description>
            {items_xml}
        </channel>
    </rss>"""

    return Response(rss, media_type="application/rss+xml")

