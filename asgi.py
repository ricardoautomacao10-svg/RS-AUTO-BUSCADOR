# asgi.py — inicializador robusto para o Render
try:
    from news_automation import app as app  # se o módulo já expõe "app"
except Exception:
    from news_automation import create_app   # fallback
    app = create_app()
