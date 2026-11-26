#!/usr/bin/env bash
# Mantém o bot rodando: se cair, volta sozinho em 5 segundos.
# Também confere o GitHub a cada 10 minutos e, se tiver versão nova, atualiza e reinicia sozinho.
# Para desligar a atualização automática, ponha AUTO_UPDATE=0 no .env.
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$DIR" || exit 1
PY="$(cat .python 2>/dev/null || echo python)"
AUTO_UPDATE="$(grep -s '^AUTO_UPDATE=' .env | cut -d= -f2 | tr -d ' ')"
CHECK_EVERY=600

log() { echo "$(date '+%d/%m %H:%M:%S') $*" >> bot.log; }

has_update() {
  [ "$AUTO_UPDATE" = 0 ] && return 1
  [ -d .git ] || return 1
  git fetch -q origin 2>/dev/null || return 1
  local branch; branch="$(git rev-parse --abbrev-ref HEAD)"
  [ "$(git rev-parse HEAD)" != "$(git rev-parse "origin/$branch" 2>/dev/null)" ]
}

update() {
  log "versão nova no GitHub, atualizando…"
  local before; before="$(git rev-parse HEAD)"
  if git pull -q --ff-only; then
    if ! git diff --quiet "$before" HEAD -- requirements.txt; then
      "$PY" -m pip install -q -r requirements.txt >> bot.log 2>&1 \
        || "$PY" -m pip install -q --break-system-packages -r requirements.txt >> bot.log 2>&1
    fi
    log "atualizado para $(git rev-parse --short HEAD)"
  else
    log "não consegui atualizar (git pull falhou), sigo na versão atual"
  fi
}

while true; do
  # log não passa de ~2 MB
  if [ -f bot.log ] && [ "$(wc -c < bot.log)" -gt 2000000 ]; then
    tail -c 500000 bot.log > bot.log.tmp && mv bot.log.tmp bot.log
  fi
  "$PY" "$DIR/main.py" >> bot.log 2>&1 &
  BOT=$!
  waited=0
  while kill -0 "$BOT" 2>/dev/null; do
    sleep 5; waited=$((waited + 5))
    if [ "$waited" -ge "$CHECK_EVERY" ]; then
      waited=0
      if has_update; then
        update
        kill "$BOT" 2>/dev/null; wait "$BOT" 2>/dev/null
        log "reiniciando com a versão nova…"
        continue 2
      fi
    fi
  done
  wait "$BOT"; code=$?
  [ "$code" = 3 ] && { echo "Erro de configuração — rode: levebot configurar" >> bot.log; exit 3; }
  log "bot parou (código $code), reiniciando em 5 s…"
  sleep 5
done
