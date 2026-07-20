#!/bin/zsh
# Автозапуск UkrFlow при вході в систему (LaunchAgent для launchd).
# Використання: ./install-autostart.sh   Вимкнення: ./uninstall-autostart.sh
set -e
cd "$(dirname "$0")"

LABEL="com.ukrflow.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
# Фізичний шлях (без симлінків): у plist мають потрапити реальні файли.
DIR="$(pwd -P)"

# launchd не може запускати агенти з тек, захищених TCC: системному
# xpcproxy заборонено читати їх вміст (posix_spawn → Operation not permitted).
case "$DIR" in
    "$HOME/Desktop/"*|"$HOME/Documents/"*|"$HOME/Downloads/"*)
        echo "❌ Проєкт лежить у захищеній теці ($DIR) — автозапуск звідси неможливий."
        echo "   Перенесіть його, напр.: mv <тека> ~/ukrflow && ln -s ~/ukrflow <тека>"
        exit 1;;
esac

# launchd не читає профіль шелу — шлях до claude CLI треба прописати явно,
# інакше бекенд claude-code не знайде виконуваний файл.
CLAUDE_BIN_DIR=""
command -v claude >/dev/null && CLAUDE_BIN_DIR="$(dirname "$(command -v claude)"):"

# Два екземпляри б'ються за гарячі клавіші й мікрофон — ручний зупиняємо.
if pgrep -f "[Pp]ython.* ukrflow\.py" >/dev/null; then
    echo "⏹  Зупиняю екземпляр, запущений вручну…"
    pkill -f "[Pp]ython.* ukrflow\.py" || true
    sleep 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$DIR/.venv/bin/python</string>
        <string>ukrflow.py</string>
    </array>
    <key>WorkingDirectory</key><string>$DIR</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key><false/>
    </dict>
    <key>ProcessType</key><string>Interactive</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>$CLAUDE_BIN_DIR/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>StandardOutPath</key><string>$DIR/launchd.log</string>
    <key>StandardErrorPath</key><string>$DIR/launchd.log</string>
</dict>
</plist>
EOF

# Якщо агент уже був зареєстрований — перереєстровуємо з новим plist.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "✅ Автозапуск увімкнено: UkrFlow запущено і стартуватиме при вході в систему."
echo "   Дозволи (одноразово, тепер для «Python», а не термінала):"
echo "   System Settings → Privacy & Security → Microphone, Input Monitoring,"
echo "   Accessibility — увімкнути для Python; якщо нема в списку, додати файл"
echo "   $DIR/.venv/bin/python"
echo "   Перезапуск після зміни коду: launchctl kickstart -k gui/\$(id -u)/$LABEL"
