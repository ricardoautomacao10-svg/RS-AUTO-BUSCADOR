# asgi.py — inicializador robusto para o Render
try:
    # se o módulo já expõe "app", usa direto
    from news_automation import app as app  # noqa: F401
except Exception:
    # fallback: cria a app chamando create_app()
    from news_automation import create_app  # type: ignore
    app = create_app()
