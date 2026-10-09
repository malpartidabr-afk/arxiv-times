#!/bin/sh
# Instala Arxiv Times en macOS para que arranque solo al iniciar sesión.
# Uso: ./install.sh [puerto]   (por defecto 8000)
set -e

PORT="${1:-8000}"
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="local.arxiv-times"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/arxiv-times.log"

if [ "$(uname)" != "Darwin" ]; then
  echo "Este instalador es para macOS. En Linux corré: python3 $DIR/arxiv_times.py"
  exit 1
fi

PY="$(command -v python3 || true)"
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
  echo "Necesitás Python 3.9 o más nuevo. En macOS se instala con: xcode-select --install"
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DIR/arxiv_times.py</string>
    <string>--no-browser</string>
    <string>--port</string>
    <string>$PORT</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

printf '#!/bin/sh\nopen http://localhost:%s\n' "$PORT" > "$DIR/Abrir Arxiv Times.command"
chmod +x "$DIR/Abrir Arxiv Times.command"

echo "Listo: Arxiv Times corre en http://localhost:$PORT y va a arrancar solo cada vez que inicies sesión."
echo "Si movés esta carpeta, volvé a correr ./install.sh"
sleep 1
open "http://localhost:$PORT"
