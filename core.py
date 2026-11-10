"""Bot do Telegram: venda de planos, liberação de acesso, remarketing, upsell/downsell e expiração."""

import asyncio
import html
import logging
import os
from datetime import datetime

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Kb
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes

import gateways as gw
from db import admin_id, db, get_int, get_setting, now, seed_from_env, set_setting

log = logging.getLogger("levevip")

CHECK_EVERY = 30  # segundos entre consultas de pagamento

LABELS = {"brl": "R$", "usd": "US$", "eur": "€"}
FLAGS = {"brl": "🇧🇷", "usd": "🇺🇸", "eur": "🇪🇺"}
KIND_NAMES = {"n": "normal", "d": "downsell", "u": "upsell"}


# ───────────────────────── Utilidades ─────────────────────────
def esc(s) -> str:
    return html.escape("" if s is None else str(s))


def money(currency: str, cents: int) -> str:
    v = f"{cents / 100:,.2f}"
    if currency in ("brl", "eur"):
        v = v.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{LABELS.get(currency, currency.upper())} {v}"


def fmt_date(ts: int | None) -> str:
    return "vitalício" if ts is None else datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M")


def plan_duration(days: int) -> str:
    return "vitalício" if days == 0 else f"{days} dias"


def get_plan(plan_id: int):
    return db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()


def plan_price(plan, currency: str, kind: str) -> int | None:
    base = plan[f"price_{currency}"]
    if not base:
        return None
    pct = 0
    if kind == "downsell":
        pct = get_int("downsell_percent", 0)
    elif kind == "upsell":
        pct = get_int("upsell_percent", 0)
    return max(100 if currency == "brl" else 50, round(base * (100 - pct) / 100))  # mínimo R$ 1 / 0,50


async def safe_send(bot, user_id: int, text: str, **kw) -> bool:
    """Envia mensagem sem derrubar nada se a pessoa bloqueou o bot."""
    try:
        await bot.send_message(user_id, text, **kw)
        return True
    except Forbidden:
        db.execute("UPDATE leads SET blocked=1 WHERE user_id=?", (user_id,))
        db.commit()
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        return await safe_send(bot, user_id, text, **kw)
    except TelegramError as e:
        log.warning("Falha ao enviar para %s: %s", user_id, e)
    return False


# ───────────────────────── Acesso ─────────────────────────
async def grant(bot, user_id: int, plan) -> int | None:
    """Libera/renova o acesso ao grupo do plano. Retorna o novo vencimento (None = vitalício)."""
    group_id = plan["group_id"]
    row = db.execute("SELECT expires_at, removed FROM members WHERE user_id=? AND group_id=?",
                     (user_id, group_id)).fetchone()

    if plan["days"] == 0 or (row and row["expires_at"] is None and not row["removed"]):
        new_exp = None  # vitalício (ou já era)
    else:
        current = row["expires_at"] if row and not row["removed"] and row["expires_at"] else 0
        new_exp = max(now(), current) + plan["days"] * 86400

    db.execute(
        """INSERT INTO members (user_id, group_id, plan_id, expires_at, reminded, removed)
           VALUES (?, ?, ?, ?, 0, 0)
           ON CONFLICT(user_id, group_id) DO UPDATE SET
             plan_id=excluded.plan_id, expires_at=excluded.expires_at, reminded=0, removed=0""",
        (user_id, group_id, plan["id"], new_exp),
    )
    db.commit()

    already_in = False
    try:
        m = await bot.get_chat_member(group_id, user_id)
        already_in = m.status in ("member", "administrator", "creator", "restricted")
    except TelegramError:
        pass

    validity = "Acesso <b>vitalício</b> 🏆" if new_exp is None else f"Acesso válido até <b>{fmt_date(new_exp)}</b>."
    paid_text = esc(get_setting("paid_text"))

    if already_in:
        await safe_send(bot, user_id, f"{paid_text}\n\n{validity}", parse_mode=ParseMode.HTML)
        return new_exp

    try:
        await bot.unban_chat_member(group_id, user_id, only_if_banned=True)
    except TelegramError:
        pass
    try:
        link = await bot.create_chat_invite_link(
            group_id, member_limit=1, expire_date=now() + 86400, name=f"VIP {user_id}"[:32]
        )
    except TelegramError as e:
        log.error("Não consegui criar convite para o grupo %s: %s", group_id, e)
        await safe_send(bot, user_id, f"{paid_text}\n\n{validity}\n\nO admin vai te enviar o link em instantes.",
                        parse_mode=ParseMode.HTML)
        if admin_id():
            await safe_send(bot, admin_id(), f"⚠️ Não consegui gerar convite para {user_id} no grupo {group_id}: {e}")
        return new_exp

    await safe_send(
        bot, user_id,
        f"{paid_text}\n\n{validity}\nEntre pelo botão abaixo (link pessoal, válido por 24 h):",
        parse_mode=ParseMode.HTML,
        reply_markup=Kb([[Btn("🔓 Entrar no VIP", url=link.invite_link)]]),
    )
    return new_exp


async def remove_member(bot, user_id: int, group_id: int) -> None:
    try:
        await bot.ban_chat_member(group_id, user_id)
        await bot.unban_chat_member(group_id, user_id, only_if_banned=True)  # tira sem banir de vez
    except TelegramError as e:
        log.warning("Não consegui remover %s de %s: %s", user_id, group_id, e)
    db.execute("UPDATE members SET removed=1 WHERE user_id=? AND group_id=?", (user_id, group_id))
    db.commit()


# ───────────────────────── Pagamentos ─────────────────────────
async def process_payment(bot, pay_id: int) -> str:
    """Consulta o gateway e, se pago, libera o acesso (uma vez só). Retorna paid | pending | failed."""
    p = db.execute("SELECT * FROM payments WHERE id=?", (pay_id,)).fetchone()
    if not p:
        return "failed"
    if p["status"] == "paid":
        return "paid"

    st = await gw.charge_status(p["provider"], p["ext_id"])
    if st == "failed":
        db.execute("UPDATE payments SET status='expired' WHERE id=? AND status='pending'", (pay_id,))
        db.commit()
        return "failed"
    if st != "paid":
        return "pending"

    cur = db.execute("UPDATE payments SET status='paid', paid_at=? WHERE id=? AND status IN ('pending','expired')",
                     (now(), pay_id))
    db.commit()
    if not cur.rowcount:
        return "paid"  # outro processo já liberou

    plan = get_plan(p["plan_id"])
    log.info("Venda: user=%s plano=%s %s", p["user_id"], p["plan_id"], money(p["currency"], p["amount"]))
    if plan:
        await grant(bot, p["user_id"], plan)

    if admin_id():
        lead = db.execute("SELECT username, first_name, source FROM leads WHERE user_id=?", (p["user_id"],)).fetchone()
        who = f"@{lead['username']}" if lead and lead["username"] else (lead["first_name"] if lead else p["user_id"])
        src = lead["source"] if lead and lead["source"] else "direto"
        tag = "" if p["kind"] == "normal" else f" ({p['kind']})"
        await safe_send(bot, admin_id(),
                        f"💰 Venda{tag}: {plan['name'] if plan else '?'} — {money(p['currency'], p['amount'])}\n"
                        f"👤 {who} · origem: {src}")

    if p["kind"] == "normal":
        await offer_upsell(bot, p["user_id"], p["plan_id"])
    return "paid"


async def offer_upsell(bot, user_id: int, bought_plan_id: int) -> None:
    if get_setting("upsell_on") != "1":
        return
    try:
        up = get_plan(int(get_setting("upsell_plan_id")))
    except ValueError:
        return
    if not up or not up["active"] or up["id"] == bought_plan_id:
        return
    price = plan_price(up, "brl", "upsell")
    full = up["price_brl"]
    await safe_send(
        bot, user_id,
        f"{esc(get_setting('upsell_text'))}\n\n<b>{esc(up['name'])}</b> ({plan_duration(up['days'])})\n"
        f"<s>{money('brl', full)}</s> por <b>{money('brl', price)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=Kb([[Btn(f"🔥 Quero por {money('brl', price)}", callback_data=f"b:{up['id']}:brl:u")]]),
    )


def offer_allowed(user_id: int, plan_id: int, kind: str) -> bool:
    """Confere se o desconto foi mesmo oferecido a essa pessoa (evita uso indevido)."""
    day_ago = now() - 86400 * 2
    if kind == "downsell":
        return bool(db.execute(
            "SELECT 1 FROM payments WHERE user_id=? AND plan_id=? AND followup=2 AND created_at>?",
            (user_id, plan_id, day_ago)).fetchone())
    if kind == "upsell":
        return (get_setting("upsell_on") == "1" and get_setting("upsell_plan_id") == str(plan_id)
                and bool(db.execute("SELECT 1 FROM payments WHERE user_id=? AND status='paid' AND paid_at>?",
                                    (user_id, day_ago)).fetchone()))
    return True


async def create_payment(bot, chat_message, user_id: int, plan, currency: str, kind: str) -> None:
    price = plan_price(plan, currency, kind)
    if price is None:
        await chat_message.reply_text("Esse plano não aceita essa moeda.")
        return
    desc = f"{plan['name']} - {plan_duration(plan['days'])}"
    try:
        provider, ext_id, payload = await gw.create_charge(
            currency, price, desc, user_id, return_url=f"https://t.me/{bot.username}")
    except Exception as e:
        log.error("Erro ao gerar cobrança (%s): %s", currency, e)
        await chat_message.reply_text("❌ Não consegui gerar o pagamento agora. Tente de novo em instantes.")
        return

    pay_id = db.execute(
        """INSERT INTO payments (ext_id, provider, user_id, plan_id, currency, amount, kind, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (ext_id, provider, user_id, plan["id"], currency, price, kind, now()),
    ).lastrowid
    db.commit()

    check_btn = Btn("✅ Já paguei", callback_data=f"c:{pay_id}")
    if provider == "syncpay":
        await chat_message.reply_photo(
            gw.qr_png(payload),
            caption=f"💠 Pix de <b>{money('brl', price)}</b> — {esc(plan['name'])}\n\n"
                    "Escaneie o QR Code ou toque no código abaixo para copiar. "
                    "Assim que o pagamento cair, eu libero seu acesso na hora.",
            parse_mode=ParseMode.HTML,
        )
        await chat_message.reply_text(f"<code>{esc(payload)}</code>", parse_mode=ParseMode.HTML,
                                      reply_markup=Kb([[check_btn]]))
    else:
        await chat_message.reply_text(
            f"💳 Pagamento de <b>{money(currency, price)}</b> — {esc(plan['name'])}\n\n"
            "Toque em <b>Pagar</b> para abrir a página segura.",
            parse_mode=ParseMode.HTML,
            reply_markup=Kb([[Btn("💳 Pagar", url=payload)], [check_btn]]),
        )


# ───────────────────────── Comandos e botões ─────────────────────────
def plans_keyboard() -> Kb | None:
    rows = db.execute("SELECT * FROM plans WHERE active=1 ORDER BY sort, id").fetchall()
    if not rows:
        return None
    return Kb([[Btn(f"{p['name']} · {money('brl', p['price_brl'])}", callback_data=f"p:{p['id']}")] for p in rows])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    source = (context.args[0] if context.args else "")[:64] or None
    db.execute(
        """INSERT INTO leads (user_id, username, first_name, source, created_at) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name,
             blocked=0, source=COALESCE(leads.source, excluded.source)""",
        (u.id, u.username, u.first_name, source, now()),
    )
    db.commit()

    kb = plans_keyboard()
    if not kb:
        await update.message.reply_text("As vendas estão fechadas no momento. Volte em breve!")
        return
    await update.message.reply_text(get_setting("welcome_text"), reply_markup=kb)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.execute(
        """SELECT m.expires_at, p.name FROM members m LEFT JOIN plans p ON p.id=m.plan_id
           WHERE m.user_id=? AND m.removed=0 AND (m.expires_at IS NULL OR m.expires_at>?)""",
        (update.effective_user.id, now()),
    ).fetchall()
    if not rows:
        await update.message.reply_text("Você não tem acesso VIP ativo. Toque em /start para assinar.")
        return
    lines = [f"👑 {r['name'] or 'VIP'}: {'vitalício' if r['expires_at'] is None else 'até ' + fmt_date(r['expires_at'])}"
             for r in rows]
    await update.message.reply_text("\n".join(lines))


async def cmd_painel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not admin_id() or update.effective_user.id != admin_id():
        return
    url = gw.PUBLIC_URL or f"http://localhost:{os.environ.get('PANEL_PORT', '8080')}"
    await update.message.reply_text(f"🖥️ Painel: {url}")


_admin_tries: dict[int, list[int]] = {}


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/admin <senha do painel> — vira dono do bot (recebe avisos de venda e de grupos)."""
    u = update.effective_user
    if update.effective_chat.type != "private":
        return
    tries = [t for t in _admin_tries.get(u.id, []) if t > now() - 3600]
    if len(tries) >= 5:
        await update.message.reply_text("Muitas tentativas. Tente de novo em 1 hora.")
        return
    try:
        await update.message.delete()  # apaga a mensagem com a senha
    except TelegramError:
        pass
    senha = " ".join(context.args or [])
    if not senha or senha != os.environ.get("PANEL_PASSWORD", ""):
        _admin_tries[u.id] = tries + [now()]
        await context.bot.send_message(u.id, "❌ Senha incorreta. Use: /admin SUA_SENHA_DO_PAINEL")
        return
    set_setting("admin_id", str(u.id))
    await context.bot.send_message(
        u.id,
        "👑 Pronto, você é o dono do bot! Vou te avisar de cada venda.\n\n"
        "Próximo passo: adicione este bot como <b>administrador</b> do seu grupo ou canal VIP "
        "(com permissão para <b>convidar</b> e <b>banir</b> usuários). Eu detecto sozinho.",
        parse_mode=ParseMode.HTML,
    )


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detecta sozinho quando o bot é adicionado/promovido/removido de um grupo ou canal."""
    cmu = update.my_chat_member
    chat = cmu.chat
    if chat.type == "private":
        return
    new = cmu.new_chat_member
    is_admin = new.status == "administrator"
    can_invite = bool(is_admin and getattr(new, "can_invite_users", False))
    can_ban = bool(is_admin and getattr(new, "can_restrict_members", False))
    if chat.type == "channel" and is_admin:
        can_ban = True  # em canal, admin com "adicionar inscritos" já consegue remover

    if new.status in ("left", "kicked"):
        db.execute("DELETE FROM groups WHERE id=?", (chat.id,))
        db.commit()
        if admin_id():
            await safe_send(context.bot, admin_id(), f"⚠️ Fui removido de <b>{esc(chat.title)}</b>.",
                            parse_mode=ParseMode.HTML)
        return

    db.execute(
        """INSERT INTO groups (id, title, type, is_admin, can_invite, can_ban, updated_at) VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET title=excluded.title, type=excluded.type, is_admin=excluded.is_admin,
             can_invite=excluded.can_invite, can_ban=excluded.can_ban, updated_at=excluded.updated_at""",
        (chat.id, chat.title, chat.type, int(is_admin), int(can_invite), int(can_ban), now()),
    )
    db.commit()

    notify = admin_id() or cmu.from_user.id  # se ainda não tem dono, avisa quem adicionou
    title = esc(chat.title)
    if not is_admin:
        await safe_send(context.bot, notify,
                        f"👋 Entrei em <b>{title}</b>, mas preciso ser <b>administrador</b> para liberar e remover membros.",
                        parse_mode=ParseMode.HTML)
        return
    if not (can_invite and can_ban):
        falta = " e ".join(n for n, ok in (("convidar usuários", can_invite), ("banir usuários", can_ban)) if not ok)
        await safe_send(context.bot, notify,
                        f"⚠️ Sou admin em <b>{title}</b>, mas falta a permissão de <b>{falta}</b>. Ative para eu funcionar.",
                        parse_mode=ParseMode.HTML)
        return

    created = seed_from_env(chat.id)
    msg = f"✅ Tudo certo em <b>{title}</b>!"
    if created:
        p = db.execute("SELECT * FROM plans ORDER BY id LIMIT 1").fetchone()
        msg += (f"\n\nCriei o plano <b>{esc(p['name'])}</b> ({plan_duration(p['days'])}) por "
                f"<b>{money('brl', p['price_brl'])}</b> para esse grupo. As vendas já estão abertas: "
                f"https://t.me/{context.bot.username}\n\nPara mudar preço ou criar outros planos, use o painel.")
    else:
        msg += "\n\nPara vender acesso a ele, crie um plano no painel (aba Planos) e escolha esse grupo."
    if not admin_id():
        msg += "\n\n🔑 Para receber os avisos de venda, mande aqui: /admin SUA_SENHA_DO_PAINEL"
    await safe_send(context.bot, notify, msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def on_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    plan = get_plan(int(q.data.split(":")[1]))
    if not plan or not plan["active"]:
        await q.message.reply_text("Esse plano não está mais disponível. Toque em /start.")
        return

    currencies = ["brl"] + ([c for c in ("usd", "eur") if plan[f"price_{c}"]] if gw.STRIPE_ON else [])
    if len(currencies) == 1:
        await create_payment(context.bot, q.message, q.from_user.id, plan, "brl", "normal")
        return
    await q.message.reply_text(
        f"<b>{esc(plan['name'])}</b> — {plan_duration(plan['days'])}\nEscolha como pagar:",
        parse_mode=ParseMode.HTML,
        reply_markup=Kb([[Btn(f"{FLAGS[c]} {money(c, plan[f'price_{c}'])}" + (" · Pix" if c == "brl" else " · cartão"),
                              callback_data=f"b:{plan['id']}:{c}:n")] for c in currencies]),
    )


async def on_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    try:
        _, plan_id, currency, k = q.data.split(":")
        plan = get_plan(int(plan_id))
        kind = KIND_NAMES[k]
    except (ValueError, KeyError):
        return
    if not plan or not plan["active"] or currency not in LABELS:
        await q.message.reply_text("Esse plano não está mais disponível. Toque em /start.")
        return
    if currency != "brl" and not gw.STRIPE_ON:
        return
    if not offer_allowed(q.from_user.id, plan["id"], kind):
        await q.message.reply_text("Essa oferta expirou. Toque em /start para ver os planos.")
        return
    await create_payment(context.bot, q.message, q.from_user.id, plan, currency, kind)


async def on_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        pay_id = int(q.data.split(":")[1])
    except ValueError:
        return
    p = db.execute("SELECT user_id, status FROM payments WHERE id=?", (pay_id,)).fetchone()
    if not p or p["user_id"] != q.from_user.id:
        await q.answer("Pagamento não encontrado.", show_alert=True)
        return
    if p["status"] == "paid":
        await q.answer("Esse pagamento já foi confirmado ✅", show_alert=True)
        return
    try:
        st = await process_payment(context.bot, pay_id)
    except Exception as e:
        log.warning("Falha ao checar pagamento %s: %s", pay_id, e)
        await q.answer("Não consegui consultar agora. Tente de novo em instantes.", show_alert=True)
        return
    await q.answer({
        "paid": "Confirmado! Veja a mensagem com o seu acesso 🎉",
        "failed": "Essa cobrança expirou. Toque em /start para gerar outra.",
        "pending": "Ainda não recebi a confirmação. Às vezes leva alguns minutos — eu aviso sozinho quando cair.",
    }[st], show_alert=True)


# ───────────────────────── Tarefas automáticas ─────────────────────────
async def job_poll(context: ContextTypes.DEFAULT_TYPE) -> None:
    cutoff = now() - 25 * 3600
    for row in db.execute("SELECT id FROM payments WHERE status='pending' AND created_at>?", (cutoff,)).fetchall():
        try:
            await process_payment(context.bot, row["id"])
        except Exception as e:
            log.warning("Falha ao checar %s: %s", row["id"], e)
    db.execute("UPDATE payments SET status='expired' WHERE status='pending' AND created_at<=?", (cutoff,))
    db.commit()


NOT_PAID_SINCE = """NOT EXISTS (SELECT 1 FROM payments p2 WHERE p2.user_id=payments.user_id
                    AND p2.status='paid' AND p2.created_at>=payments.created_at)"""


async def job_remarketing(context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = context.bot

    # 1) Lembrete: Pix gerado e não pago
    if get_setting("remarketing_on") == "1":
        limit = now() - get_int("remarketing_minutes", 15) * 60
        for p in db.execute(
            f"""SELECT * FROM payments WHERE kind='normal' AND status='pending' AND followup=0
                AND created_at<=? AND created_at>? AND {NOT_PAID_SINCE}""",
            (limit, now() - 86400),
        ).fetchall():
            db.execute("UPDATE payments SET followup=1 WHERE id=?", (p["id"],))
            db.commit()
            await safe_send(bot, p["user_id"], get_setting("remarketing_text"),
                            reply_markup=Kb([[Btn("✅ Já paguei", callback_data=f"c:{p['id']}")]]))

    # 2) Downsell: oferece desconto para quem não pagou
    if get_setting("downsell_on") == "1" and get_int("downsell_percent", 0) > 0:
        limit = now() - get_int("downsell_minutes", 60) * 60
        for p in db.execute(
            f"""SELECT * FROM payments WHERE kind='normal' AND status IN ('pending','expired') AND followup<2
                AND created_at<=? AND created_at>? AND {NOT_PAID_SINCE}""",
            (limit, now() - 86400),
        ).fetchall():
            # um downsell por pessoa e plano a cada 2 dias
            dup = db.execute("SELECT 1 FROM payments WHERE user_id=? AND plan_id=? AND followup=2 AND created_at>?",
                             (p["user_id"], p["plan_id"], now() - 2 * 86400)).fetchone()
            db.execute("UPDATE payments SET followup=2 WHERE id=?", (p["id"],))
            db.commit()
            plan = get_plan(p["plan_id"])
            if dup or not plan or not plan["active"]:
                continue
            price = plan_price(plan, "brl", "downsell")
            await safe_send(
                bot, p["user_id"],
                f"{esc(get_setting('downsell_text'))}\n\n<b>{esc(plan['name'])}</b>\n"
                f"<s>{money('brl', plan['price_brl'])}</s> por <b>{money('brl', price)}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=Kb([[Btn(f"🎁 Pegar por {money('brl', price)}", callback_data=f"b:{plan['id']}:brl:d")]]),
            )


async def job_expirations(context: ContextTypes.DEFAULT_TYPE) -> None:
    bot = context.bot
    t = now()

    remind = get_int("remind_days", 3) * 86400
    for m in db.execute(
        """SELECT * FROM members WHERE removed=0 AND reminded=0 AND expires_at IS NOT NULL
           AND expires_at BETWEEN ? AND ?""", (t, t + remind)
    ).fetchall():
        db.execute("UPDATE members SET reminded=1 WHERE user_id=? AND group_id=?", (m["user_id"], m["group_id"]))
        db.commit()
        await safe_send(bot, m["user_id"], f"⏰ Seu VIP vence em {fmt_date(m['expires_at'])}. Toque em /start para renovar.")

    for m in db.execute(
        "SELECT * FROM members WHERE removed=0 AND expires_at IS NOT NULL AND expires_at<=?", (t,)
    ).fetchall():
        await remove_member(bot, m["user_id"], m["group_id"])
        await safe_send(bot, m["user_id"], get_setting("expired_text"))


# ───────────────────────── Disparo em massa ─────────────────────────
SEGMENTS = {
    "todos": ("Todos os leads", "SELECT user_id FROM leads WHERE blocked=0"),
    "ativos": ("Assinantes ativos",
               "SELECT DISTINCT user_id FROM members WHERE removed=0 AND (expires_at IS NULL OR expires_at>:t)"),
    "nunca_pagou": ("Nunca pagaram",
                    "SELECT user_id FROM leads WHERE blocked=0 AND user_id NOT IN (SELECT user_id FROM payments WHERE status='paid')"),
    "ex": ("Ex-assinantes (venceu)",
           """SELECT DISTINCT user_id FROM members WHERE removed=1 AND user_id NOT IN
              (SELECT user_id FROM members WHERE removed=0 AND (expires_at IS NULL OR expires_at>:t))"""),
}

broadcast_state = {"running": False, "sent": 0, "failed": 0, "total": 0}


def segment_users(segment: str) -> list[int]:
    return [r[0] for r in db.execute(SEGMENTS[segment][1], {"t": now()}).fetchall()]


async def broadcast(bot, segment: str, text: str, with_plans: bool) -> None:
    users = segment_users(segment)
    broadcast_state.update(running=True, sent=0, failed=0, total=len(users))
    kb = plans_keyboard() if with_plans else None
    try:
        for uid in users:
            ok = await safe_send(bot, uid, text, reply_markup=kb)
            broadcast_state["sent" if ok else "failed"] += 1
            await asyncio.sleep(0.05)  # ~20 msg/s, dentro do limite do Telegram
    finally:
        broadcast_state["running"] = False


# ───────────────────────── Montagem ─────────────────────────
def build_application() -> Application:
    app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("painel", cmd_painel))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CallbackQueryHandler(on_plan, pattern=r"^p:"))
    app.add_handler(CallbackQueryHandler(on_buy, pattern=r"^b:"))
    app.add_handler(CallbackQueryHandler(on_check, pattern=r"^c:"))
    app.job_queue.run_repeating(job_poll, interval=CHECK_EVERY, first=5)
    app.job_queue.run_repeating(job_remarketing, interval=60, first=20)
    app.job_queue.run_repeating(job_expirations, interval=600, first=30)
    return app
