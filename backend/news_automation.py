from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import feedparser
import asyncio
from pathlib import Path
from typing import List, Dict
import uvicorn
import os

app = FastAPI()

base_dir = Path(__file__).resolve().parent.parent
static_dir = base_dir / "static"

app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def serve_panel():
    return FileResponse(static_dir / "index.html")


news_storage: List[Dict] = []
keywords = ["exemplo", "notícia", "tecnologia"]

class NewsItem(BaseModel):
    title: str
    link: str
    published: str
    summary: str

def fetch_news_from_rss(keyword: str) -> List[NewsItem]:
    rss_url = f"https://news.google.com/rss/search?q={keyword}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    feed = feedparser.parse(rss_url)
    articles = []
    for entry in feed.entries[:10]:
        articles.append(NewsItem(
            title=entry.title,
            link=entry.link,
            published=entry.get("published", ""),
            summary=entry.get("summary", "")
        ).dict())
    return articles

async def collect_news():
    global news_storage
    news_storage.clear()
    for kw in keywords:
        articles = fetch_news_from_rss(kw)
        news_storage.extend(articles)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(schedule_periodic_collect())

async def schedule_periodic_collect():
    while True:
        await collect_news()
        await asyncio.sleep(600)  # 10 minutos

@app.get("/news", response_model=List[NewsItem])
async def get_news():
    return news_storage

@app.post("/keywords")
async def update_keywords(new_keywords: List[str]):
    global keywords
    keywords = new_keywords
    await collect_news()
    return {"message": "Palavras-chave atualizadas e notícias coletadas."}

if __name__ == "__main__":
    uvicorn.run("news_automation:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
