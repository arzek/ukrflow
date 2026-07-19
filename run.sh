#!/bin/zsh
# Запуск UkrFlow
cd "$(dirname "$0")"
exec .venv/bin/python ukrflow.py "$@"
