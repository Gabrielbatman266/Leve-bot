#!/usr/bin/env bash
# Instalador do Leve VIP — no Termux, rode:
#   curl -fsSL https://raw.githubusercontent.com/Gabrielbatman266/Leve-bot/main/install.sh | bash
set -e

REPO="https://github.com/Gabrielbatman266/Leve-bot.git"
DIR="$HOME/leve-bot"
G="\033[32m"; Y="\033[33m"; B="\033[1m"; N="\033[0m"

echo -e "\n${B}👑 Instalando o Leve VIP…${N}\n"

if [ -d /data/data/com.termux ]; then
  TERMUX=1
  echo -e "${Y}▸ Instalando Python e Git (pode demorar uns minutos)…${N}"
  pkg update -y -o Dpkg::Options::="--force-confnew" >/dev/null 2>&1 || pkg update -y
  pkg install -y -o Dpkg::Options::="--force-confnew" python python-pillow git procps >/dev/null 2>&1 \
    || pkg install -y python python-pillow git procps
  BIN="$PREFIX/bin"
else
  TERMUX=0
  for c in git python3; do
    command -v $c >/dev/null || { echo "Instale o $c primeiro (ex.: sudo apt install git python3 python3-venv)"; exit 1; }
  done
  BIN="$HOME/.local/bin"; mkdir -p "$BIN"
fi

echo -e "${Y}▸ Baixando o bot…${N}"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only -q
else
  git clone -q --depth 1 "$REPO" "$DIR"
fi
cd "$DIR"

echo -e "${Y}▸ Instalando as bibliotecas…${N}"
if [ "$TERMUX" = 1 ]; then
  PY=python
  pip install -q -r requirements.txt 2>/dev/null || pip install -q --break-system-packages -r requirements.txt
else
  [ -d .venv ] || python3 -m venv .venv
  PY="$DIR/.venv/bin/python"
  "$PY" -m pip install -q -r requirements.txt
fi
echo "$PY" > .python

chmod +x levebot rodar.sh
ln -sf "$DIR/levebot" "$BIN/levebot"

if [ "$TERMUX" = 1 ]; then
  # Liga sozinho quando o celular reinicia (precisa do app Termux:Boot)
  mkdir -p "$HOME/.termux/boot"
  cat > "$HOME/.termux/boot/leve-bot.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
$DIR/levebot iniciar
EOF
  chmod +x "$HOME/.termux/boot/leve-bot.sh"
fi

if [ ! -f .env ]; then
  "$PY" configurar.py < /dev/tty
fi

"$DIR/levebot" reiniciar

echo -e "
${G}${B}✔ Leve VIP instalado e rodando!${N}

  Painel:   abra ${B}http://localhost:8080${N} no navegador
  Comandos: ${B}levebot${N} (mostra todos)
"
if [ "$TERMUX" = 1 ]; then
  echo -e "${Y}Para ligar sozinho quando o celular reiniciar:${N} instale o app ${B}Termux:Boot${N} (F-Droid) e abra ele uma vez.
${Y}No Xiaomi:${N} Configurações → Apps → Termux → Economia de bateria → ${B}Sem restrições${N}
"
fi
