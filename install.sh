#!/bin/zsh
# Встановлення UkrFlow на новому Mac (Apple Silicon).
# Використання: скопіюйте папку ukrflow (без .venv) і запустіть ./install.sh
set -e
cd "$(dirname "$0")"

[ "$(uname -m)" = "arm64" ] || { echo "❌ Потрібен Apple Silicon (M1+): MLX не працює на Intel"; exit 1; }
command -v python3 >/dev/null || { echo "❌ Потрібен Python 3: xcode-select --install"; exit 1; }
command -v claude >/dev/null || echo "⚠️  Claude Code CLI не знайдено — шліфування через підписку не працюватиме (npm install -g @anthropic-ai/claude-code)"

python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo ""
echo "✅ Готово. Запуск: ./run.sh"
echo "   Моделі (~1.6 ГБ Whisper) завантажаться при першому старті."
echo "   Не забудьте дозволи: System Settings → Privacy & Security →"
echo "   Microphone, Input Monitoring, Accessibility для вашого термінала."
