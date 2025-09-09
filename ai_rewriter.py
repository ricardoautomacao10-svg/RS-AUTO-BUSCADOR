import os
import httpx
import asyncio

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

async def rewrite_with_openrouter(title, paragraphs, source_name, source_url):
    if not OPENROUTER_API_KEY:
        return title, paragraphs

    prompt = f"Rewrite this news title and content to be unique:\nTitle: {title}\nContent:\n" + "\n".join(paragraphs)
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.8
    }

    async with httpx.AsyncClient(timeout=40) as client:
        response = await client.post("https://openrouter.ai/api/v1/chat/completions", headers=headers, json=data)
        response.raise_for_status()
        result = response.json()
        content = result["choices"][0]["message"]["content"]
        parts = content.split("\n\n", 1)
        new_title = parts[0] if parts else title
        new_paragraphs = parts[1].split("\n") if len(parts) > 1 else paragraphs
        return new_title.strip(), [p.strip() for p in new_paragraphs if p.strip()]
