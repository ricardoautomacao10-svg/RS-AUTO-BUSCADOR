# news_automation.py
# Automação leve de notícias:
# - Busca por palavras-chave (Google News RSS) nas últimas X horas
# - Extrai H1, imagem principal e parágrafos "inteiros" (limpos)
# - Remove "leia mais/também", publicidade e CTAs
# - Gera permalink fixo /item/{id} e lista por /q/{slug}
# - Endpoint /add para ingerir 1 link específico
#
# Requisitos (requirements.txt):
# fastapi
# uvicorn[standard]
# httpx
# feedparser
# beautifulsoup4
# trafilatura
# python-slugify

import os
import re
import json
import base64
import hashlib
import sqlite3
import asyncio
import feedparser

from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

# ===== Configuração de banco para ambientes como Render (persistência em /data) =====
DB_PATH = os.getenv("DB_PATH", "/data/news.db")

# slugify opcional; se não houver, fallback simples
try:
    from slugify import slugify
except Exception:  # pragma: no cover
    def slugify(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"\s+", "-", s)
        s = re.sub(r"[^a-z0-9\-]+", "", s)
        return s

# trafilatura é opcional (melhora extração de texto)
try:
    import trafilatura  # type: ignore
except Exception:  # pragma: no cover
    trafilatura = None  # seguirá apenas com BeautifulSoup


# ========================== Utilidades ==========================

APP_TITLE = "News Automation"

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()

def from_pubdate_struct(tm: Any) -> Optional[datetime]:
    if not tm:
        return None
    try:
        return datetime(*tm[:6], tzinfo=timezone.utc)
    except Exception:
        return None

def stable_id(url: str) -> str:
    """ID curto, estável, baseado no URL (bom para permalink)."""
    h = hashlib.sha256(url.encode("utf-8")).digest()[:9]
    return base64.urlsafe_b64encode(h).decode("ascii").rstrip("=")

def hostname_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc
        return re.sub(r"^www\.", "", host)
    except Exception:
        return "fonte"

BAD_SNIPPETS = [
    "leia mais", "leia também", "publicidade", "anúncio",
    "assine", "assinar", "clique aqui", "veja também",
    "continue lendo", "continue a ler", "compartilhe",
    "siga-nos", "newsletter", "inscreva-se", "oferta"
]

def clean_paragraph(p: str) -> Optional[str]:
    txt = re.sub(r"\s+", " ", p or "").strip()
    if not txt:
        return None
    low = txt.lower()
    if any(b in low for b in BAD_SNIPPETS):
        return None
    # descarta muito curtos (breadcrumbs/legendas/CTAs)
    if len(txt) < 25:
        return None
