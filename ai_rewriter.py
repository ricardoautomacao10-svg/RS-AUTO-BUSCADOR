# ai_rewriter.py — reescreve título e lead usando OpenRouter (se REWRITE_WITH_AI=1 e OPENROUTER_API_KEY definido)
# Instale: pip install httpx
import os, httpx, asyncio

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.1-8b-instruct:free")  # opções free mudam com o tempo

PROMPT_SYS = """Você é um editor de portal de notícias brasileiro. Reescreva título e organize 1–3 parágrafos iniciais, claros e objetivos.
- mantenha fatos, remova publicidade
- português BR
- NÃO invente
- foco em SEO local quando houver toponímia
- 200 a 300 palavras no total (mínimo 200)
Retorne JSON: {"title": "...", "paragraphs": ["...", "...", "..."]}"""

async def rewrite_with_openrouter(title: str, paragraphs: list, source_name: str, source_url: str):
    if not OPENROUTER_API_KEY:
        return title, paragraphs
    text = "\n\n".join(paragraphs[:5])
    user = f"""Fonte: {source_name} — {source_url}
Título original: {title}
Texto (trechos):
{text}
"""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MODEL,
                    "messages": [
                        {"role":"system","content":PROMPT_SYS},
                        {"role":"user","content":user},
                    ],
                    "temperature": 0.3,
                    "response_format": {"type":"json_object"},
                }
            )
            r.raise_for_status()
            js = r.json()
            out = js["choices"][0]["message"]["content"]
    except Exception:
        return title, paragraphs
    # parse simples
    import json
    try:
        data = json.loads(out)
        new_title = data.get("title") or title
        new_pars = [p for p in data.get("paragraphs", []) if isinstance(p,str) and p.strip()]
        # garante mínimo de 200 palavras
        wc = sum(len(p.split()) for p in new_pars)
        if wc < 200:
            # complementa com parágrafos originais
            for p in paragraphs:
                if wc >= 200: break
                new_pars.append(p)
                wc += len(p.split())
        return new_title, new_pars[:10]
    except Exception:
        return title, paragraphs
