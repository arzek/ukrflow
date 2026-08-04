#!/bin/zsh
# Встановлює UkrFlow.app у /Applications і вмикає автозапуск через Login Items.
# Замінює старий механізм автозапуску (LaunchAgent) — тепер це звичайний
# macOS-застосунок. Використання: ./make-app.sh && ./install-app.sh
set -e
cd "$(dirname "$0")"

APP_SRC="dist/UkrFlow.app"
APP_DST="/Applications/UkrFlow.app"
LABEL="com.ukrflow.app"

[ -d "$APP_SRC" ] || { echo "❌ Немає $APP_SRC — спершу ./make-app.sh"; exit 1; }

# 1) Прибираємо старі способи запуску, щоб не було двох екземплярів, що б'ються
#    за мікрофон і гарячі клавіші.
if [ -f "$HOME/Library/LaunchAgents/$LABEL.plist" ]; then
    echo "⏹  Вимикаю старий автозапуск (LaunchAgent)…"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
fi
if pgrep -f "[Pp]ython.* ukrflow\.py" >/dev/null; then
    echo "⏹  Зупиняю екземпляр, запущений вручну…"
    pkill -f "[Pp]ython.* ukrflow\.py" || true
    sleep 1
fi

# 2) Переносимо застосунок у /Applications.
echo "▸ Копіюю в $APP_DST…"
rm -rf "$APP_DST"
cp -R "$APP_SRC" "$APP_DST"

# 3) Автозапуск через Login Items (сучасний спосіб для macOS-застосунків).
echo "▸ Додаю до Login Items…"
osascript >/dev/null <<OSA
tell application "System Events"
    if login item "UkrFlow" exists then delete login item "UkrFlow"
    make login item at end with properties {path:"$APP_DST", hidden:false, name:"UkrFlow"}
end tell
OSA

# 4) Запускаємо зараз (через LaunchServices, щоб дозволи прив'язались до .app).
echo "▸ Запускаю UkrFlow…"
open -a "$APP_DST"

cat <<'DONE'

✅ Готово. UkrFlow тепер звичайний застосунок у /Applications і стартує при вході.

⚠️  Один раз потрібно надати дозволи вже для «UkrFlow» (а не для Python/термінала):
    System Settings → Privacy & Security →
      • Microphone         → увімкнути UkrFlow
      • Input Monitoring   → додати/увімкнути UkrFlow
      • Accessibility      → додати/увімкнути UkrFlow
    Якщо UkrFlow немає в списку — натисніть «+» і виберіть /Applications/UkrFlow.app.
    Після надання дозволів вийдіть із застосунку (меню-бар → Вийти) і запустіть знову.

Далі: правки ukrflow.py дозволів НЕ скидають. Щоб підхопити зміни коду —
просто перезапустіть застосунок:  killall UkrFlow 2>/dev/null; open -a UkrFlow
DONE
