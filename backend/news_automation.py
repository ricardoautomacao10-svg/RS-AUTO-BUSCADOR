from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import feedparser
import urllib.parse
from pathlib import Path
from typing import List, Dict

app = FastAPI()

# Ajuste 'static_dir' para o seu caminho da pasta estática com index.html
static_dir = Path(__file__).parent / "static"

# Monta a pasta estática para servir HTML, CSS, JS, imagens
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def root():
    # Serve o arquivo index.html da pasta estática na rota raiz
    return FileResponse(static_dir / "index.html")

class NewsItem(BaseModel):
    title: str
    link: str
    published: str
    summary: str
    rss_url: str

@app.get("/news", response_model=List[NewsItem])
def get_news(q: str = "brasil"):
    q_encoded = urllib.parse.quote(q)
    rss_url = f"https://news.google.com/rss/search?q={q_encoded}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
    feed = feedparser.parse(rss_url)
    articles = []
    for entry in feed.entries[:10]:
        articles.append(NewsItem(
            title=entry.title,
            link=entry.link,
            published=entry.get("published", ""),
            summary=entry.get("summary", ""),
            rss_url=rss_url
        ))
    return articles
