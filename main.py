"""Leve VIP — bot de vendas VIP no Telegram + painel web, num processo só.

Rode:  python main.py
Painel: http://localhost:8080 (ou o PANEL_PORT do .env)
"""

import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()  # antes de importar o resto (eles leem o .env)

import uvicorn  # noqa: E402
from telegram import Update  # noqa: E402
from telegram.error import InvalidToken  # noqa: E402

import core  # noqa: E402
import db  # noqa: E402
import panel  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
for noisy in ("httpx", "telegram.ext", "uvicorn.access", "apscheduler"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("levevip")


async def main() -> None:
    missing = [k for k in ("BOT_TOKEN", "SYNCPAY_CLIENT_ID", "SYNCPAY_CLIENT_SECRET", "PANEL_PASSWORD") if not os.environ.get(k)]
    if missing:
        print(f"Faltam no .env: {', '.join(missing)} — rode: levebot configurar")
        raise SystemExit(3)

    db.seed_from_env()
    if not db.admin_id():
        log.info("Mande /admin SUA_SENHA para o bot no Telegram para virar o dono.")
    tg = core.build_application()
    host = os.environ.get("PANEL_HOST", "127.0.0.1")
    port = int(os.environ.get("PANEL_PORT", "8080"))
    server = uvicorn.Server(uvicorn.Config(panel.create_app(tg), host=host, port=port, log_level="warning"))

    try:
        await tg.initialize()
    except InvalidToken:
        print("Token do bot recusado pelo Telegram — rode: levebot configurar")
        raise SystemExit(3)
    async with tg:
        await tg.start()
        await tg.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info("Bot @%s rodando. Painel em http://%s:%s", tg.bot.username,
                 "localhost" if host in ("127.0.0.1", "0.0.0.0") else host, port)
        try:
            await server.serve()  # roda até Ctrl+C
        finally:
            await tg.updater.stop()
            await tg.stop()


if __name__ == "__main__":
    asyncio.run(main())
