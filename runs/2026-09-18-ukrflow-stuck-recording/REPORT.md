# UkrFlow: «запис не зупиняється після відпускання клавіші» — звіт (2026-09-18)

## Стан
Нова версія запущена під launchd (pid 6820 на 22:46, event-tap увімкнений, `launchd.log` тепер пишеться порядково з мітками часу). Git не чіпав: змінені `ukrflow.py`, `README.md`, `CLAUDE.md`, `AGENTS.md`; нетрекана тека `runs/`. Справжнє диктування й нові пункти меню не перевірялись (GUI rumps headless не тестується) — це ручна перевірка.

## Причини
1. **Слухач клавіш.** pynput (1.8.2 у `.venv` і master) не обробляє `kCGEventTapDisabledByTimeout/ByUserInput`: tap — локальна змінна в `ListenerMixin._run`, подія про вимкнення трактується як хибний release. Після вимкнення tap-а слухач глухий до перезапуску процесу. Додатково pynput розрізняє `cmd_l`/`cmd_r` за спільним прапорцем Command — release може прийти як press.
2. **Зависання на зупинці.** `Pa_StopStream` на macOS може заклинити назавжди (PortAudio #1175). Закриття мікрофона йшло в єдиному воркері, а аудіо забиралося з пам'яті лише після нього: 🔴 лишалась, WAV не писався. `_finalize_recording` не мав try/except.
3. **Аудіо лише в RAM.** У сьогоднішньому інциденті (між 19:52 і 20:38) запис не зберігся взагалі.
4. **Діагностика губилась.** stdout під launchd блочно буферизувався: у `launchd.log` дійшло 68 диктувань із ~1400 у `ukrflow.log`.

Оцінка: який саме механізм спрацьовував у користувача — не доведено (слідів не лишалось), тому закрито всі; наступний інцидент буде видно в лозі.

## Що змінено
- `ResilientListener`: тримає tap, вмикає його назад, не пускає подію про вимкнення в pynput, прибирає tap після зупинки; `_start_engine` перестворює слухач у циклі з backoff.
- `_watch_health` (крок 0.5 с): вмикає вимкнений tap, перезапускає слухач при «глухому» tap-і (клавіша фізично натиснута ≥ 2 с, а press не дійшов), сповіщає про воркер, заклинений > 15 с.
- Вотчдог відпускання: два джерела стану (HID + сесія), зупинка після двох поспіль «відпущено».
- `stop_recording` лише перемикає стан; закриття мікрофона — окремий потік із таймаутом 3 с; `_pa_lock` серіалізує open/close PortAudio; `sd._terminate()` не викликається, поки є незакритий потік; про завислий мікрофон — одноразове сповіщення.
- Аварійний журнал `recordings/<stamp>.<pid>.pcm.part` (дописується щосекунди) + `recover_orphan_journals()` на старті й у `--retry`: після kill/перезапуску аудіо стає WAV, приходить сповіщення.
- Меню: «Зупинити запис», «Повторити останній запис», «Перезапустити UkrFlow» (під launchd — `launchctl kickstart -k` + страхувальний вихід із ненульовим кодом; з термінала — `os.execv`).
- `last.md` пишеться ще до шліфування (нешліфований текст), `cycle_mode` винесено з callback-а слухача у воркер, `diag()` з мітками часу, line-buffered stdout.

## Якщо щось зависло
«Зупинити запис» → «Повторити останній запис» → «Перезапустити UkrFlow». Вручну: `launchctl kickstart -k gui/$(id -u)/com.ukrflow.app`.

## Перевірка
- 14 ad-hoc скриптів (t1…t14, t_health) у scratchpad сесії — за звітом `coder` усі PASS; координатор перевіряв `ast.parse`, імпорт, ключові місця коду й перезапуск агента.
- Незалежний `verifier` (opus): 2 MAJOR (`Pa_Terminate` при завислому потоці; відновлений WAV видаляла власна чистка → падіння `run()` на старті) + 4 MINOR — усе виправлено.
- Останнє доопрацювання (сповіщення про завислий мікрофон, `_pa_wedged`) має лише тести автора, без повторної незалежної перевірки.

## Обмеження
- Кеш списку пристроїв у python-sounddevice / `sd._terminate()+_initialize()` — першоджерело не знайдено (UNVERIFIED); у коді це лише одноразовий retry після збою відкриття мікрофона.
- Сторінка Apple про CGEventType відповідає 200, але вміст автоматично не прочитався — твердження про вимкнення tap за таймаутом підтверджене частково.
- `restore_clipboard` (0.35 с) теоретично може відновити буфер раніше, ніж повільний застосунок прочитає вставку; текст у такому разі лишається в `last.md` («Останній результат»). Не змінювалось.

## Джерела (пройшли linkcheck)
- https://raw.githubusercontent.com/moses-palmer/pynput/master/lib/pynput/_util/darwin.py
- https://raw.githubusercontent.com/moses-palmer/pynput/master/lib/pynput/keyboard/_darwin.py
- https://github.com/PortAudio/portaudio/issues/1175
- https://docs.python.org/3.10/library/sys.html#sys.stdout
- https://keith.github.io/xcode-man-pages/launchd.plist.5.html
- https://keith.github.io/xcode-man-pages/launchctl.1.html
- https://developer.apple.com/documentation/coregraphics/cgeventtype (частково)
