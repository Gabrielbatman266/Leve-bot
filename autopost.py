"""Autopost: fila de fotos e vídeos que o bot posta sozinho no grupo/canal VIP.

Como alimentar a fila:
  - mande foto ou vídeo (com legenda, se quiser) no privado do bot, sendo o dono; ou
  - envie pelo painel, na aba "Postagem".

Comandos do dono no privado do bot:
  /fila  /postar  /intervalo N  /pausar  /retomar  /reciclar
"""

import logging
import random

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from db import admin_id, db, get_int, get_setting, now, set_setting

log = logging.getLogger("levevip.autopost")

db.executescript(
    """
    CREATE TABLE IF NOT EXISTS posts (
        id         INTEGER PRIMARY KEY,
        kind       TEXT    NOT NULL,          -- photo | video
        file_id    TEXT    NOT NULL,
        thumb_id   TEXT,                      -- miniatura (para o painel)
        caption    TEXT    NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        posted_at  INTEGER,                   -- NULL = ainda na fila
        times      INTEGER NOT NULL DEFAULT 0 -- quantas vezes já saiu
    );
    """
)
db.commit()

# Padrões (editáveis no painel)
DEFAULTS = {
    "ap_on": "1",          # postar sozinho
    "ap_group": "",        # grupo/canal de destino ("" = grupo do primeiro plano)
    "ap_minutes": "60",    # intervalo
    "ap_recycle": "1",     # quando a fila acabar, repostar o que já saiu
    "ap_last": "0",        # quando foi o último post (timestamp)
    "ap_last_id": "0",     # id do último post (para não repetir em seguida)
}


def setting(key: str) -> str:
    v = get_setting(key)
    return v if v != "" else DEFAULTS.get(key, "")


def target_group() -> int:
    g = setting("ap_group")
    if g:
        try:
            return int(g)
        except ValueError:
            pass
    row = db.execute("SELECT group_id FROM plans WHERE active=1 ORDER BY sort, id LIMIT 1").fetchone()
    return row["group_id"] if row else 0


def counts() -> dict:
    q = db.execute("SELECT COUNT(*) FROM posts WHERE posted_at IS NULL").fetchone()[0]
    done = db.execute("SELECT COUNT(*) FROM posts WHERE posted_at IS NOT NULL").fetchone()[0]
    return {"fila": q, "postados": done}


def add(kind: str, file_id: str, caption: str = "", thumb_id: str | None = None) -> int:
    cur = db.execute(
        "INSERT INTO posts (kind, file_id, thumb_id, caption, created_at) VALUES (?, ?, ?, ?, ?)",
        (kind, file_id, thumb_id, caption or "", now()),
    )
    db.commit()
    return cur.lastrowid


def next_post():
    row = db.execute("SELECT * FROM posts WHERE posted_at IS NULL ORDER BY id LIMIT 1").fetchone()
    if row or setting("ap_recycle") != "1":
        return row
    last = int(setting("ap_last_id") or 0)
    rows = db.execute("SELECT * FROM posts WHERE posted_at IS NOT NULL AND id != ?", (last,)).fetchall()
    if not rows:
        rows = db.execute("SELECT * FROM posts WHERE posted_at IS NOT NULL").fetchall()
    return random.choice(rows) if rows else None


async def post_now(bot) -> str:
    group = target_group()
    if not group:
        return "Nenhum grupo de destino. Crie um plano ou escolha o grupo no painel."
    row = next_post()
    if row is None:
        return "Fila vazia."
    cap = row["caption"] or None
    if row["kind"] == "photo":
        await bot.send_photo(group, row["file_id"], caption=cap)
    else:
        await bot.send_video(group, row["file_id"], caption=cap, supports_streaming=True)
    db.execute("UPDATE posts SET posted_at=?, times=times+1 WHERE id=?", (now(), row["id"]))
    db.commit()
    set_setting("ap_last", str(now()))
    set_setting("ap_last_id", str(row["id"]))
    return f"Postado. Restam {counts()['fila']} na fila."


# ───────────────────────── Job ─────────────────────────
_warned_empty = False


async def job_autopost(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Roda a cada minuto; posta quando o intervalo configurado passou."""
    global _warned_empty
    if setting("ap_on") != "1":
        return
    minutes = max(1, get_int("ap_minutes", 60) or 60)
    if now() - int(setting("ap_last") or 0) < minutes * 60:
        return
    try:
        result = await post_now(context.bot)
    except TelegramError as e:
        log.warning("autopost falhou: %s", e)
        set_setting("ap_last", str(now()))  # tenta de novo só no próximo intervalo
        if admin_id():
            try:
                await context.bot.send_message(admin_id(), f"⚠️ Autopost falhou: {e}")
            except TelegramError:
                pass
        return
    if result == "Fila vazia.":
        if not _warned_empty and admin_id():
            _warned_empty = True
            try:
                await context.bot.send_message(admin_id(), "⚠️ A fila de postagem acabou. Mande mais fotos/vídeos.")
            except TelegramError:
                pass
    else:
        _warned_empty = False


# ───────────────────────── Telegram (dono) ─────────────────────────
def is_owner(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id == admin_id()


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    m = update.message
    if m.photo:
        add("photo", m.photo[-1].file_id, m.caption or "", m.photo[0].file_id)
    elif m.video:
        add("video", m.video.file_id, m.caption or "", m.video.thumbnail.file_id if m.video.thumbnail else None)
    else:
        return
    await m.reply_text(f"✅ Na fila ({counts()['fila']} esperando).")


async def cmd_fila(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    c = counts()
    await update.message.reply_text(
        f"Fila: {c['fila']} · já postados: {c['postados']}\n"
        f"Intervalo: {setting('ap_minutes')} min · {'ativo ▶️' if setting('ap_on') == '1' else 'pausado ⏸'}\n"
        f"Reciclar: {'ligado' if setting('ap_recycle') == '1' else 'desligado'}"
    )


async def cmd_postar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_owner(update):
        try:
            await update.message.reply_text(await post_now(context.bot))
        except TelegramError as e:
            await update.message.reply_text(f"Erro: {e}")


async def cmd_intervalo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    try:
        n = max(1, int(context.args[0]))
    except (IndexError, ValueError):
        await update.message.reply_text("Use: /intervalo 45")
        return
    set_setting("ap_minutes", str(n))
    await update.message.reply_text(f"Intervalo: {n} min.")


async def cmd_pausar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_owner(update):
        set_setting("ap_on", "0")
        await update.message.reply_text("⏸ Postagem pausada.")


async def cmd_retomar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_owner(update):
        set_setting("ap_on", "1")
        await update.message.reply_text("▶️ Postagem ligada.")


async def cmd_reciclar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_owner(update):
        new = "0" if setting("ap_recycle") == "1" else "1"
        set_setting("ap_recycle", new)
        await update.message.reply_text(f"Reciclar {'ligado' if new == '1' else 'desligado'}.")


def register(app: Application) -> None:
    private = filters.ChatType.PRIVATE
    app.add_handler(MessageHandler(private & (filters.PHOTO | filters.VIDEO), on_media))
    for name, fn in (("fila", cmd_fila), ("postar", cmd_postar), ("intervalo", cmd_intervalo),
                     ("pausar", cmd_pausar), ("retomar", cmd_retomar), ("reciclar", cmd_reciclar)):
        app.add_handler(CommandHandler(name, fn, private))
    app.job_queue.run_repeating(job_autopost, interval=60, first=40)
