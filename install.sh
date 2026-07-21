#!/bin/zsh
# Встановлення UkrFlow на новому Mac (Apple Silicon).
# Використання: скопіюйте папку ukrflow (без .venv) і запустіть ./install.sh
set -e
cd "$(dirname "$0")"

[ "$(uname -m)" = "arm64" ] || { echo "❌ Потрібен Apple Silicon (M1+): MLX не працює на Intel"; exit 1; }
command -v python3 >/dev/null || { echo "❌ Потрібен Python 3: xcode-select --install"; exit 1; }
if ! command -v codex >/dev/null && ! command -v claude >/dev/null; then
  echo "⚠️  Не знайдено ні Codex, ні Claude Code CLI — хмарне шліфування не працюватиме"
  echo "   Встановіть і залогіньте хоча б один із них, потім оберіть його в меню «Бекенд»."
fi

python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo ""
echo "✅ Готово. Запуск: ./run.sh"
echo "   Моделі (~1.6 ГБ Whisper) завантажаться при першому старті."
echo "   Не забудьте дозволи: System Settings → Privacy & Security →"
echo "   Microphone, Input Monitoring, Accessibility для вашого термінала."
