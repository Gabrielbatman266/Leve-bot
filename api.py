"""API para o Post Leve conversar com o Leve VIP (mesmo celular: http://127.0.0.1:8080).

Autenticação: cabeçalho  X-Leve-Key: <chave>  (a chave aparece no painel, aba Post Leve).

GET  /api/v1/info                 → bot, planos, assinantes ativos, vendas de hoje
GET  /api/v1/eventos?desde=<ms>   → leads e vendas com a origem (o código do link rastreado)
POST /api/v1/origens              → {"origem": "th_x1y2", "titulo": "Threads · legenda…"} dá nome a um link
POST /api/v1/vip/postar           → multipart: foto (arquivo), legenda (opcional) → posta nos grupos VIP
"""

import hmac
import logging
import re
import secrets
from datetime import datetime

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from telegram.error import TelegramError

from db import db, get_setting, now, set_setting

log = logging.getLogger("levevip.api")

SOURCE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def api_key() -> str:
    k = get_setting("api_key")
    if not k:
        k = secrets.token_urlsafe(24)
        set_setting("api_key", k)
    return k


def new_api_key() -> str:
    k = secrets.token_urlsafe(24)
    set_setting("api_key", k)
    return k


def _auth(request: Request) -> bool:
    ok = hmac.compare_digest(request.headers.get("x-leve-key", ""), api_key())
    if ok:
        set_setting("postleve_seen", str(now()))
    return ok


def _deny() -> JSONResponse:
    return JSONResponse({"ok": False, "erro": "chave inválida"}, status_code=401)


def label_of(source: str | None) -> str | None:
    if not source:
        return None
    row = db.execute("SELECT label FROM source_labels WHERE source=?", (source,)).fetchone()
    return row["label"] if row else None


def vip_groups() -> list[int]:
    """Grupos dos planos ativos (onde o conteúdo VIP é postado)."""
    return [r[0] for r in db.execute("SELECT DISTINCT group_id FROM plans WHERE active=1").fetchall()]


# ───────────────────────── Rotas ─────────────────────────
async def info(request: Request):
    if not _auth(request):
        return _deny()
    t = now()
    midnight = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    today = db.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments WHERE status='paid' AND currency='brl' AND paid_at>=?",
                       (midnight,)).fetchone()
    active = db.execute("SELECT COUNT(DISTINCT user_id) FROM members WHERE removed=0 AND (expires_at IS NULL OR expires_at>?)",
                        (t,)).fetchone()[0]
    plans = [{"id": p["id"], "nome": p["name"], "preco": p["price_brl"], "dias": p["days"]}
             for p in db.execute("SELECT * FROM plans WHERE active=1 ORDER BY sort, id").fetchall()]
    return JSONResponse({
        "ok": True,
        "bot": request.app.state.tg.bot.username,
        "ativos": active,
        "hoje": {"vendas": today[0], "valor": today[1]},
        "planos": plans,
        "grupos": len(vip_groups()),
    })


async def events(request: Request):
    if not _auth(request):
        return _deny()
    try:
        since_ms = int(request.query_params.get("desde", "0"))
    except ValueError:
        since_ms = 0
    since = since_ms // 1000
    out = []
    for r in db.execute("SELECT source, created_at FROM leads WHERE source IS NOT NULL AND created_at>? ORDER BY created_at LIMIT 1000",
                        (since,)).fetchall():
        out.append({"tipo": "lead", "origem": r["source"], "at": r["created_at"] * 1000})
    for r in db.execute(
        """SELECT l.source, p.paid_at, p.amount, p.currency, p.kind, pl.name FROM payments p
           JOIN leads l ON l.user_id=p.user_id LEFT JOIN plans pl ON pl.id=p.plan_id
           WHERE p.status='paid' AND p.paid_at>? AND l.source IS NOT NULL ORDER BY p.paid_at LIMIT 1000""", (since,)
    ).fetchall():
        out.append({"tipo": "venda", "origem": r["source"], "at": r["paid_at"] * 1000, "valor": r["amount"],
                    "moeda": r["currency"], "tipo_venda": r["kind"], "plano": r["name"]})
    out.sort(key=lambda e: e["at"])
    return JSONResponse({"ok": True, "agora": now() * 1000, "eventos": out})


async def name_source(request: Request):
    if not _auth(request):
        return _deny()
    try:
        body = await request.json()
        source, title = str(body["origem"]), str(body.get("titulo", ""))[:120]
    except Exception:
        return JSONResponse({"ok": False, "erro": "json inválido"}, status_code=400)
    if not SOURCE_RE.match(source):
        return JSONResponse({"ok": False, "erro": "origem inválida"}, status_code=400)
    db.execute("INSERT INTO source_labels (source, label) VALUES (?, ?) ON CONFLICT(source) DO UPDATE SET label=excluded.label",
               (source, title))
    db.commit()
    return JSONResponse({"ok": True})


async def post_to_vip(request: Request):
    if not _auth(request):
        return _deny()
    form = await request.form()
    photo = form.get("foto")
    if photo is None or not hasattr(photo, "read"):
        return JSONResponse({"ok": False, "erro": "mande a foto no campo 'foto'"}, status_code=400)
    data = await photo.read()
    if not data or len(data) > 10 * 1024 * 1024:
        return JSONResponse({"ok": False, "erro": "foto vazia ou maior que 10 MB"}, status_code=400)
    caption = (form.get("legenda") or "")[:1000] or None
    groups = vip_groups()
    if not groups:
        return JSONResponse({"ok": False, "erro": "nenhum plano ativo com grupo"}, status_code=409)

    bot = request.app.state.tg.bot
    sent, errors, file_id = 0, [], None
    for g in groups:
        try:
            msg = await bot.send_photo(g, photo=file_id or data, caption=caption)
            file_id = file_id or msg.photo[-1].file_id  # reaproveita o upload nos outros grupos
            sent += 1
        except TelegramError as e:
            errors.append(f"{g}: {e}")
            log.warning("Falha ao postar no VIP %s: %s", g, e)
    if sent:
        set_setting("postleve_vip_posts", str(int(get_setting("postleve_vip_posts") or 0) + sent))
    return JSONResponse({"ok": sent > 0, "enviados": sent, "erros": errors})


ROUTES = [
    Route("/api/v1/info", info),
    Route("/api/v1/eventos", events),
    Route("/api/v1/origens", name_source, methods=["POST"]),
    Route("/api/v1/vip/postar", post_to_vip, methods=["POST"]),
]
