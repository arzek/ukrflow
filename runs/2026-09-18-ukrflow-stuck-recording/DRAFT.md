# DRAFT — докази для діагнозу «запис не зупиняється» (2026-09-18)

1. pynput (master) не обробляє `kCGEventTapDisabledByTimeout/ByUserInput` — у `_util/darwin.py` цих подій немає (linkcheck: так; локально в `.venv` 1.8.2 — те саме). https://raw.githubusercontent.com/moses-palmer/pynput/master/lib/pynput/_util/darwin.py
2. pynput визначає press/release модифікатора за спільним прапорцем: `Key.cmd_l` і `Key.cmd_r` → `kCGEventFlagMaskCommand`, `is_press = flags & …` (linkcheck: так). https://raw.githubusercontent.com/moses-palmer/pynput/master/lib/pynput/keyboard/_darwin.py
3. PortAudio на macOS: «macos: deadlock stopping audio» — AB-BA інверсія назавжди клинить потік, що викликав `Pa_StopStream` (linkcheck: так). https://github.com/PortAudio/portaudio/issues/1175
4. Python: stdout не в терміналі — «block-buffered like regular text files»; `reconfigure(line_buffering=True)` дає рядкову (linkcheck: так; цитата Codex про `-u` на цій сторінці не знайдена). https://docs.python.org/3.10/library/sys.html#sys.stdout
5. launchd: `SuccessfulExit=false` → перезапуск після неуспішного виходу; `kickstart -k` — «kill the running instance before restarting the service» (linkcheck: так). https://keith.github.io/xcode-man-pages/launchd.plist.5.html · https://keith.github.io/xcode-man-pages/launchctl.1.html
6. Apple: сторінка CGEventType доступна (HTTP 200), але вміст WebFetch не витяг — твердження про вимкнення tap за таймаутом лишається «частково» (Codex підтвердив, Copilot — UNVERIFIED). https://developer.apple.com/documentation/coregraphics/cgeventtype
7. UNVERIFIED: кеш списку пристроїв у python-sounddevice та `sd._terminate()/_initialize()` — першоджерело не знайдено жодним вендором (оцінка: прийом робочий, у коді — лише як одноразовий retry після збою відкриття мікрофона).

Оцінка: який саме механізм спрацьовує в користувача — не доведено (stdout під launchd губився, слідів немає); виправлення закриває всі три (глухий tap, зависле `Pa_StopStream`, аудіо лише в RAM) і вмикає діагностику.
