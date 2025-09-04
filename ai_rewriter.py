# ai_rewriter.py — reescrita via OpenRouter (modelos :free)
import os, re, httpx

OPENROUTER_API_BASE = os.getenv("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen2.5-7b-instruct:free")

async def rewrite_with_openrouter(title, paragraphs, source_name, source_url):
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        return title, paragraphs

    sys = ("Você é editor de um portal brasileiro. Reescreva títulos e textos com "
           "clareza, concisão e neutralidade, preservando estritamente os fatos.")

    user = f"""
Reescreva para português do Brasil, mantendo fatos e sem opinião.

Título atual:
{title}

Texto (parágrafos):
{chr(10).join(paragraphs)}

Regras:
- Título novo: informativo e conciso (≤ 78 caracteres), sem clickbait.
- Corpo: 2 a 4 parágrafos coesos; sem 'leia mais', CTA, opinião ou 1ª pessoa.
- Não invente nada. Não use emojis. Não repita o título no corpo.
"""

    payload = {
        "model": DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": sys},
            {"role": "user", "content": user}
        ],
        "temperature": 0.2
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "HTTP-Referer": os.getenv("OPENROUTER_HTTP_REFERER", "https://seu-dominio"),
        "X-Title": os.getenv("OPENROUTER_X_TITLE", "NewsAutomation"),
    }

    async with httpx.AsyncClient(base_url=OPENROUTER_API_BASE, timeout=25.0) as client:
        r = await client.post("/chat/completions", json=payload, headers=headers)
        r.raise_for_status()
        js = r.json()
        content = (js.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()

    # Parse simples: 1ª linha = título; demais = corpo
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    new_title = title
    new_pars = paragraphs
    if lines:
        if lines[0].lower().startswith("título"):
            new_title = lines[0].split(":", 1)[1].strip() or title
            body = "\n".join(lines[1:])
        else:
            new_title = lines[0][:120]
            body = "\n".join(lines[1:]) or "\n".join(lines)
        parts = [p.strip() for p in re.split(r"\n{2,}", body) if p.strip()]
        if not parts:
            parts = [body]
        new_pars = parts[:4]
    return new_title, new_pars
