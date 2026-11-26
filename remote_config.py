"""Configuração remota: o arquivo config_remota.json do GitHub vira configuração do bot.

Quando o arquivo muda (o bot se atualiza sozinho a cada 10 min), cada seção nova é aplicada
uma vez. O que você mudar depois no painel continua valendo até a próxima mudança no arquivo.

Seções (todas opcionais; o que não estiver no arquivo não é mexido):
  "textos":   {"welcome_text": "...", "remarketing_text": "...", ...}
  "config":   {"remarketing_minutes": "15", "downsell_percent": "20", ...}
  "postagem": {"ap_on": "1", "ap_minutes": "60", "ap_recycle": "1"}
  "planos":   [{"id": 1, "nome": "VIP Mensal", "dias": 30, "preco": "19,90", "ativo": true}]
              sem "id" = plano novo (vai para o mesmo grupo do primeiro plano, ou "grupo": -100…)

Para ver a configuração atual (sem senhas):  levebot exportar
"""

import hashlib
import json
import logging
import os
import sys

from db import DEFAULTS, db, get_setting, set_setting

log = logging.getLogger("levevip.remoto")

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config_remota.json")
POST_KEYS = ["ap_on", "ap_minutes", "ap_recycle", "ap_group"]


def _price(v) -> int | None:
    """'19,90' / 19.9 / 1990 (centavos se int grande) -> centavos."""
    if v is None or v == "":
        return None
    if isinstance(v, int):
        return v if v >= 100 else v * 100
    t = str(v).replace("R$", "").replace("US$", "").replace("€", "").strip()
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    return round(float(t) * 100)


def _changed(section: str, value) -> bool:
    h = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    key = f"_remote_{section}"
    if get_setting(key) == h:
        return False
    set_setting(key, h)
    return True


def _apply_plans(plans: list) -> int:
    first = db.execute("SELECT group_id FROM plans ORDER BY sort, id LIMIT 1").fetchone()
    n = 0
    for i, p in enumerate(plans):
        fields = {}
        if "nome" in p:
            fields["name"] = str(p["nome"])
        if "dias" in p:
            fields["days"] = int(p["dias"])
        if "preco" in p:
            fields["price_brl"] = _price(p["preco"])
        if "preco_usd" in p:
            fields["price_usd"] = _price(p["preco_usd"])
        if "preco_eur" in p:
            fields["price_eur"] = _price(p["preco_eur"])
        if "ativo" in p:
            fields["active"] = 1 if p["ativo"] else 0
        if "grupo" in p:
            fields["group_id"] = int(p["grupo"])
        if "ordem" in p:
            fields["sort"] = int(p["ordem"])
        pid = p.get("id")
        if not pid and "name" in fields:  # sem id: procura pelo nome para não duplicar
            row = db.execute("SELECT id FROM plans WHERE name=?", (fields["name"],)).fetchone()
            pid = row["id"] if row else None
        if pid and db.execute("SELECT 1 FROM plans WHERE id=?", (int(pid),)).fetchone():
            if fields:
                sets = ", ".join(f"{k}=?" for k in fields)
                db.execute(f"UPDATE plans SET {sets} WHERE id=?", (*fields.values(), int(pid)))
                n += 1
        else:
            fields.setdefault("group_id", first["group_id"] if first else None)
            if not fields.get("group_id") or not fields.get("price_brl") or "name" not in fields:
                log.warning("Plano novo #%s ignorado: precisa de nome, preco e um grupo.", i + 1)
                continue
            fields.setdefault("days", 30)
            fields.setdefault("sort", i)
            cols = ", ".join(fields)
            db.execute(f"INSERT INTO plans ({cols}) VALUES ({', '.join('?' * len(fields))})", tuple(fields.values()))
            n += 1
    db.commit()
    return n


def apply() -> None:
    """Chamado ao ligar o bot."""
    if not os.path.exists(PATH):
        return
    try:
        with open(PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        log.error("config_remota.json inválido, ignorado: %s", e)
        return

    applied = []
    for section, allowed in (("textos", None), ("config", None), ("postagem", POST_KEYS)):
        values = cfg.get(section) or {}
        if values and _changed(section, values):
            for k, v in values.items():
                if allowed is not None and k not in allowed:
                    continue
                if allowed is None and k not in DEFAULTS:
                    log.warning("Chave desconhecida em %s: %s", section, k)
                    continue
                set_setting(k, str(int(v) if isinstance(v, bool) else v))
            applied.append(section)
    plans = cfg.get("planos") or []
    if plans and _changed("planos", plans):
        try:
            applied.append(f"planos ({_apply_plans(plans)})")
        except (ValueError, TypeError, KeyError) as e:
            log.error("Erro nos planos do config_remota.json: %s", e)
    if applied:
        log.info("Configuração remota aplicada: %s", ", ".join(applied))


def export() -> dict:
    """Configuração atual, no mesmo formato do arquivo (sem segredos)."""
    from autopost import DEFAULTS as AP_DEFAULTS
    textos = {k: get_setting(k) for k in DEFAULTS if k.endswith("_text")}
    config = {k: get_setting(k) for k in DEFAULTS if not k.endswith("_text")}
    postagem = {k: (get_setting(k) or AP_DEFAULTS.get(k, "")) for k in POST_KEYS}
    planos = [
        {"id": r["id"], "nome": r["name"], "dias": r["days"],
         "preco": f"{r['price_brl'] / 100:.2f}".replace(".", ","), "ativo": bool(r["active"]), "grupo": r["group_id"]}
        for r in db.execute("SELECT * FROM plans ORDER BY sort, id")
    ]
    return {"textos": textos, "config": config, "postagem": postagem, "planos": planos}


if __name__ == "__main__" and sys.argv[1:] == ["exportar"]:
    print(json.dumps(export(), ensure_ascii=False, indent=2))
