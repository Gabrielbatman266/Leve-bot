"""Banco de dados (SQLite) e configurações do Leve VIP."""

import os
import secrets
import sqlite3
import time

DB_PATH = os.environ.get("DB_PATH", "leve_vip.db")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row

db.executescript(
    """
    -- Planos à venda. days = 0 significa vitalício.
    CREATE TABLE IF NOT EXISTS plans (
        id        INTEGER PRIMARY KEY,
        name      TEXT    NOT NULL,
        days      INTEGER NOT NULL DEFAULT 30,
        price_brl INTEGER NOT NULL,          -- centavos
        price_usd INTEGER,                   -- centavos (opcional, Stripe)
        price_eur INTEGER,                   -- centavos (opcional, Stripe)
        group_id  INTEGER NOT NULL,          -- grupo/canal liberado
        active    INTEGER NOT NULL DEFAULT 1,
        sort      INTEGER NOT NULL DEFAULT 0
    );

    -- Todo mundo que deu /start (com a origem do link ?start=)
    CREATE TABLE IF NOT EXISTS leads (
        user_id    INTEGER PRIMARY KEY,
        username   TEXT,
        first_name TEXT,
        source     TEXT,
        created_at INTEGER NOT NULL,
        blocked    INTEGER NOT NULL DEFAULT 0  -- bloqueou o bot
    );

    CREATE TABLE IF NOT EXISTS payments (
        id         INTEGER PRIMARY KEY,
        ext_id     TEXT UNIQUE,              -- id no gateway
        provider   TEXT NOT NULL,            -- syncpay | stripe | manual
        user_id    INTEGER NOT NULL,
        plan_id    INTEGER,
        currency   TEXT NOT NULL,
        amount     INTEGER NOT NULL,         -- centavos
        kind       TEXT NOT NULL DEFAULT 'normal',  -- normal | downsell | upsell
        status     TEXT NOT NULL DEFAULT 'pending', -- pending | paid | expired
        created_at INTEGER NOT NULL,
        paid_at    INTEGER,
        followup   INTEGER NOT NULL DEFAULT 0       -- 0 nada, 1 lembrete, 2 downsell
    );

    -- Acesso de cada pessoa a cada grupo. expires_at NULL = vitalício.
    CREATE TABLE IF NOT EXISTS members (
        user_id    INTEGER NOT NULL,
        group_id   INTEGER NOT NULL,
        plan_id    INTEGER,
        expires_at INTEGER,
        reminded   INTEGER NOT NULL DEFAULT 0,
        removed    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, group_id)
    );

    -- Grupos/canais onde o bot foi adicionado (detectados sozinhos)
    CREATE TABLE IF NOT EXISTS groups (
        id         INTEGER PRIMARY KEY,
        title      TEXT,
        type       TEXT,
        is_admin   INTEGER NOT NULL DEFAULT 0,
        can_invite INTEGER NOT NULL DEFAULT 0,
        can_ban    INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """
)
db.commit()

# Valores padrão (editáveis no painel)
DEFAULTS = {
    "welcome_text": "👑 Bem-vindo ao VIP!\n\nEscolha o seu plano abaixo:",
    "paid_text": "✅ Pagamento confirmado! Seja bem-vindo(a) ao VIP 💛",
    "expired_text": "Seu acesso VIP terminou. Para voltar, é só tocar em /start 💛",
    "remind_days": "3",
    # Remarketing de Pix gerado e não pago
    "remarketing_on": "1",
    "remarketing_minutes": "15",
    "remarketing_text": "⏳ Seu Pix ainda está esperando! Ele vale por pouco tempo — é só copiar o código e pagar.",
    "downsell_on": "1",
    "downsell_minutes": "60",
    "downsell_percent": "20",
    "downsell_text": "🎁 Ainda dá tempo! Liberei um desconto especial só para você:",
    # Upsell depois da compra
    "upsell_on": "0",
    "upsell_plan_id": "",
    "upsell_percent": "30",
    "upsell_text": "🔥 Oferta única para quem acabou de entrar:",
}


def get_setting(key: str) -> str:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULTS.get(key, "")


def set_setting(key: str, value: str) -> None:
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    db.commit()


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(get_setting(key))
    except ValueError:
        return default


def panel_secret() -> str:
    """Segredo para assinar o login do painel (gerado uma vez e guardado)."""
    s = get_setting("_panel_secret")
    if not s:
        s = secrets.token_hex(32)
        set_setting("_panel_secret", s)
    return s


def seed_from_env(group: int | str | None = None) -> bool:
    """Cria o primeiro plano (preço e dias do .env) se ainda não existir nenhum. Retorna True se criou."""
    if db.execute("SELECT 1 FROM plans LIMIT 1").fetchone():
        return False
    group = group or os.environ.get("GROUP_ID")
    if not group:
        return False
    usd = os.environ.get("PRICE_USD")
    eur = os.environ.get("PRICE_EUR")
    db.execute(
        "INSERT INTO plans (name, days, price_brl, price_usd, price_eur, group_id) VALUES (?, ?, ?, ?, ?, ?)",
        (
            os.environ.get("PLAN_NAME", "VIP Mensal"),
            int(os.environ.get("PLAN_DAYS", "30")),
            int(os.environ.get("PRICE_BRL", "2990")),
            int(usd) if usd else None,
            int(eur) if eur else None,
            int(group),
        ),
    )
    db.commit()
    return True


def admin_id() -> int:
    """Dono do bot: definido com /admin <senha> (ou ADMIN_ID no .env)."""
    v = get_setting("admin_id") or os.environ.get("ADMIN_ID", "")
    try:
        return int(v)
    except ValueError:
        return 0


def now() -> int:
    return int(time.time())
