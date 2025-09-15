from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import httpx
from bs4 import BeautifulSoup
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
    image_url: str
    generated_text: str

async def fetch_news_for_keyword(keyword: str) -> List[NewsItem]:
    url = f"https://news.google.com/search?q={keyword}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
    soup = BeautifulSoup(response.text, "html.parser")
    articles = soup.select("article")[:3]

    results = []
    for article in articles:
        title_tag = article.find("h3")
        img_tag = article.find("img")
        link_tag = article.find("a", href=True)
        if not (title_tag and link_tag):
            continue
        title = title_tag.get_text()
        image_url = img_tag['src'] if img_tag else ""
        link = link_tag['href']
        text_raw = await scrape_article_text(link)
        generated_text = await generate_text_via_ia(text_raw)
        results.append(NewsItem(title=title, image_url=image_url, generated_text=generated_text).dict())
    return results

async def scrape_article_text(url: str) -> str:
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
    soup = BeautifulSoup(r.text, "html.parser")
    paragraphs = soup.find_all("p")
    text = " ".join(p.get_text() for p in paragraphs[:5])
    return text

async def generate_text_via_ia(text: str) -> str:
    return f"Texto único gerado pela IA para: {text[:200]}..."

async def collect_news():
    global news_storage
    news_storage.clear()
    for kw in keywords:
        news = await fetch_news_for_keyword(kw)
        news_storage.extend(news)

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
