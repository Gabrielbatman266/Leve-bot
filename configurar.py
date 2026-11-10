"""Assistente de configuração do Leve VIP: pergunta tudo, confere se está certo e grava o .env."""

import os
import secrets
import string
import sys

import httpx

ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

B, G, R, Y, N = "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def load_env() -> dict:
    data = {}
    if os.path.exists(ENV):
        for line in open(ENV, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
    return data


def ask(label: str, default: str = "", secret_hint: bool = False) -> str:
    shown = default
    if secret_hint and default:
        shown = default[:4] + "…" + default[-4:]
    suffix = f" [{shown}]" if default else ""
    try:
        val = input(f"{B}{label}{N}{suffix}: ").strip()
    except EOFError:
        print()
        sys.exit("Instalação cancelada.")
    return val or default


def check_bot(token: str) -> str | None:
    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
        if r.status_code == 200 and r.json().get("ok"):
            return r.json()["result"]["username"]
    except Exception:
        pass
    return None


def check_syncpay(cid: str, secret: str) -> bool | None:
    """True = ok, False = chave errada, None = sem conexão."""
    try:
        r = httpx.post(
            "https://api.syncpayments.com.br/api/partner/v1/auth-token",
            json={"client_id": cid, "client_secret": secret},
            headers={"Accept": "application/json"},
            timeout=20,
        )
        return r.status_code == 200 and "access_token" in r.json()
    except httpx.HTTPError:
        return None
    except ValueError:
        return False


def check_stripe(key: str) -> bool:
    try:
        r = httpx.get("https://api.stripe.com/v1/balance", auth=(key, ""), timeout=15)
        return r.status_code == 200
    except Exception:
        return False


def parse_price(text: str) -> int | None:
    t = text.replace("R$", "").strip()
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        cents = round(float(t) * 100)
        return cents if cents >= 100 else None
    except ValueError:
        return None


def main() -> None:
    env = load_env()
    print(f"\n{B}👑 Configuração do Leve VIP{N}")
    print("Aperte Enter para manter o valor entre [colchetes].\n")

    # 1. Bot do Telegram
    print(f"{Y}1/4 · Bot do Telegram{N} — no @BotFather mande /newbot e copie o token.")
    while True:
        token = ask("Token do bot", env.get("BOT_TOKEN", ""), True)
        username = check_bot(token) if token else None
        if username:
            print(f"{G}✔ Bot encontrado: @{username}{N}\n")
            break
        print(f"{R}✘ Token inválido (ou sem internet). Confira e cole de novo.{N}")

    # 2. SyncPay
    print(f"{Y}2/4 · SyncPay (Pix){N} — painel SyncPay → Developer → API → gerar chave (deixe 'Saque' DESMARCADO).")
    while True:
        cid = ask("ID público (client_id)", env.get("SYNCPAY_CLIENT_ID", ""))
        csec = ask("ID privado (client_secret)", env.get("SYNCPAY_CLIENT_SECRET", ""), True)
        ok = check_syncpay(cid, csec) if cid and csec else False
        if ok:
            print(f"{G}✔ SyncPay conectado!{N}\n")
            break
        if ok is None:
            print(f"{Y}! Não consegui falar com o SyncPay agora (internet?). Vou salvar assim mesmo.{N}\n")
            break
        print(f"{R}✘ O SyncPay recusou essas chaves. Lembre que a conta precisa estar aprovada.{N}")
        if ask("Tentar de novo? (s/n)", "s").lower().startswith("n"):
            print(f"{Y}! Salvando assim mesmo. Corrija depois com: levebot configurar{N}\n")
            break

    # 3. Painel
    print(f"{Y}3/4 · Painel{N} — senha para entrar no painel (e para virar dono do bot com /admin).")
    default_pw = env.get("PANEL_PASSWORD") or "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))
    while True:
        pw = ask("Senha do painel", default_pw)
        if len(pw) >= 6 and " " not in pw:
            break
        print(f"{R}✘ Use pelo menos 6 caracteres, sem espaço.{N}")
    print(f"{G}✔ Senha: {pw}{N}  (anote!)\n")

    # 4. Primeiro plano
    print(f"{Y}4/4 · Primeiro plano{N} — é criado sozinho quando você adicionar o bot ao grupo. Dá para mudar no painel.")
    name = ask("Nome do plano", env.get("PLAN_NAME", "VIP Mensal"))
    while True:
        cur = env.get("PRICE_BRL")
        price = parse_price(ask("Preço em R$", f"{int(cur) / 100:.2f}".replace(".", ",") if cur else "29,90"))
        if price:
            break
        print(f"{R}✘ Valor inválido (mínimo R$ 1,00).{N}")
    while True:
        days = ask("Duração em dias (0 = vitalício)", env.get("PLAN_DAYS", "30"))
        if days.isdigit():
            break

    # Opcional: Stripe
    stripe_key = env.get("STRIPE_SECRET_KEY", "")
    print(f"\n{Y}Opcional · Stripe{N} — só se quiser vender em dólar/euro no cartão. Enter para pular.")
    k = ask("Chave secreta do Stripe (sk_live_…)", stripe_key, True)
    if k and k != stripe_key:
        print(f"{G}✔ Stripe ok{N}" if check_stripe(k) else f"{Y}! Não consegui validar a chave do Stripe.{N}")
    stripe_key = k

    lines = [
        "# Gerado por configurar.py — para mudar, rode: levebot configurar",
        f"BOT_TOKEN={token}",
        f"SYNCPAY_CLIENT_ID={cid}",
        f"SYNCPAY_CLIENT_SECRET={csec}",
        f"PANEL_PASSWORD={pw}",
        f"PANEL_PORT={env.get('PANEL_PORT', '8080')}",
        f"PANEL_HOST={env.get('PANEL_HOST', '127.0.0.1')}",
        f"STRIPE_SECRET_KEY={stripe_key}",
        f"PUBLIC_URL={env.get('PUBLIC_URL', '')}",
        f"SYNCPAY_WEBHOOK_TOKEN={env.get('SYNCPAY_WEBHOOK_TOKEN', '')}",
        f"PLAN_NAME={name}",
        f"PLAN_DAYS={days}",
        f"PRICE_BRL={price}",
    ]
    if env.get("ADMIN_ID"):
        lines.append(f"ADMIN_ID={env['ADMIN_ID']}")
    with open(ENV, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(ENV, 0o600)

    print(f"\n{G}{B}✔ Configuração salva!{N}")
    print(f"""
{B}Agora, no Telegram:{N}
  1. Abra @{username} e mande:  /admin {pw}
  2. Adicione @{username} como ADMINISTRADOR do seu grupo/canal VIP
     (permissões: convidar usuários + banir usuários)
O bot detecta o grupo sozinho e já abre as vendas.
""")


if __name__ == "__main__":
    main()
