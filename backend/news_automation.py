# backend/news_automation.py

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime, timezone
import sqlite3
import hashlib
import base64
import xml.etree.ElementTree as ET
import html
import json
import os

DATABASE_DIR = "./backend/db"
DATABASE = f"{DATABASE_DIR}/newsflowai.db"

app = FastAPI(title="NewsFlow AI Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Models

class NewsKeyword(BaseModel):
    id: Optional[int] = None
    keyword: Optional[str] = None
    source_url: Optional[str] = None
    source_type: str = "keyword"
    description: Optional[str] = None
    is_active: bool = True
    update_frequency: int = 10
    hours_back: int = 8
    last_updated: Optional[datetime] = None


class NewsArticle(BaseModel):
    id: Optional[int] = None
    title: str
    content: Optional[str] = None
    summary: Optional[str] = None
    source: Optional[str] = None
    url: Optional[str] = None
    published_date: Optional[datetime] = Field(default_factory=lambda: datetime.now(timezone.utc))
    keyword: Optional[str] = None
    relevance_score: Optional[int] = 80
    image_url: Optional[str] = None
    category: Optional[str] = None


class NewsFeed(BaseModel):
    id: Optional[int] = None
    feed_name: str
    keywords: List[str]
    feed_type: str = "rss"
    public_url: Optional[str] = None
    is_public: bool = True
    max_articles: int = 50

# DB utils

def ensure_db_dir():
    if not os.path.exists(DATABASE_DIR):
        os.makedirs(DATABASE_DIR)

def get_connection():
    ensure_db_dir()
    conn = sqlite3.connect(DATABASE, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS newskeywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT,
            source_url TEXT,
            source_type TEXT NOT NULL DEFAULT 'keyword',
            description TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            update_frequency INTEGER NOT NULL DEFAULT 10,
            hours_back INTEGER NOT NULL DEFAULT 8,
            last_updated TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS newsarticles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT,
            summary TEXT,
            source TEXT,
            url TEXT,
            published_date TIMESTAMP,
            keyword TEXT,
            relevance_score INTEGER,
            image_url TEXT,
            category TEXT
        );
        CREATE TABLE IF NOT EXISTS newsfeeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_name TEXT NOT NULL,
            keywords TEXT NOT NULL,
            feed_type TEXT NOT NULL DEFAULT 'rss',
            public_url TEXT,
            is_public INTEGER NOT NULL DEFAULT 1,
            max_articles INTEGER NOT NULL DEFAULT 50
        );
        """)
        conn.commit()

def stable_id(value: str) -> str:
    h = hashlib.sha256(value.encode('utf-8')).digest()[:9]
    return base64.urlsafe_b64encode(h).decode('utf-8').rstrip("=")

# CRUD keywords

@app.post("/keywords", response_model=NewsKeyword)
def create_keyword(keyword: NewsKeyword):
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO newskeywords (keyword, source_url, source_type, description, is_active, update_frequency, hours_back, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (keyword.keyword, keyword.source_url, keyword.source_type, keyword.description,
             int(keyword.is_active), keyword.update_frequency, keyword.hours_back, keyword.last_updated)
        )
        conn.commit()
        keyword.id = cur.lastrowid
    return keyword

@app.get("/keywords", response_model=List[NewsKeyword])
def list_keywords(order_by: Optional[str] = None):
    sql = "SELECT * FROM newskeywords"
    if order_by:
        allowed = ['keyword', 'last_updated', 'update_frequency']
        if order_by.lstrip('-') in allowed:
            direction = "DESC" if order_by.startswith('-') else "ASC"
            col = order_by.lstrip('-')
            sql += f" ORDER BY {col} {direction}"
    with get_connection() as conn:
        rows = conn.execute(sql).fetchall()
        keywords = [NewsKeyword(**row) for row in map(dict, rows)]
    return keywords

@app.patch("/keywords/{keyword_id}", response_model=NewsKeyword)
def update_keyword(keyword_id: int, keyword: NewsKeyword):
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM newskeywords WHERE id = ?", (keyword_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Keyword not found")
        cur = conn.execute(
            """UPDATE newskeywords SET keyword=?, source_url=?, source_type=?, description=?, 
            is_active=?, update_frequency=?, hours_back=?, last_updated=? WHERE id = ?""",
            (
                keyword.keyword or row['keyword'],
                keyword.source_url or row['source_url'],
                keyword.source_type or row['source_type'],
                keyword.description or row['description'],
                int(keyword.is_active) if keyword.is_active is not None else row['is_active'],
                keyword.update_frequency or row['update_frequency'],
                keyword.hours_back or row['hours_back'],
                keyword.last_updated or row['last_updated'],
                keyword_id
            )
        )
        conn.commit()
    return keyword

@app.delete("/keywords/{keyword_id}", status_code=204)
def delete_keyword(keyword_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM newskeywords WHERE id = ?", (keyword_id,))
        conn.commit()
    return Response(status_code=204)

# CRUD Articles

@app.get("/articles", response_model=List[NewsArticle])
def list_articles(keyword: Optional[str] = None, limit: int = 50):
    sql = "SELECT * FROM newsarticles"
    params = []
    if keyword:
        sql += " WHERE keyword = ?"
        params.append(keyword)
    sql += " ORDER BY published_date DESC LIMIT ?"
    params.append(limit)
    with get_connection() as conn:
        rows = conn.execute(sql, params).fetchall()
        articles = [NewsArticle(**row) for row in map(dict, rows)]
    return articles

# CRUD Feeds

@app.post("/feeds", response_model=NewsFeed)
def create_feed(feed: NewsFeed):
    keywords_json = json.dumps(feed.keywords)
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO newsfeeds (feed_name, keywords, feed_type, public_url, is_public, max_articles) VALUES (?, ?, ?, ?, ?, ?)",
            (feed.feed_name, keywords_json, feed.feed_type, feed.public_url, int(feed.is_public), feed.max_articles)
        )
        conn.commit()
        feed.id = cur.lastrowid
    return feed

@app.get("/feeds", response_model=List[NewsFeed])
def list_feeds():
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM newsfeeds").fetchall()
        feeds = []
        for row in rows:
            feed = dict(row)
            feed['keywords'] = json.loads(feed['keywords'])
            feed['is_public'] = bool(feed['is_public'])
            feeds.append(NewsFeed(**feed))
    return feeds

@app.delete("/feeds/{feed_id}", status_code=204)
def delete_feed(feed_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM newsfeeds WHERE id = ?", (feed_id,))
        conn.commit()
    return Response(status_code=204)

# Public feed

@app.get("/publicfeed/{feed_id}")
def get_public_feed(feed_id: int):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM newsfeeds WHERE id = ?", (feed_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Feed not found")
        feed = dict(row)
        feed['keywords'] = json.loads(feed['keywords'])
        if not feed['is_public']:
            raise HTTPException(status_code=403, detail="Feed is not public")

        articles_rows = conn.execute(
            f"SELECT * FROM newsarticles WHERE keyword IN ({','.join(['?']*len(feed['keywords']))}) ORDER BY published_date DESC LIMIT ?",
            (*feed['keywords'], feed['max_articles'])
        ).fetchall()
        articles = [dict(r) for r in articles_rows]

    if feed['feed_type'] == "json":
        return JSONResponse(content=articles, media_type="application/json")

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = feed["feed_name"]
    ET.SubElement(channel, "link").text = f"/publicfeed/{feed_id}"
    ET.SubElement(channel, "description").text = f"RSS feed for keywords: {', '.join(feed['keywords'])}"
    ET.SubElement(channel, "language").text = "pt-br"

    for art in articles:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = html.escape(art.get("title", ""))
        ET.SubElement(item, "link").text = art.get("url", "")
        ET.SubElement(item, "guid").text = stable_id(art.get("url", art.get("title", "")))
        ET.SubElement(item, "pubDate").text = art.get("published_date", "").replace("T", " ") if art.get("published_date") else ""
        ET.SubElement(item, "description").text = html.escape(art.get("summary") or art.get("content") or "")
        ET.SubElement(item, "source").text = art.get("source", "")
        ET.SubElement(item, "category").text = art.get("keyword", "")

    xml_str = ET.tostring(rss, encoding="utf-8")
    return Response(content=xml_str, media_type="application/rss+xml")

# Simulated collection (replace with your real logic)

@app.post("/collect/{keyword_id}")
def collect_news(keyword_id: int):
    with get_connection() as conn:
        cur = conn.execute("SELECT * FROM newskeywords WHERE id = ?", (keyword_id,))
        kw = cur.fetchone()
        if not kw:
            raise HTTPException(status_code=404, detail="Keyword not found")

        sample_articles = [
            {
                "title": f"Notícia sobre {kw['keyword']} em {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "content": f"Conteúdo gerado para a palavra-chave {kw['keyword']}.",
                "summary": f"Resumo da notícia para {kw['keyword']}.",
                "source": "Simulação",
                "url": f"http://example.com/news/{kw['keyword'].replace(' ', '_')}/{datetime.now().timestamp()}",
                "published_date": datetime.now(timezone.utc).isoformat(),
                "keyword": kw['keyword'],
                "relevance_score": 75,
                "category": "Geral"
            }
        ]

        for art in sample_articles:
            conn.execute(
                "INSERT INTO newsarticles (title, content, summary, source, url, published_date, keyword, relevance_score, category) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (art["title"], art["content"], art["summary"], art["source"], art["url"], art["published_date"], art["keyword"], art["relevance_score"], art["category"])
            )
        conn.execute("UPDATE newskeywords SET last_updated = ? WHERE id = ?", (datetime.now(timezone.utc), keyword_id))
        conn.commit()

    return {"status": "success", "collected_articles": len(sample_articles)}

@app.on_event("startup")
def startup():
    init_db()
