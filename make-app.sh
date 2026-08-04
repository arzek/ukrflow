#!/bin/zsh
# Збирає UkrFlow.app — легкий бандл-обгортку навколо наявного venv.
# Код (ukrflow.py) лишається в цій теці, а не всередині .app: тому правки коду
# НЕ скидають дозволи macOS і не потребують перезбірки застосунку.
# Використання: ./make-app.sh   (далі ./install-app.sh для встановлення)
set -e
cd "$(dirname "$0")"

[ "$(uname -m)" = "arm64" ] || { echo "❌ Потрібен Apple Silicon (M1+)"; exit 1; }
[ -x ".venv/bin/python" ] || { echo "❌ Немає .venv — спершу ./install.sh"; exit 1; }

VERSION="1.0"
BUNDLE_ID="com.ukrflow.app"
# Фізичний шлях без симлінків — саме його впишемо в лаунчер бандла.
PROJ="$(pwd -P)"
APP="dist/UkrFlow.app"

echo "▸ Проєкт: $PROJ"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ── Info.plist ─────────────────────────────────────────────────────────────
cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>UkrFlow</string>
    <key>CFBundleDisplayName</key><string>UkrFlow</string>
    <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
    <key>CFBundleExecutable</key><string>UkrFlow</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <!-- Меню-бар застосунок: без іконки в Dock і в перемикачі -->
    <key>LSUIElement</key><true/>
    <!-- Текст у діалозі дозволу на мікрофон -->
    <key>NSMicrophoneUsageDescription</key>
    <string>UkrFlow слухає мікрофон під час утримання гарячої клавіші для диктування.</string>
</dict>
</plist>
EOF

# ── Лаунчер (головний виконуваний файл бандла) ─────────────────────────────
# LaunchServices/Login Items стартують із мінімальним PATH — тож codex/claude
# з ~/.local/bin треба прописати явно, інакше хмарне шліфування їх не знайде.
cat > "$APP/Contents/MacOS/UkrFlow" <<EOF
#!/bin/zsh
export PATH="\$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
PROJ="$PROJ"
cd "\$PROJ"
exec "\$PROJ/.venv/bin/python" "\$PROJ/ukrflow.py" >>"\$PROJ/launchd.log" 2>&1
EOF
chmod +x "$APP/Contents/MacOS/UkrFlow"

# ── Ad-hoc підпис ──────────────────────────────────────────────────────────
# Стабільна ідентичність бандла: дозволи macOS (Мікрофон / Моніторинг вводу /
# Accessibility) прив'язуються до UkrFlow.app і зберігаються, поки не міняється
# сам бандл. Правки ukrflow.py на нього не впливають.
codesign --force --sign - "$APP"

echo "✅ Зібрано: $APP"
echo "   Далі: ./install-app.sh  (перенесе в /Applications і ввімкне автозапуск)"
