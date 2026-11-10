#!/usr/bin/env bash
# Mantém o bot rodando: se cair, volta sozinho em 5 segundos.
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$DIR" || exit 1
PY="$(cat .python 2>/dev/null || echo python)"

while true; do
  # log não passa de ~2 MB
  if [ -f bot.log ] && [ "$(wc -c < bot.log)" -gt 2000000 ]; then
    tail -c 500000 bot.log > bot.log.tmp && mv bot.log.tmp bot.log
  fi
  "$PY" "$DIR/main.py" >> bot.log 2>&1
  code=$?
  [ "$code" = 3 ] && { echo "Erro de configuração — rode: levebot configurar" >> bot.log; exit 3; }
  echo "$(date '+%d/%m %H:%M:%S') bot parou (código $code), reiniciando em 5 s…" >> bot.log
  sleep 5
done
