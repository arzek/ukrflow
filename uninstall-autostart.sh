#!/bin/zsh
# Вимкнення автозапуску UkrFlow: зупиняє процес і видаляє LaunchAgent.
set -e
LABEL="com.ukrflow.app"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "✅ Автозапуск вимкнено. Ручний запуск: ./run.sh"
