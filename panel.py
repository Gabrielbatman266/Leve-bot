"""Painel web do Leve VIP (abre no navegador, funciona no celular)."""

import asyncio
import hmac
import logging
import os
import re
import secrets
from datetime import datetime

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

import api
import autopost
import core
import gateways as gw
from core import esc, money
from db import DEFAULTS, admin_id, db, get_setting, now, panel_secret, set_setting

log = logging.getLogger("levevip.panel")

PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
WEBHOOK_TOKEN = os.environ.get("SYNCPAY_WEBHOOK_TOKEN", "")

CSS = """
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
background:#0f1115;color:#e7e9ee}a{color:#8ab4ff;text-decoration:none}
header{position:sticky;top:0;background:#151821;border-bottom:1px solid #262b36;padding:10px 14px;z-index:5}
header b{font-size:17px;margin-right:10px}nav{display:flex;gap:6px;overflow-x:auto;margin-top:8px;padding-bottom:2px}
nav a{white-space:nowrap;padding:6px 11px;border-radius:999px;background:#1d2130;color:#c9cfdb;font-size:14px}
nav a.on{background:#3b5bdb;color:#fff}main{padding:14px;max-width:1000px;margin:auto}
h2{font-size:18px;margin:18px 0 10px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.card{background:#171a23;border:1px solid #262b36;border-radius:12px;padding:12px}
.card small{color:#8b93a7;display:block;font-size:12px}.card .v{font-size:20px;font-weight:700;margin-top:2px}
.tw{overflow-x:auto;background:#171a23;border:1px solid #262b36;border-radius:12px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:8px 10px;border-bottom:1px solid #232835;text-align:left;white-space:nowrap}
th{color:#8b93a7;font-weight:600;font-size:12px}tr:last-child td{border-bottom:0}
form.box{background:#171a23;border:1px solid #262b36;border-radius:12px;padding:12px;display:grid;gap:10px}
label{font-size:13px;color:#aab2c3;display:grid;gap:4px}
input,select,textarea{width:100%;padding:9px 10px;border-radius:8px;border:1px solid #303747;background:#0f1115;color:#e7e9ee;font:inherit}
textarea{min-height:80px}.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}
button{padding:9px 14px;border:0;border-radius:8px;background:#3b5bdb;color:#fff;font:inherit;font-weight:600}
button.sec{background:#2a3040}button.red{background:#c92a2a}button.sm{padding:5px 9px;font-size:13px}
.pill{padding:2px 8px;border-radius:999px;font-size:12px}.ok{background:#1f4d2e;color:#8ce99a}
.pend{background:#4d401f;color:#ffd43b}.exp{background:#3a2424;color:#ffa8a8}.muted{color:#8b93a7}
.flash{background:#1f3a5f;border:1px solid #2f5a8f;padding:10px;border-radius:10px;margin-bottom:12px}
.inline{display:inline}code{background:#1d2130;padding:2px 6px;border-radius:6px;word-break:break-all}
"""

NAV = [("/", "Início"), ("/vendas", "Vendas"), ("/membros", "Membros"), ("/planos", "Planos"),
       ("/links", "Links"), ("/copy", "Copy"), ("/disparo", "Disparo"), ("/postagem", "Postagem"), ("/postleve", "Post Leve"), ("/config", "Config"), ("/sair", "Sair")]


# ───────────────────────── Helpers ─────────────────────────
def page(request: Request, title: str, body: str, flash: str = "") -> HTMLResponse:
    path = request.url.path
    nav = "".join(f'<a href="{h}" class="{"on" if h == path else ""}">{t}</a>' for h, t in NAV)
    fl = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    msg = request.session.pop("flash", "")
    if msg:
        fl += f'<div class="flash">{esc(msg)}</div>'
    return HTMLResponse(
        f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · Leve VIP</title>
<style>{CSS}</style></head><body><header><b>👑 Leve VIP</b><nav>{nav}</nav></header>
<main>{fl}{body}</main></body></html>"""
    )


def csrf(request: Request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_hex(16)
    return f'<input type="hidden" name="csrf" value="{request.session["csrf"]}">'


async def form_data(request: Request):
    form = await request.form()
    if not hmac.compare_digest(form.get("csrf", ""), request.session.get("csrf", "-")):
        raise PermissionError("csrf")
    return form


def logged(request: Request) -> bool:
    return request.session.get("ok") is True


def back(request: Request, url: str, flash: str = "") -> RedirectResponse:
    if flash:
        request.session["flash"] = flash
    return RedirectResponse(url, status_code=303)


def protected(handler):
    async def wrapper(request: Request):
        if not logged(request):
            return RedirectResponse("/entrar", status_code=303)
        try:
            return await handler(request)
        except PermissionError:
            section = "/" + request.url.path.strip("/").split("/")[0]
            return back(request, section, "Sessão expirada, tente de novo.")
    return wrapper


def parse_money(text: str) -> int | None:
    """'29,90' / '29.90' / '1.299,90' -> centavos. Vazio -> None."""
    t = (text or "").strip().replace("R$", "").replace("US$", "").replace("€", "").strip()
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    return round(float(t) * 100)


def cents_to_input(c) -> str:
    return "" if c is None else f"{c / 100:.2f}".replace(".", ",")


def dt(ts) -> str:
    return "—" if not ts else datetime.fromtimestamp(ts).strftime("%d/%m %H:%M")


def who(row) -> str:
    if row["username"]:
        return f'@{esc(row["username"])}'
    return esc(row["first_name"] or row["user_id"])


def revenue_since(ts: int) -> tuple[str, int]:
    rows = db.execute("SELECT currency, SUM(amount), COUNT(*) FROM payments WHERE status='paid' AND paid_at>=? "
                      "GROUP BY currency", (ts,)).fetchall()
    total_n = sum(r[2] for r in rows)
    if not rows:
        return money("brl", 0), 0
    return " + ".join(money(r[0], r[1]) for r in rows), total_n


def status_pill(st: str) -> str:
    cls, txt = {"paid": ("ok", "pago"), "pending": ("pend", "pendente"), "expired": ("exp", "expirado")}.get(st, ("exp", st))
    return f'<span class="pill {cls}">{txt}</span>'


def sources_table() -> str:
    rows = db.execute(
        """SELECT COALESCE(l.source,'(direto)') src, COUNT(*) leads,
             COUNT(DISTINCT CASE WHEN p.status='paid' THEN l.user_id END) buyers,
             COALESCE(SUM(CASE WHEN p.status='paid' AND p.currency='brl' THEN p.amount END),0) brl
           FROM leads l LEFT JOIN payments p ON p.user_id=l.user_id
           GROUP BY src ORDER BY brl DESC, leads DESC LIMIT 50"""
    ).fetchall()
    # leads conta duplicado com o join; recalcula leads por origem
    leads = dict(db.execute("SELECT COALESCE(source,'(direto)'), COUNT(*) FROM leads GROUP BY 1").fetchall())
    if not rows:
        return '<p class="muted">Nenhum lead ainda.</p>'
    trs = ""
    for r in rows:
        n = leads.get(r["src"], 0)
        conv = f"{r['buyers'] * 100 / n:.1f}%" if n else "—"
        lbl = api.label_of(r["src"])
        name = f"{esc(lbl)}<br><span class='muted'>{esc(r['src'])}</span>" if lbl else esc(r["src"])
        trs += f"<tr><td>{name}</td><td>{n}</td><td>{r['buyers']}</td><td>{conv}</td><td>{money('brl', r['brl'])}</td></tr>"
    return f'<div class="tw"><table><tr><th>Origem</th><th>Leads</th><th>Compraram</th><th>Conversão</th><th>Faturou (R$)</th></tr>{trs}</table></div>'


def sales_rows(where: str = "1=1", params: tuple = (), limit: int = 200) -> str:
    rows = db.execute(
        f"""SELECT p.*, l.username, l.first_name, pl.name plan_name FROM payments p
            LEFT JOIN leads l ON l.user_id=p.user_id LEFT JOIN plans pl ON pl.id=p.plan_id
            WHERE {where} ORDER BY p.id DESC LIMIT {int(limit)}""", params).fetchall()
    if not rows:
        return '<p class="muted">Nada por aqui ainda.</p>'
    trs = ""
    for r in rows:
        kind = "" if r["kind"] == "normal" else f' <span class="pill pend">{r["kind"]}</span>'
        trs += (f"<tr><td>{dt(r['paid_at'] or r['created_at'])}</td><td>{who(r)}</td><td>{esc(r['plan_name'] or '—')}{kind}</td>"
                f"<td>{money(r['currency'], r['amount'])}</td><td>{'Pix' if r['provider'] == 'syncpay' else 'Cartão'}</td>"
                f"<td>{status_pill(r['status'])}</td></tr>")
    return (f'<div class="tw"><table><tr><th>Data</th><th>Cliente</th><th>Plano</th><th>Valor</th>'
            f'<th>Via</th><th>Status</th></tr>{trs}</table></div>')


def plan_options(selected=None, include_empty=False) -> str:
    opts = '<option value="">—</option>' if include_empty else ""
    for p in db.execute("SELECT id, name, active FROM plans ORDER BY sort, id").fetchall():
        sel = " selected" if str(p["id"]) == str(selected) else ""
        off = "" if p["active"] else " (pausado)"
        opts += f'<option value="{p["id"]}"{sel}>{esc(p["name"])}{off}</option>'
    return opts


# ───────────────────────── Login ─────────────────────────
async def login(request: Request):
    error = ""
    if request.method == "POST":
        form = await request.form()
        if PANEL_PASSWORD and hmac.compare_digest(form.get("senha", ""), PANEL_PASSWORD):
            request.session.clear()
            request.session["ok"] = True
            return RedirectResponse("/", status_code=303)
        await asyncio.sleep(1.5)  # freia tentativas
        error = '<p style="color:#ffa8a8">Senha incorreta.</p>'
    return HTMLResponse(
        f"""<!doctype html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Entrar · Leve VIP</title>
<style>{CSS}</style></head><body><main style="max-width:360px;margin-top:12vh">
<h2>👑 Leve VIP</h2>{error}<form class="box" method="post"><label>Senha do painel
<input type="password" name="senha" autofocus></label><button>Entrar</button></form></main></body></html>"""
    )


async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/entrar", status_code=303)


# ───────────────────────── Início ─────────────────────────
@protected
async def home(request: Request):
    t = now()
    midnight = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    today, n_today = revenue_since(midnight)
    week, n_week = revenue_since(t - 7 * 86400)
    month, n_month = revenue_since(t - 30 * 86400)
    leads = db.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    leads_today = db.execute("SELECT COUNT(*) FROM leads WHERE created_at>=?", (midnight,)).fetchone()[0]
    buyers = db.execute("SELECT COUNT(DISTINCT user_id) FROM payments WHERE status='paid'").fetchone()[0]
    active = db.execute("SELECT COUNT(DISTINCT user_id) FROM members WHERE removed=0 AND "
                        "(expires_at IS NULL OR expires_at>?)", (t,)).fetchone()[0]
    pending = db.execute("SELECT COUNT(*) FROM payments WHERE status='pending'").fetchone()[0]
    recovered = db.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments WHERE status='paid' AND kind='downsell'").fetchone()
    upsold = db.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM payments WHERE status='paid' AND kind='upsell'").fetchone()
    conv = f"{buyers * 100 / leads:.1f}%" if leads else "—"

    try:
        bal = await asyncio.wait_for(gw.syncpay_balance(), 6)
        balance = f"R$ {bal.get('balance', '?')}"
    except Exception:
        balance = "—"

    def card(label, value, sub=""):
        return f'<div class="card"><small>{label}</small><div class="v">{value}</div><small>{sub}</small></div>'

    steps = []
    if not admin_id():
        steps.append("Mande <code>/admin SUA_SENHA</code> para o bot no Telegram")
    if not db.execute("SELECT 1 FROM groups WHERE is_admin=1 AND can_invite=1 AND can_ban=1").fetchone():
        steps.append("Adicione o bot como administrador do grupo/canal VIP (convidar + banir)")
    if not db.execute("SELECT 1 FROM plans WHERE active=1").fetchone():
        steps.append('Crie um plano na aba <a href="/planos">Planos</a>')
    todo = ("<div class='flash'><b>Falta pouco:</b><br>" + "<br>".join(f"☐ {x}" for x in steps) + "</div>") if steps else ""
    body = todo + f"""
<div class="grid">
{card("Hoje", today, f"{n_today} vendas")}{card("7 dias", week, f"{n_week} vendas")}{card("30 dias", month, f"{n_month} vendas")}
{card("Saldo SyncPay", balance)}{card("Assinantes ativos", active)}{card("Leads", leads, f"+{leads_today} hoje")}
{card("Conversão", conv, f"{buyers} compradores")}{card("Pix pendentes", pending)}
{card("Downsell recuperou", money('brl', recovered[1]), f"{recovered[0]} vendas")}
{card("Upsell", money('brl', upsold[1]), f"{upsold[0]} vendas")}
</div>
<h2>Origem das vendas</h2>{sources_table()}
<h2>Últimas vendas</h2>{sales_rows("p.status='paid'", limit=10)}
"""
    return page(request, "Início", body)


# ───────────────────────── Vendas ─────────────────────────
@protected
async def sales(request: Request):
    st = request.query_params.get("s", "paid")
    where = {"paid": "p.status='paid'", "pending": "p.status='pending'", "expired": "p.status='expired'"}.get(st, "1=1")
    tabs = " ".join(
        f'<a href="/vendas?s={k}"><button class="sm {"" if k == st else "sec"}">{v}</button></a>'
        for k, v in [("paid", "Pagas"), ("pending", "Pendentes"), ("expired", "Expiradas"), ("all", "Todas")])
    extra = ""
    if st == "pending":
        extra = f'<form method="post" action="/vendas/verificar" style="margin:10px 0">{csrf(request)}' \
                '<button class="sec">🔄 Verificar pendentes agora</button></form>'
    return page(request, "Vendas", f"<h2>Vendas</h2><p>{tabs}</p>{extra}{sales_rows(where)}")


@protected
async def sales_check(request: Request):
    await form_data(request)
    bot = request.app.state.tg.bot
    ids = [r[0] for r in db.execute("SELECT id FROM payments WHERE status='pending'").fetchall()]
    paid = 0
    for i in ids:
        try:
            if await core.process_payment(bot, i) == "paid":
                paid += 1
        except Exception as e:
            log.warning("Falha ao verificar %s: %s", i, e)
    return back(request, "/vendas?s=pending", f"{len(ids)} verificados, {paid} confirmados agora.")


# ───────────────────────── Membros ─────────────────────────
@protected
async def members(request: Request):
    t = now()
    q = request.query_params.get("q", "").strip().lstrip("@")
    where, params = "m.removed=0 AND (m.expires_at IS NULL OR m.expires_at>?)", [t]
    if q:
        where += " AND (CAST(m.user_id AS TEXT)=? OR l.username LIKE ?)"
        params += [q, f"%{q}%"]
    rows = db.execute(
        f"""SELECT m.*, l.username, l.first_name, p.name plan_name FROM members m
            LEFT JOIN leads l ON l.user_id=m.user_id LEFT JOIN plans p ON p.id=m.plan_id
            WHERE {where} ORDER BY m.expires_at IS NULL, m.expires_at LIMIT 500""", params).fetchall()
    c = csrf(request)
    trs = ""
    for r in rows:
        exp = "vitalício" if r["expires_at"] is None else dt(r["expires_at"])
        days_left = "" if r["expires_at"] is None else f' <span class="muted">({(r["expires_at"] - t) // 86400}d)</span>'
        trs += (f"<tr><td>{who(r)}<br><span class='muted'>{r['user_id']}</span></td><td>{esc(r['plan_name'] or '—')}</td>"
                f"<td>{exp}{days_left}</td><td>"
                f"<form class='inline' method='post' action='/membros/dias'>{c}"
                f"<input type='hidden' name='user_id' value='{r['user_id']}'><input type='hidden' name='group_id' value='{r['group_id']}'>"
                f"<input name='dias' value='30' style='width:58px;padding:5px'> <button class='sm sec'>+ dias</button></form> "
                f"<form class='inline' method='post' action='/membros/remover' onsubmit=\"return confirm('Remover do grupo?')\">{c}"
                f"<input type='hidden' name='user_id' value='{r['user_id']}'><input type='hidden' name='group_id' value='{r['group_id']}'>"
                f"<button class='sm red'>Remover</button></form></td></tr>")
    table = (f'<div class="tw"><table><tr><th>Membro</th><th>Plano</th><th>Vence</th><th></th></tr>{trs}</table></div>'
             if rows else '<p class="muted">Nenhum assinante ativo.</p>')
    body = f"""
<h2>Liberar acesso manual</h2>
<form class="box" method="post" action="/membros/adicionar">{c}
<div class="row"><label>ID do Telegram<input name="user_id" required inputmode="numeric"></label>
<label>Plano<select name="plan_id">{plan_options()}</select></label>
<label>Dias (vazio = do plano)<input name="dias" inputmode="numeric"></label></div>
<button>Liberar e mandar convite</button></form>
<h2>Assinantes ativos ({len(rows)})</h2>
<form method="get" style="margin-bottom:10px"><input name="q" value="{esc(q)}" placeholder="Buscar por @usuário ou ID"></form>
{table}"""
    return page(request, "Membros", body)


@protected
async def members_add(request: Request):
    f = await form_data(request)
    try:
        user_id = int(f["user_id"])
        plan = core.get_plan(int(f["plan_id"]))
    except (ValueError, KeyError):
        return back(request, "/membros", "Dados inválidos.")
    if not plan:
        return back(request, "/membros", "Plano não encontrado.")
    plan = dict(plan)
    if f.get("dias", "").strip():
        plan["days"] = int(f["dias"])
    await core.grant(request.app.state.tg.bot, user_id, plan)
    return back(request, "/membros", f"Acesso liberado para {user_id}.")


@protected
async def members_days(request: Request):
    f = await form_data(request)
    try:
        uid, gid, days = int(f["user_id"]), int(f["group_id"]), int(f["dias"])
    except (ValueError, KeyError):
        return back(request, "/membros", "Dados inválidos.")
    db.execute("UPDATE members SET expires_at=MAX(expires_at, ?) + ?, reminded=0 WHERE user_id=? AND group_id=? "
               "AND expires_at IS NOT NULL", (now(), days * 86400, uid, gid))
    db.commit()
    return back(request, "/membros", f"+{days} dias para {uid}.")


@protected
async def members_remove(request: Request):
    f = await form_data(request)
    uid, gid = int(f["user_id"]), int(f["group_id"])
    await core.remove_member(request.app.state.tg.bot, uid, gid)
    return back(request, "/membros", f"{uid} removido.")


# ───────────────────────── Planos ─────────────────────────
def group_options(selected=None) -> str:
    rows = db.execute("SELECT * FROM groups ORDER BY title").fetchall()
    ids = {r["id"] for r in rows}
    opts = ""
    if selected is not None and selected not in ids:
        opts += f'<option value="{selected}" selected>{selected}</option>'
    for r in rows:
        sel = " selected" if r["id"] == selected else ""
        warn = "" if r["is_admin"] and r["can_invite"] and r["can_ban"] else " ⚠️ sem permissão"
        opts += f'<option value="{r["id"]}"{sel}>{esc(r["title"])}{warn}</option>'
    return opts or '<option value="">Nenhum grupo — adicione o bot como admin</option>'


def groups_table() -> str:
    rows = db.execute("SELECT * FROM groups ORDER BY title").fetchall()
    if not rows:
        return ('<p class="muted">Nenhum ainda. Adicione o bot como <b>administrador</b> do seu grupo ou canal '
                '(com permissão de convidar e banir) e ele aparece aqui sozinho.</p>')
    trs = ""
    for r in rows:
        ok = r["is_admin"] and r["can_invite"] and r["can_ban"]
        st = '<span class="pill ok">pronto</span>' if ok else '<span class="pill exp">falta permissão</span>'
        trs += f"<tr><td>{esc(r['title'])}</td><td>{'canal' if r['type'] == 'channel' else 'grupo'}</td><td>{st}</td><td class='muted'>{r['id']}</td></tr>"
    return f'<div class="tw"><table><tr><th>Nome</th><th>Tipo</th><th>Status</th><th>ID</th></tr>{trs}</table></div>'


ACTIVE_PILL = '<span class="pill ok">ativo</span>'
PAUSED_PILL = '<span class="pill exp">pausado</span>'

@protected
async def plans(request: Request):
    edit_id = request.query_params.get("editar")
    p = core.get_plan(int(edit_id)) if edit_id and edit_id.isdigit() else None
    c = csrf(request)
    rows = db.execute("SELECT * FROM plans ORDER BY sort, id").fetchall()
    trs = ""
    for r in rows:
        extra = " · ".join(money(cur, r[f"price_{cur}"]) for cur in ("usd", "eur") if r[f"price_{cur}"])
        trs += (f"<tr><td>{esc(r['name'])}</td><td>{core.plan_duration(r['days'])}</td>"
                f"<td>{money('brl', r['price_brl'])}<br><span class='muted'>{extra}</span></td><td>{r['group_id']}</td>"
                f"<td>{ACTIVE_PILL if r['active'] else PAUSED_PILL}</td>"
                f"<td><a href='/planos?editar={r['id']}'><button class='sm sec'>Editar</button></a> "
                f"<form class='inline' method='post' action='/planos/alternar'>{c}<input type='hidden' name='id' value='{r['id']}'>"
                f"<button class='sm sec'>{'Pausar' if r['active'] else 'Ativar'}</button></form></td></tr>")
    table = (f'<div class="tw"><table><tr><th>Plano</th><th>Duração</th><th>Preço</th><th>Grupo</th><th></th><th></th></tr>{trs}</table></div>'
             if rows else '<p class="muted">Nenhum plano. Crie o primeiro abaixo.</p>')
    v = (lambda k, d="": esc(p[k]) if p and p[k] is not None else d)
    stripe_note = "" if gw.STRIPE_ON else '<p class="muted">Preços em US$ e € só aparecem se a chave do Stripe estiver no .env.</p>'
    body = f"""
<h2>Planos</h2>{table}
<h2>Grupos detectados</h2>{groups_table()}
<h2>{"Editar plano" if p else "Novo plano"}</h2>
<form class="box" method="post" action="/planos/salvar">{c}<input type="hidden" name="id" value="{v('id')}">
<div class="row"><label>Nome<input name="name" required value="{v('name')}" placeholder="VIP Mensal"></label>
<label>Dias (0 = vitalício)<input name="days" required inputmode="numeric" value="{v('days', '30')}"></label>
<label>Ordem<input name="sort" inputmode="numeric" value="{v('sort', '0')}"></label></div>
<div class="row"><label>Preço R$ (Pix)<input name="price_brl" required value="{cents_to_input(p['price_brl']) if p else ''}" placeholder="29,90"></label>
<label>Preço US$ (opcional)<input name="price_usd" value="{cents_to_input(p['price_usd']) if p else ''}"></label>
<label>Preço € (opcional)<input name="price_eur" value="{cents_to_input(p['price_eur']) if p else ''}"></label></div>
<label>Grupo/canal liberado<select name="group_id">{group_options(p['group_id'] if p else None)}</select></label>
<label>…ou cole o ID manualmente (opcional)<input name="group_id_manual" placeholder="-100…"></label>
{stripe_note}<button>Salvar plano</button></form>"""
    return page(request, "Planos", body)


@protected
async def plans_save(request: Request):
    f = await form_data(request)
    try:
        data = (
            f["name"].strip()[:60], int(f["days"]), parse_money(f["price_brl"]),
            parse_money(f.get("price_usd", "")), parse_money(f.get("price_eur", "")),
            int((f.get("group_id_manual") or "").strip() or f["group_id"]), int(f.get("sort") or 0),
        )
        if not data[0] or data[2] is None or data[2] < 100 or data[1] < 0:
            raise ValueError
    except (ValueError, KeyError):
        return back(request, "/planos", "Confira os campos: nome, dias, preço (mínimo R$ 1,00) e ID do grupo.")
    if f.get("id"):
        db.execute("UPDATE plans SET name=?, days=?, price_brl=?, price_usd=?, price_eur=?, group_id=?, sort=? WHERE id=?",
                   (*data, int(f["id"])))
    else:
        db.execute("INSERT INTO plans (name, days, price_brl, price_usd, price_eur, group_id, sort) VALUES (?,?,?,?,?,?,?)", data)
    db.commit()
    return back(request, "/planos", "Plano salvo.")


@protected
async def plans_toggle(request: Request):
    f = await form_data(request)
    db.execute("UPDATE plans SET active=1-active WHERE id=?", (int(f["id"]),))
    db.commit()
    return back(request, "/planos")


# ───────────────────────── Links de rastreio ─────────────────────────
@protected
async def links(request: Request):
    bot_user = request.app.state.tg.bot.username
    src = re.sub(r"[^A-Za-z0-9_-]", "", request.query_params.get("origem", ""))[:64]
    made = ""
    if src:
        url = f"https://t.me/{bot_user}?start={src}"
        made = f'<div class="flash">Seu link: <code>{esc(url)}</code></div>'
    body = f"""
<h2>Criar link rastreado</h2>
<p class="muted">Use um link diferente em cada lugar (bio do Instagram, Threads, TikTok…) para saber de onde vêm as vendas.</p>
{made}<form class="box" method="get"><label>Nome da origem (letras, números, _ e -)
<input name="origem" placeholder="threads_bio" required pattern="[A-Za-z0-9_-]+"></label><button>Gerar link</button></form>
<h2>Desempenho por origem</h2>{sources_table()}"""
    return page(request, "Links", body)


# ───────────────────────── Disparo ─────────────────────────
@protected
async def broadcast(request: Request):
    st = core.broadcast_state
    if request.method == "POST":
        f = await form_data(request)
        seg, text = f.get("segmento"), (f.get("texto") or "").strip()
        if st["running"]:
            return back(request, "/disparo", "Já tem um disparo em andamento.")
        if seg not in core.SEGMENTS or not text:
            return back(request, "/disparo", "Escolha o público e escreva a mensagem.")
        asyncio.create_task(core.broadcast(request.app.state.tg.bot, seg, text, f.get("planos") == "1"))
        return back(request, "/disparo", "Disparo iniciado!")

    opts = "".join(f'<option value="{k}">{v[0]} ({len(core.segment_users(k))})</option>' for k, v in core.SEGMENTS.items())
    status = ""
    if st["total"]:
        status = (f'<div class="flash">{"⏳ Enviando…" if st["running"] else "✅ Último disparo concluído"} '
                  f'— {st["sent"]} enviadas, {st["failed"]} falharam, de {st["total"]}. '
                  f'{"<a href=/disparo>atualizar</a>" if st["running"] else ""}</div>')
    body = f"""<h2>Disparo em massa</h2>{status}
<form class="box" method="post">{csrf(request)}
<label>Público<select name="segmento">{opts}</select></label>
<label>Mensagem<textarea name="texto" required placeholder="🔥 Promoção relâmpago só hoje!"></textarea></label>
<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="planos" value="1" checked style="width:auto">
Incluir botões dos planos</label>
<button onclick="return confirm('Enviar para todo esse público?')">Enviar</button></form>"""
    return page(request, "Disparo", body)


# ───────────────────────── Post Leve ─────────────────────────
@protected
async def postleve(request: Request):
    from urllib.parse import quote
    if request.method == "POST":
        await form_data(request)
        api.new_api_key()
        return back(request, "/postleve", "Chave nova gerada. Toque em Conectar de novo.")

    key = api.api_key()
    port = os.environ.get("PANEL_PORT", "8080")
    base = gw.PUBLIC_URL or f"http://127.0.0.1:{port}"
    bot_user = request.app.state.tg.bot.username
    q = f"u={quote(base, safe='')}&k={quote(key, safe='')}&b={quote(bot_user, safe='')}"
    intent_link = f"intent://vip?{q}#Intent;scheme=postleve;package=com.leve.analise;end"
    plain_link = f"postleve://vip?{q}"

    seen = int(get_setting("postleve_seen") or 0)
    if seen and now() - seen < 3 * 3600:
        status = f'<span class="pill ok">conectado</span> último contato {dt(seen)}'
    elif seen:
        status = f'<span class="pill pend">sem contato desde {dt(seen)}</span>'
    else:
        status = '<span class="pill exp">ainda não conectado</span>'

    th = db.execute(
        """SELECT COUNT(DISTINCT l.user_id) leads,
             COUNT(DISTINCT CASE WHEN p.status='paid' THEN p.id END) vendas,
             COALESCE(SUM(CASE WHEN p.status='paid' AND p.currency='brl' THEN p.amount END),0) valor
           FROM leads l LEFT JOIN payments p ON p.user_id=l.user_id WHERE substr(l.source, 1, 3)='th_'"""
    ).fetchone()
    vip_posts = get_setting("postleve_vip_posts") or "0"

    body = f"""
<h2>Post Leve</h2>
<p>{status}</p>
<div class="grid">
<div class="card"><small>Leads vindos do Threads</small><div class="v">{th['leads']}</div></div>
<div class="card"><small>Vendas vindas do Threads</small><div class="v">{th['vendas']}</div><small>{money('brl', th['valor'])}</small></div>
<div class="card"><small>Fotos postadas no VIP</small><div class="v">{esc(vip_posts)}</div></div>
</div>
<h2>Conectar</h2>
<form class="box" onsubmit="return false">
<p class="muted" style="margin:0">Abra esta página <b>no mesmo celular</b> do Post Leve e toque no botão. O Post Leve abre já conectado.</p>
<a href="{esc(intent_link)}"><button type="button" style="width:100%">🔗 Conectar Post Leve</button></a>
<p class="muted" style="margin:0">Não abriu? Tente <a href="{esc(plain_link)}">este link</a>, ou no Post Leve toque em
<b>Colar conexão</b> depois de copiar:</p>
<code id="cx">{esc(plain_link)}</code>
<button type="button" class="sec" onclick="navigator.clipboard.writeText(document.getElementById('cx').innerText);this.innerText='Copiado ✓'">Copiar conexão</button>
</form>
<h2>O que eles trocam</h2>
<div class="card">
• O Post Leve coloca o seu link rastreado em posts de divulgação do VIP no Threads.<br>
• O Leve VIP conta quem entrou e quem comprou por cada post e devolve para o Post Leve, que aprende
quais fotos e legendas <b>vendem</b> e passa a usar mais elas.<br>
• O Post Leve manda fotos para o grupo VIP sozinho, para os assinantes sempre terem conteúdo novo.
</div>
<h2>Segurança</h2>
<form class="box" method="post" onsubmit="return confirm('Gerar chave nova? O Post Leve vai precisar conectar de novo.')">{csrf(request)}
<p class="muted" style="margin:0">A chave dá acesso só a essas trocas (não mexe em pagamentos nem membros).</p>
<button class="sec">🔄 Gerar chave nova</button></form>"""
    return page(request, "Post Leve", body)


# ───────────────────────── Config ─────────────────────────
FIELDS = [
    ("Vencimento", [
        ("remind_days", "Avisar quantos dias antes de vencer", "num"),
    ]),
    ("Remarketing (Pix gerado e não pago)", [
        ("remarketing_on", "Ativo", "bool"),
        ("remarketing_minutes", "Lembrar depois de (minutos)", "num"),
    ]),
    ("Downsell (desconto para quem não pagou)", [
        ("downsell_on", "Ativo", "bool"),
        ("downsell_minutes", "Oferecer depois de (minutos)", "num"),
        ("downsell_percent", "Desconto (%)", "num"),
    ]),
    ("Upsell (oferta logo depois da compra)", [
        ("upsell_on", "Ativo", "bool"),
        ("upsell_plan_id", "Plano oferecido", "plan"),
        ("upsell_percent", "Desconto (%)", "num"),
    ]),
]


@protected
async def config(request: Request):
    if request.method == "POST":
        f = await form_data(request)
        for _, fields in FIELDS:
            for key, _, typ in fields:
                if typ == "bool":
                    set_setting(key, "1" if f.get(key) == "1" else "0")
                elif typ == "num":
                    val = re.sub(r"\D", "", f.get(key, ""))
                    set_setting(key, val or DEFAULTS[key])
                else:
                    set_setting(key, (f.get(key) or "").strip() or DEFAULTS[key])
        return back(request, "/config", "Configurações salvas.")

    owner = admin_id()
    owner_html = (f'<div class="flash">👑 Dono do bot: <code>{owner}</code></div>' if owner else
                  '<div class="flash">⚠️ Nenhum dono definido. No Telegram, mande para o bot: '
                  '<code>/admin SUA_SENHA_DO_PAINEL</code></div>')
    html_parts = [owner_html, f'<form class="box" method="post">{csrf(request)}']
    for title, fields in FIELDS:
        html_parts.append(f"<h2 style='margin:8px 0 0'>{title}</h2>")
        for key, label, typ in fields:
            val = get_setting(key)
            if typ == "area":
                html_parts.append(f'<label>{label}<textarea name="{key}">{esc(val)}</textarea></label>')
            elif typ == "num":
                html_parts.append(f'<label>{label}<input name="{key}" inputmode="numeric" value="{esc(val)}"></label>')
            elif typ == "bool":
                chk = " checked" if val == "1" else ""
                html_parts.append(f'<label style="display:flex;gap:8px;align-items:center">'
                                  f'<input type="checkbox" name="{key}" value="1"{chk} style="width:auto">{label}</label>')
            elif typ == "plan":
                html_parts.append(f'<label>{label}<select name="{key}">{plan_options(val, True)}</select></label>')
    html_parts.append("<button>Salvar</button></form>")
    return page(request, "Config", "".join(html_parts))


# ───────────────────────── Copy (textos + fotos/vídeos do funil) ─────────────────────────
async def store_upload(bot, up):
    """Manda o arquivo para o dono (o Telegram só guarda o que foi enviado) e devolve (tipo, file_id, miniatura)."""
    owner = admin_id()
    if not owner:
        raise RuntimeError("Defina o dono primeiro: mande /admin SUA_SENHA para o bot.")
    data = await up.read()
    if (up.content_type or "").startswith("video"):
        m = await bot.send_video(owner, data, filename=up.filename, disable_notification=True)
        res = ("video", m.video.file_id, m.video.thumbnail.file_id if m.video.thumbnail else "")
    else:
        m = await bot.send_photo(owner, data, disable_notification=True)
        res = ("photo", m.photo[-1].file_id, m.photo[0].file_id)
    try:
        await m.delete()
    except Exception:
        pass
    return res


_copy_thumbs: dict[str, bytes] = {}


@protected
async def copy_page(request: Request):
    parts = ['<h2>Copy do funil</h2><p class="muted">Texto e foto/vídeo de cada mensagem que o bot manda. '
             'Com mídia, o texto vira a legenda (até 1024 letras; se passar, vai em mensagem separada). '
             'Preços e planos ficam na aba <a href="/planos">Planos</a>; tempos e descontos em <a href="/config">Config</a>.</p>']
    for key, label in core.MEDIA_KEYS.items():
        m = core.get_media(key)
        if m:
            prev = (f'<img src="/copy/mini/{key}?v={esc(m[1][-8:])}" style="width:90px;height:90px;object-fit:cover;border-radius:10px">'
                    if m[2] else "")
            media_html = (f'<div style="display:flex;gap:10px;align-items:center">{prev}<span>{"🎬 vídeo" if m[0] == "video" else "🖼 foto"} anexado</span>'
                          f'<label style="display:flex;gap:6px;align-items:center;margin-left:auto"><input type="checkbox" name="tirar" value="1" style="width:auto">tirar</label></div>')
        else:
            media_html = '<span class="muted">Sem foto/vídeo</span>'
        text = get_setting(f"{key}_text")
        parts.append(f"""<form class="box" method="post" action="/copy/salvar" enctype="multipart/form-data" style="margin-bottom:12px">{csrf(request)}
<input type="hidden" name="key" value="{key}"><b>{esc(label)}</b>
<label>Texto<textarea name="texto">{esc(text)}</textarea></label>
{media_html}
<label>Trocar foto/vídeo (opcional)<input type="file" name="arquivo" accept="image/*,video/*"></label>
<button>Salvar</button></form>""")
    return page(request, "Copy", "".join(parts))


@protected
async def copy_save(request: Request):
    f = await form_data(request)
    key = f.get("key", "")
    if key not in core.MEDIA_KEYS:
        return back(request, "/copy")
    text = (f.get("texto") or "").strip()
    set_setting(f"{key}_text", text or DEFAULTS.get(f"{key}_text", ""))
    msg = "Salvo."
    up = f.get("arquivo")
    if f.get("tirar") == "1":
        set_setting(f"{key}_media", "")
    if up is not None and getattr(up, "filename", ""):
        try:
            kind, fid, thumb = await store_upload(request.app.state.tg.bot, up)
            set_setting(f"{key}_media", f"{kind}|{fid}|{thumb}")
            _copy_thumbs.pop(key, None)
            msg = "Salvo com a nova mídia."
        except Exception as e:
            msg = f"Texto salvo, mas a mídia falhou: {e}"
    return back(request, "/copy", msg)


@protected
async def copy_mini(request: Request):
    key = request.path_params["key"]
    m = core.get_media(key) if key in core.MEDIA_KEYS else None
    if not m or not m[2]:
        return Response(status_code=404)
    cache_key = f"{key}:{m[2]}"
    if cache_key not in _copy_thumbs:
        try:
            tf = await request.app.state.tg.bot.get_file(m[2])
            _copy_thumbs[cache_key] = bytes(await tf.download_as_bytearray())
        except Exception:
            return Response(status_code=404)
    return Response(_copy_thumbs[cache_key], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ───────────────────────── Postagem automática ─────────────────────────
_thumbs: dict[int, bytes] = {}


@protected
async def postagem(request: Request):
    if request.method == "POST":
        f = await form_data(request)
        set_setting("ap_on", "1" if f.get("ap_on") == "1" else "0")
        set_setting("ap_recycle", "1" if f.get("ap_recycle") == "1" else "0")
        mins = re.sub(r"\D", "", f.get("ap_minutes", ""))
        set_setting("ap_minutes", mins or "60")
        set_setting("ap_group", re.sub(r"[^\d-]", "", f.get("ap_group", "")))
        return back(request, "/postagem", "Postagem salva.")

    c = autopost.counts()
    on = autopost.setting("ap_on") == "1"
    rec = autopost.setting("ap_recycle") == "1"
    grp = autopost.target_group()
    last = int(autopost.setting("ap_last") or 0)
    mins = autopost.setting("ap_minutes")
    nxt = "—" if not on else ("agora" if now() - last >= int(mins or 60) * 60 else dt(last + int(mins or 60) * 60))

    def card(label, value):
        return f'<div class="card"><small>{label}</small><div class="v">{value}</div></div>'

    chk_on = " checked" if on else ""
    chk_rec = " checked" if rec else ""
    status_txt = "ligado ▶️" if on else "pausado ⏸"
    ag = autopost.setting("ap_group")
    grp_opts = group_options(int(ag) if ag.lstrip("-").isdigit() else None)
    grp_txt = grp or "nenhum"
    cards = card("Na fila", c["fila"]) + card("Já postados", c["postados"]) + card("Status", status_txt) + card("Próximo post", nxt)

    rows = db.execute("SELECT * FROM posts ORDER BY (posted_at IS NOT NULL), id DESC LIMIT 120").fetchall()
    items = ""
    for r in rows:
        mini = (f'<img src="/postagem/mini/{r["id"]}" loading="lazy" style="width:56px;height:56px;object-fit:cover;border-radius:8px">'
                if r["thumb_id"] else "🎬" if r["kind"] == "video" else "🖼")
        st = ('<span class="pill pend">na fila</span>' if r["posted_at"] is None
              else f'<span class="pill ok">saiu {r["times"]}x</span>')
        items += (f"<tr><td>{mini}</td><td>{'vídeo' if r['kind'] == 'video' else 'foto'}</td><td>{st}</td>"
                  f"<td style='white-space:normal;min-width:140px'>{esc(r['caption'][:80]) or '<span class=muted>sem legenda</span>'}</td>"
                  f"<td><form method='post' action='/postagem/apagar' class='inline'>{csrf(request)}"
                  f"<input type='hidden' name='id' value='{r['id']}'><button class='sm red'>Apagar</button></form></td></tr>")
    table = (f'<div class="tw"><table><tr><th></th><th>Tipo</th><th>Status</th><th>Legenda</th><th></th></tr>{items}</table></div>'
             if items else '<p class="muted">Nada ainda. Envie abaixo ou mande fotos/vídeos no privado do bot.</p>')

    body = f"""<h2>Postagem automática</h2>
<div class="grid">{cards}</div>

<h2>Enviar fotos e vídeos</h2>
<form class="box" method="post" action="/postagem/enviar" enctype="multipart/form-data">{csrf(request)}
<label>Arquivos (pode escolher vários; vídeo até 50 MB)<input type="file" name="arquivos" multiple accept="image/*,video/*" required></label>
<label>Legenda (opcional, vale para todos)<textarea name="legenda" style="min-height:60px"></textarea></label>
<button>Colocar na fila</button></form>

<h2>Ajustes</h2>
<form class="box" method="post">{csrf(request)}
<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="ap_on" value="1"{chk_on} style="width:auto">Postar sozinho</label>
<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="ap_recycle" value="1"{chk_rec} style="width:auto">Quando a fila acabar, repostar o que já saiu (nunca o mesmo duas vezes seguidas)</label>
<div class="row"><label>Intervalo (minutos)<input name="ap_minutes" inputmode="numeric" value="{esc(mins)}"></label>
<label>Grupo/canal de destino<select name="ap_group"><option value="">Grupo do primeiro plano</option>{grp_opts}</select></label></div>
<button>Salvar</button></form>
<form method="post" action="/postagem/agora" style="margin-top:10px">{csrf(request)}<button class="sec" style="width:100%">📤 Postar o próximo agora</button></form>
<p class="muted">Destino atual: <code>{grp_txt}</code></p>

<h2>Fila e histórico</h2>{table}"""
    return page(request, "Postagem", body)


@protected
async def postagem_enviar(request: Request):
    f = await form_data(request)
    if not admin_id():
        return back(request, "/postagem", "Defina o dono primeiro: mande /admin SUA_SENHA para o bot.")
    bot = request.app.state.tg.bot
    caption = (f.get("legenda") or "").strip()
    ok = fail = 0
    for up in f.getlist("arquivos"):
        if not getattr(up, "filename", ""):
            continue
        try:
            kind, fid, thumb = await store_upload(bot, up)
            autopost.add(kind, fid, caption, thumb or None)
            ok += 1
        except Exception as e:  # arquivo grande demais, formato inválido…
            log.warning("upload falhou (%s): %s", up.filename, e)
            fail += 1
    msg = f"{ok} arquivo(s) na fila." + (f" {fail} falharam (vídeo acima de 50 MB?)." if fail else "")
    return back(request, "/postagem", msg)


@protected
async def postagem_apagar(request: Request):
    f = await form_data(request)
    db.execute("DELETE FROM posts WHERE id=?", (int(f.get("id") or 0),))
    db.commit()
    return back(request, "/postagem", "Apagado.")


@protected
async def postagem_agora(request: Request):
    await form_data(request)
    try:
        msg = await autopost.post_now(request.app.state.tg.bot)
    except Exception as e:
        msg = f"Erro ao postar: {e}"
    return back(request, "/postagem", msg)


@protected
async def postagem_mini(request: Request):
    pid = int(request.path_params["pid"])
    if pid not in _thumbs:
        row = db.execute("SELECT thumb_id FROM posts WHERE id=?", (pid,)).fetchone()
        if not row or not row["thumb_id"]:
            return Response(status_code=404)
        try:
            tf = await request.app.state.tg.bot.get_file(row["thumb_id"])
            _thumbs[pid] = bytes(await tf.download_as_bytearray())
        except Exception:
            return Response(status_code=404)
        if len(_thumbs) > 300:
            _thumbs.pop(next(iter(_thumbs)))
    return Response(_thumbs[pid], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


# ───────────────────────── Webhook SyncPay ─────────────────────────
async def webhook_syncpay(request: Request):
    """Aviso instantâneo do SyncPay. Não confiamos no conteúdo: só usamos o id e consultamos a API."""
    if WEBHOOK_TOKEN:
        auth = request.headers.get("authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {WEBHOOK_TOKEN}"):
            return JSONResponse({"ok": False}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    ext_id = data.get("id") or data.get("identifier") or data.get("reference_id")
    row = db.execute("SELECT id FROM payments WHERE ext_id=? AND provider='syncpay'", (str(ext_id),)).fetchone()
    if row:
        asyncio.create_task(core.process_payment(request.app.state.tg.bot, row["id"]))
    return JSONResponse({"ok": True})


# ───────────────────────── App ─────────────────────────
def create_app(tg_application) -> Starlette:
    app = Starlette(
        routes=[
            Route("/entrar", login, methods=["GET", "POST"]),
            Route("/sair", logout),
            Route("/", home),
            Route("/vendas", sales),
            Route("/vendas/verificar", sales_check, methods=["POST"]),
            Route("/membros", members),
            Route("/membros/adicionar", members_add, methods=["POST"]),
            Route("/membros/dias", members_days, methods=["POST"]),
            Route("/membros/remover", members_remove, methods=["POST"]),
            Route("/planos", plans),
            Route("/planos/salvar", plans_save, methods=["POST"]),
            Route("/planos/alternar", plans_toggle, methods=["POST"]),
            Route("/links", links),
            Route("/disparo", broadcast, methods=["GET", "POST"]),
            Route("/config", config, methods=["GET", "POST"]),
            Route("/webhook/syncpay", webhook_syncpay, methods=["POST"]),
            Route("/postleve", postleve, methods=["GET", "POST"]),
            Route("/copy", copy_page),
            Route("/copy/salvar", copy_save, methods=["POST"]),
            Route("/copy/mini/{key}", copy_mini),
            Route("/postagem", postagem, methods=["GET", "POST"]),
            Route("/postagem/enviar", postagem_enviar, methods=["POST"]),
            Route("/postagem/apagar", postagem_apagar, methods=["POST"]),
            Route("/postagem/agora", postagem_agora, methods=["POST"]),
            Route("/postagem/mini/{pid:int}", postagem_mini),
            *api.ROUTES,
        ],
        middleware=[Middleware(SessionMiddleware, secret_key=panel_secret(), session_cookie="levevip",
                               max_age=30 * 86400, same_site="strict",
                               https_only=gw.PUBLIC_URL.startswith("https"))],
    )
    app.state.tg = tg_application
    return app
