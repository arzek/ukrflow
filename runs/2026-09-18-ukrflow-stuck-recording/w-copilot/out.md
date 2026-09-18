| № | Твердження | Дослівна цитата з джерела | URL |
|---:|---|---|---|
| 1 | macOS може вимкнути CGEventTap через timeout або user input; застосунок повторно вмикає його через CGEventTapEnable. | — | UNVERIFIED |
| 2 | **Підтверджено частково:** код pynput слухає keyDown, keyUp і flagsChanged, але не обробляє події вимкнення tap. | “The events that we listen to” | https://raw.githubusercontent.com/moses-palmer/pynput/master/lib/pynput/keyboard/_darwin.py |
| 3 | **Підтверджено:** pynput визначає press/release модифікатора перевіркою спільного прапорця в kCGEventFlagsChanged. | `is_press = flags & self._MODIFIER_FLAGS.get(key, 0)` | https://raw.githubusercontent.com/moses-palmer/pynput/master/lib/pynput/keyboard/_darwin.py |
| 4 | **Підтверджено:** у PortAudio є macOS CoreAudio проблема взаємної блокировки під час start/stop потоку. | “Fix/1174 coreaudio startstop deadlock” | https://github.com/PortAudio/portaudio/issues/1175 |
| 5 | **Не підтверджено:** sounddevice кешує пристрої під час ініціалізації, а оновлення виконується через `_terminate()` і `_initialize()`. | — | UNVERIFIED |
| 6 | **Підтверджено частково:** stdout у файл буферизується блоками; документація також описує line buffering і параметри небуферизованого запуску. | “Other text files use the policy described above for binary files.” | https://github.com/python/cpython/blob/3.14/Lib/_pyio.py |
| 7 | **Підтверджено частково:** `KeepAlive` із `SuccessfulExit=false` перезапускає після ненульового виходу; `kickstart -k` цим джерелом не підтверджено. | “If false, the job will be restarted in the inverse condition.” | https://www.manpagez.com/man/5/launchd.plist/; https://www.manpagez.com/man/1/launchctl/ |
