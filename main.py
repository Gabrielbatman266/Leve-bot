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
from telegram.error import InvalidToken, NetworkError  # noqa: E402

import core  # noqa: E402
import db  # noqa: E402
import panel  # noqa: E402
import remote_config  # noqa: E402

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
for noisy in ("httpx", "telegram.ext", "uvicorn.access", "apscheduler"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("levevip")


def lan_ip() -> str:
    """IP deste aparelho na rede local (para abrir o painel de outro celular)."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "IP-DO-APARELHO"


async def main() -> None:
    missing = [k for k in ("BOT_TOKEN", "SYNCPAY_CLIENT_ID", "SYNCPAY_CLIENT_SECRET", "PANEL_PASSWORD") if not os.environ.get(k)]
    if missing:
        print(f"Faltam no .env: {', '.join(missing)} — rode: levebot configurar")
        raise SystemExit(3)

    db.seed_from_env()
    remote_config.apply()  # config_remota.json do GitHub
    if not db.admin_id():
        log.info("Mande /admin SUA_SENHA para o bot no Telegram para virar o dono.")
    tg = core.build_application()
    host = os.environ.get("PANEL_HOST", "127.0.0.1")
    port = int(os.environ.get("PANEL_PORT", "8080"))
    server = uvicorn.Server(uvicorn.Config(panel.create_app(tg), host=host, port=port, log_level="warning"))

    wait = 5
    while True:  # sem internet ou Telegram lento: tenta de novo em vez de cair
        try:
            await tg.initialize()
            break
        except InvalidToken:
            print("Token do bot recusado pelo Telegram — rode: levebot configurar")
            raise SystemExit(3)
        except NetworkError as e:
            log.warning("Sem conexão com o Telegram (%s). Tentando de novo em %s s…", e, wait)
            await asyncio.sleep(wait)
            wait = min(wait * 2, 60)
    async with tg:
        await tg.start()
        await tg.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info("Bot @%s rodando. Painel em http://%s:%s", tg.bot.username,
                 "localhost" if host in ("127.0.0.1", "0.0.0.0") else host, port)
        if host == "0.0.0.0":
            log.info("Painel no Wi-Fi: http://%s:%s", lan_ip(), port)
        try:
            await server.serve()  # roda até Ctrl+C
        finally:
            await tg.updater.stop()
            await tg.stop()


if __name__ == "__main__":
    asyncio.run(main())
