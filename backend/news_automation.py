from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import feedparser
import urllib.parse

app = FastAPI()

# Permitir acesso frontend (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Ajuste para o domínio do seu frontend em produção
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class NewsItem(BaseModel):
    title: str
    link: str
    published: str
    summary: str
    rss_url: str

@app.get("/news", response_model=list[NewsItem])
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
