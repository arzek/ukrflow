#!/usr/bin/env python3
"""
UkrFlow STT-сервер — спільний для всіх профілів macOS розпізнавач мовлення.

Клієнт (ukrflow.py) піднімає його на вимогу, сервер слухає лише
127.0.0.1:<port>, тримає одну модель Whisper (MLX) на всіх і сам завершується
після простою. Перший, хто зайняв порт, — і є сервер: другий екземпляр
отримує EADDRINUSE і виходить з кодом 3.

Коди виходу: 0 — ідл/shutdown/SIGTERM; 3 — порт зайнятий; 1 — self-watchdog
або фатальна помилка.

Навмисно лише stdlib на верхньому рівні: bind і /health мають відповідати
за частки секунди, ще до того, як numpy/mlx_whisper встигнуть імпортуватися.
"""

import argparse
import base64
import datetime
import errno
import faulthandler
import http.server
import io
import json
import os
import pwd
import re
import signal
import socket
import socketserver
import sys
import threading
import time
import traceback
import wave
from pathlib import Path

PROTO = 1
APP = "ukrflow-stt"
DEFAULT_PORT = 47117
SAMPLE_RATE = 16000
MAX_BODY_BYTES = 64 * 1024 * 1024
LOG_ROTATE_BYTES = 5 * 1024 * 1024
SHUTDOWN_WAIT_SEC = 30.0
# Скільки дочитувати непрочитане тіло після ранньої відмови (loopback —
# це сотні МБ/с, тож навіть тіло понад 64 МіБ вкладається з запасом).
DRAIN_BODY_SEC = 5.0
# Скільки після shutdown() ще розбирати backlog, перш ніж закрити сокет.
BACKLOG_DRAIN_SEC = 0.5
LOAD_WATCHDOG_SEC = 900.0
# Лише HF repo id: локальний шлях з чужого $HOME (інший профіль) серверу
# недоступний, а довільний шлях у запиті — зайва поверхня атаки.
REPO_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
QUIET_PATHS = ("/health", "/touch")

_log_lock = threading.Lock()
_log_file = None

# Увесь змінний стан — під одним локом; Condition на ньому будить тих, хто
# чекає завершення завантаження моделі.
_state_lock = threading.Lock()
_state_cond = threading.Condition(_state_lock)
_state = {
    "busy": 0,
    "busy_since": None,
    "last_activity": time.monotonic(),
    "draining": False,
    "model_state": "idle",
    "model": None,
    "model_path": None,
    "last_error": None,
    "served": 0,
    "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
}
_inflight = 0
_exiting = False
# ModelHolder у mlx_whisper не потокобезпечний (check-then-set), а Metal
# однаково виконує одну задачу за раз — тож усі виклики mlx_whisper, включно
# з прогрівом, ідуть строго по одному.
_gpu_lock = threading.Lock()


def log(message: str) -> None:
    stamp = datetime.datetime.now().strftime("%m-%d %H:%M:%S")
    with _log_lock:
        # До bind лог не відкритий (див. main): рядок іде в stderr, який
        # клієнт дописує в той самий server.log.
        print(f"[{stamp}] {message}", file=_log_file or sys.stderr, flush=True)


def _version() -> str:
    mtime = os.path.getmtime(__file__)
    return datetime.datetime.fromtimestamp(mtime).isoformat(timespec="seconds")


class MetaError(ValueError):
    pass


def _decode_meta(raw: str | None) -> dict:
    """base64url(JSON) із заголовка: кирилиця в заголовках http.client
    падає на latin-1, тому параметри їдуть закодованими. Padding — за бажанням."""
    if not raw:
        return {}
    try:
        data = raw.strip().encode("ascii")
        data += b"=" * (-len(data) % 4)
        meta = json.loads(base64.b64decode(data, altchars=b"-_", validate=True).decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise MetaError(f"некоректний X-UkrFlow-Meta: {exc}") from None
    if not isinstance(meta, dict):
        raise MetaError("X-UkrFlow-Meta має бути JSON-об'єктом")
    return meta


def _meta_str(meta: dict, key: str, required: bool = True) -> str | None:
    value = meta.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value):
        raise MetaError(f"поле {key} має бути рядком")
    return value


def _meta_model(meta: dict) -> str:
    model = _meta_str(meta, "model")
    if not REPO_ID_RE.match(model):
        raise MetaError("model — лише HF repo id (автор/назва)")
    return model


# ─── Модель ─────────────────────────────────────────────────────────────


class ModelLoadError(RuntimeError):
    pass


def _busy_inc_locked() -> None:
    if _state["busy"] == 0:
        _state["busy_since"] = time.monotonic()
    _state["busy"] += 1


def _busy_dec_locked() -> None:
    _state["busy"] -= 1
    if _state["busy"] == 0:
        _state["busy_since"] = None
    _state["last_activity"] = time.monotonic()
    _state_cond.notify_all()


def _claim_load_locked(repo: str) -> str | None:
    """Позначає завантаження repo як розпочате (під _state_lock).
    Повертає попередню модель — для рядка «перемикаю модель»."""
    previous = _state["model"]
    _state["model"] = repo
    _state["model_state"] = "loading"
    _state["model_path"] = None
    _state["last_error"] = None
    _busy_inc_locked()
    return previous


def _load_model(repo: str, language: str, previous: str | None) -> str:
    """Завантаження вже заявлене через _claim_load_locked: busy +1 знімається
    тут, а стан стає ready або error. Повертає локальний шлях моделі."""
    t0 = time.monotonic()
    try:
        import numpy as np
        import mlx.core as mx
        import mlx_whisper
        from huggingface_hub import snapshot_download
        from huggingface_hub.errors import LocalEntryNotFoundError
        from mlx_whisper.transcribe import ModelHolder

        # Сервер стартує по кілька разів на годину: мережа до HF потрібна
        # лише для першого завантаження, далі — тільки локальний кеш.
        # Межа LOAD_WATCHDOG_SEC нижче свідомо охоплює лише GPU-прогрів, а не
        # snapshot_download: перше завантаження ~1.5 ГБ з Hugging Face на
        # повільній мережі — не зависання, і вбивати його таймером не можна.
        # Простій під час loading і так не рахується (див. _watch_idle).
        try:
            path = snapshot_download(repo_id=repo, local_files_only=True)
        except LocalEntryNotFoundError:
            log(f"кешу нема, завантажую з Hugging Face: {repo}")
            path = snapshot_download(repo_id=repo)

        with _gpu_lock:
            if ModelHolder.model is not None and ModelHolder.model_path != path:
                log(f"перемикаю модель {previous} → {repo}")
                ModelHolder.model = None
                ModelHolder.model_path = None
                mx.clear_cache()
            faulthandler.dump_traceback_later(
                LOAD_WATCHDOG_SEC, repeat=False, file=_log_file, exit=True)
            try:
                mlx_whisper.transcribe(
                    np.zeros(SAMPLE_RATE // 2, dtype=np.float32),
                    path_or_hf_repo=path,
                    language=language,
                )
            finally:
                faulthandler.cancel_dump_traceback_later()
    except BaseException as exc:
        log(f"модель {repo} не завантажилась: {exc}\n{traceback.format_exc()}")
        with _state_lock:
            if _state["model"] == repo:
                _state["model_state"] = "error"
                _state["last_error"] = f"{type(exc).__name__}: {exc}"
            _busy_dec_locked()
        raise ModelLoadError(f"{type(exc).__name__}: {exc}") from exc

    log(f"модель {repo} завантажена за {time.monotonic() - t0:.1f} с")
    with _state_lock:
        if _state["model"] == repo:
            _state["model_state"] = "ready"
            _state["model_path"] = path
        _busy_dec_locked()
    return path


def _ensure_model(repo: str, language: str) -> str:
    """Чекає чужого завантаження або вантажить сам. Другий запит під час
    loading не вантажить модель удруге — чекає на _state_cond."""
    with _state_cond:
        while _state["model_state"] == "loading":
            _state_cond.wait()
            if _state["model"] == repo and _state["model_state"] == "error":
                raise ModelLoadError(_state["last_error"])
        if _state["model"] == repo and _state["model_state"] == "ready":
            return _state["model_path"]
        previous = _claim_load_locked(repo)
    return _load_model(repo, language, previous)


def _warm_in_background(repo: str, language: str) -> None:
    try:
        _ensure_model(repo, language)
    except ModelLoadError:
        pass  # вже в лозі й у last_error


def _warm_in_background_claimed(repo: str, language: str, previous: str | None) -> None:
    try:
        _load_model(repo, language, previous)
    except ModelLoadError:
        pass  # вже в лозі й у last_error


# ─── Вихід ──────────────────────────────────────────────────────────────


def _exit_server(server, reason: str, wait_busy_sec: float) -> None:
    """Єдиний шлях виходу (ідл, /shutdown, SIGTERM): спершу draining — нові
    запити бачать 503 closing, а не процес, що зникає посеред відповіді."""
    global _exiting
    with _state_cond:
        if _exiting:
            return
        _exiting = True
        _state["draining"] = True
        deadline = time.monotonic() + wait_busy_sec
        while _state["busy"] > 0:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            _state_cond.wait(left)
        busy = _state["busy"]
    log(f"{reason}" + (f" (busy={busy} після {wait_busy_sec:.0f} с)" if busy else ""))
    # shutdown() блокує до виходу з serve_forever — лише з іншого потоку,
    # і з межею, щоб вихід не завис, якщо serve_forever ще не стартував.
    stopper = threading.Thread(target=server.shutdown, daemon=True)
    stopper.start()
    stopper.join(2.0)
    _reject_backlog(server)
    server.server_close()
    # Дати вже прийнятим з'єднанням дописати відповідь (503 closing тощо).
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with _state_lock:
            if _inflight == 0:
                break
        time.sleep(0.02)
    os._exit(0)


def _reject_backlog(server) -> None:
    """Після shutdown() serve_forever уже не робить accept, а з'єднання, що
    встигли стати в backlog, close() слухаючого сокета обірвав би RST-ом —
    клієнт бачив би Reset замість 503 closing. Розбираємо їх звичайним
    шляхом (draining уже True), з межею в часі: клієнт, що шле запити без
    паузи, не має тримати вихід вічно."""
    deadline = time.monotonic() + BACKLOG_DRAIN_SEC
    try:
        server.socket.setblocking(False)
        while time.monotonic() < deadline:
            try:
                request, address = server.socket.accept()
            except BlockingIOError:
                break
            server.process_request(request, address)
    except OSError:
        pass


def _watch_idle(server, idle_sec: float) -> None:
    while True:
        time.sleep(1.0)
        with _state_lock:
            idle = time.monotonic() - _state["last_activity"]
            if (_state["draining"] or _state["busy"] > 0
                    or _state["model_state"] == "loading" or idle <= idle_sec):
                continue
            _state["draining"] = True
        _exit_server(server, f"ідл-вихід: простій {idle:.0f} с", 0.0)
        return


# ─── HTTP ───────────────────────────────────────────────────────────────


class Server(http.server.ThreadingHTTPServer):
    # Після kill/logout порт лишається в TIME_WAIT — без REUSEADDR респавн
    # одразу після падіння не пройшов би bind.
    allow_reuse_address = True
    # Явно: SO_REUSEPORT дозволив би двом процесам слухати той самий порт,
    # а арбітраж «один сервер на два профілі» тримається саме на EADDRINUSE.
    allow_reuse_port = False
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind кличе getfqdn — DNS-запит на старті, якого
        # loopback-серверу не треба.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]

    def process_request(self, request, client_address):
        # Лічильник — ще в потоці accept: потік обробника може не встигнути
        # стартувати до того, як вихід перевірить _inflight, і вже прийняте
        # з'єднання обірвалося б без відповіді.
        global _inflight
        with _state_lock:
            _inflight += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            with _state_lock:
                _inflight -= 1
            raise

    def process_request_thread(self, request, client_address):
        global _inflight
        try:
            super().process_request_thread(request, client_address)
        finally:
            with _state_lock:
                _inflight -= 1


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    # Таймаут сокета: повільний або завислий клієнт не тримає потік вічно.
    timeout = 30
    server_uid = os.getuid()
    _body_read = False

    # ── службове ──

    def handle(self):
        try:
            super().handle()
        finally:
            self._drain_body()

    def _drain_body(self) -> None:
        """Ранні відмови (403/409/400/411/413/503) летять, поки тіло ще в
        дорозі, а close() сокета з непрочитаними даними — це RST: клієнт
        замість типізованої відповіді побачив би обрив і повторював би запит
        до дедлайну. Тож FIN — одразу (відповідь уже в сокеті), а решту тіла
        дочитуємо й відкидаємо."""
        headers = getattr(self, "headers", None)
        if self._body_read or headers is None:
            return
        length = headers.get("Content-Length")
        if length is None:
            # HTTP/1.0 без Content-Length: тіло закінчується з EOF клієнта.
            if self.command != "POST":
                return
            left = sys.maxsize
        else:
            try:
                left = int(length)
            except ValueError:
                return
        if left <= 0:
            return
        deadline = time.monotonic() + DRAIN_BODY_SEC
        try:
            self.connection.shutdown(socket.SHUT_WR)
            self.connection.settimeout(1.0)
            while left > 0 and time.monotonic() < deadline:
                chunk = self.rfile.read1(min(left, 1 << 20))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass

    def log_request(self, code="-", size="-"):
        if getattr(self, "path", None) in QUIET_PATHS:
            return
        super().log_request(code, size)

    def log_message(self, format, *args):
        log(f"http {format % args}")

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _error(self, code: int, kind: str, error: str, **extra) -> None:
        self._send_json(code, {"error": error, "kind": kind, **extra})

    def _gate(self) -> bool:
        """Перевірка на вході кожного запиту. Origin/чужий Host — це браузер
        (no-cors POST на loopback або DNS-rebinding), а не наш клієнт."""
        expected = f"127.0.0.1:{self.server.server_port}"
        if self.headers.get("Origin") is not None or self.headers.get("Host") != expected:
            self._error(403, "forbidden", "forbidden")
            return False
        with _state_lock:
            draining = _state["draining"]
        if draining:
            self._error(503, "closing", "сервер завершується")
            return False
        return True

    def _claim(self, count_busy: bool) -> bool:
        """Перевірка draining, busy +1 і last_activity — в одній критичній
        секції: ідл-вотчдог виставляє draining під тим самим локом лише при
        busy==0, тож запит або отримує 503 closing, або вже врахований у busy
        і доживе до відповіді. Окремі «перевірив» і «інкрементував» лишали
        вікно, в яке вотчдог гасив сервер посеред щойно прийнятого запиту."""
        with _state_lock:
            if _state["draining"]:
                return False
            if count_busy:
                _busy_inc_locked()
            _state["last_activity"] = time.monotonic()
        return True

    def _release(self, count_busy: bool) -> None:
        with _state_lock:
            if count_busy:
                _busy_dec_locked()
            else:
                _state["last_activity"] = time.monotonic()

    # ── методи ──

    def do_OPTIONS(self):
        self._error(405, "method", "метод не підтримується")

    def do_GET(self):
        if not self._gate():
            return
        if self.path != "/health":
            self._error(404, "not_found", "невідомий шлях")
            return
        now = time.monotonic()
        with _state_lock:
            payload = {
                "app": APP,
                "proto": PROTO,
                "version": self.server.code_version,
                "pid": os.getpid(),
                "uid": os.getuid(),
                "user": pwd.getpwuid(os.getuid()).pw_name,
                "started_at": _state["started_at"],
                "model_state": _state["model_state"],
                "model": _state["model"],
                "busy": _state["busy"],
                "busy_sec": round(now - _state["busy_since"], 3) if _state["busy_since"] else 0.0,
                "draining": _state["draining"],
                "idle_sec": round(now - _state["last_activity"], 3),
                "last_error": _state["last_error"],
                "log_path": self.server.log_path,
                "served": _state["served"],
            }
        self._send_json(200, payload)

    def do_POST(self):
        if not self._gate():
            return
        proto = self.headers.get("X-UkrFlow-Proto")
        if proto is None:
            self._error(403, "forbidden", "forbidden")
            return
        self.foreign_proto = None
        if proto.strip() != str(PROTO):
            if self.path != "/shutdown":
                self._error(409, "proto", "несумісний протокол", proto=PROTO)
                return
            # Клієнт іншої версії має змогу погасити цей сервер, щоб підняти
            # свій, — тож м'який /shutdown приймаємо з будь-яким proto. Формат
            # Meta чужої версії невідомий: його не читаємо, а force (обрив
            # чужого диктування) лишається тільки за власним протоколом.
            self.foreign_proto = proto.strip()
            meta = {}
            try:
                self.client_uid = int(self.headers.get("X-UkrFlow-Uid", ""))
            except ValueError:
                self.client_uid = None
        else:
            try:
                meta = _decode_meta(self.headers.get("X-UkrFlow-Meta"))
                self.client_uid = int(self.headers.get("X-UkrFlow-Uid", ""))
            except (MetaError, ValueError) as exc:
                self._error(400, "bad_meta", str(exc) if isinstance(exc, MetaError)
                            else "немає або некоректний X-UkrFlow-Uid")
                return

        routes = {
            "/touch": self._post_touch,
            "/warm": self._post_warm,
            "/transcribe": self._post_transcribe,
            "/shutdown": self._post_shutdown,
        }
        route = routes.get(self.path)
        if route is None:
            self._error(404, "not_found", "невідомий шлях")
            return
        # /shutdown у busy не рахуємо: інакше _exit_server чекав би (або з
        # force — звітував би busy=1) на сам запит, що його запустив.
        count_busy = self.path != "/shutdown"
        if not self._claim(count_busy):
            self._error(503, "closing", "сервер завершується")
            return
        try:
            route(meta)
        except MetaError as exc:
            self._error(400, "bad_meta", str(exc))
        finally:
            self._release(count_busy)

    # ── маршрути ──

    def _post_touch(self, meta: dict) -> None:
        self._send_json(200, {"ok": True})

    def _post_warm(self, meta: dict) -> None:
        repo = _meta_model(meta)
        language = _meta_str(meta, "language")
        # Стан loading виставляється ще до відповіді 202 — інакше /health,
        # що прийде слідом, побачив би idle і клієнт засумнівався б.
        with _state_lock:
            if _state["model"] == repo and _state["model_state"] in ("loading", "ready"):
                target = None
            elif _state["model_state"] == "loading":
                target = (_warm_in_background, (repo, language))
            else:
                previous = _claim_load_locked(repo)
                target = (_warm_in_background_claimed, (repo, language, previous))
            model_state = _state["model_state"]
        if target is not None:
            threading.Thread(target=target[0], args=target[1], daemon=True).start()
        self._send_json(202, {"model_state": model_state})

    def _post_transcribe(self, meta: dict) -> None:
        repo = _meta_model(meta)
        language = _meta_str(meta, "language")
        initial_prompt = _meta_str(meta, "initial_prompt", required=False)

        length = self.headers.get("Content-Length")
        if length is None:
            self._error(411, "length_required", "потрібен Content-Length")
            return
        try:
            length = int(length)
            if length < 0:
                raise ValueError
        except ValueError:
            self._error(400, "bad_request", "некоректний Content-Length")
            return
        if length > MAX_BODY_BYTES:
            self._error(413, "too_large", f"тіло понад {MAX_BODY_BYTES} байт")
            return

        # busy +1 уже взято в do_POST (_claim) — разом із перевіркою draining.
        self._transcribe(repo, language, initial_prompt, length)

    def _transcribe(self, repo: str, language: str, initial_prompt: str | None,
                    length: int) -> None:
        body = self.rfile.read(length)
        self._body_read = True
        try:
            with wave.open(io.BytesIO(body), "rb") as wav:
                params = (wav.getframerate(), wav.getnchannels(), wav.getsampwidth())
                frames = wav.readframes(wav.getnframes())
        except (wave.Error, EOFError) as exc:
            self._error(400, "bad_audio", f"некоректний WAV: {exc}")
            return
        if params != (SAMPLE_RATE, 1, 2):
            self._error(400, "bad_audio",
                        f"потрібен WAV {SAMPLE_RATE} Гц, моно, int16; отримано {params}")
            return

        try:
            import numpy as np

            audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
            audio_sec = len(audio) / SAMPLE_RATE
            t0 = time.monotonic()
            while True:
                path = _ensure_model(repo, language)
                with _gpu_lock:
                    # Між _ensure_model і локом інший запит міг перемкнути
                    # модель — тоді чекаємо знову, а не вантажимо її мовчки
                    # всередині mlx_whisper.
                    with _state_lock:
                        current = (_state["model"] == repo
                                   and _state["model_state"] == "ready")
                    if not current:
                        continue
                    import mlx_whisper

                    faulthandler.dump_traceback_later(
                        60 + 0.5 * audio_sec, repeat=False, file=_log_file, exit=True)
                    try:
                        result = mlx_whisper.transcribe(
                            audio,
                            path_or_hf_repo=path,
                            language=language,
                            initial_prompt=initial_prompt or None,
                            condition_on_previous_text=False,
                        )
                    finally:
                        faulthandler.cancel_dump_traceback_later()
                    break
            seconds = time.monotonic() - t0
        except Exception as exc:
            trace = traceback.format_exc()
            log(f"transcribe uid={self.client_uid} збій: {exc}\n{trace}")
            self._error(500, "mlx", f"{type(exc).__name__}: {exc}", trace_tail=trace[-2000:])
            return

        log(f"transcribe uid={self.client_uid} аудіо {audio_sec:.1f} с → {seconds:.1f} с")
        with _state_lock:
            _state["served"] += 1
        self._send_json(200, {
            "text": result["text"],
            "seconds": round(seconds, 3),
            "audio_sec": round(audio_sec, 3),
            "model": repo,
        })

    def _post_shutdown(self, meta: dict) -> None:
        force = meta.get("force", False)
        if not isinstance(force, bool):
            raise MetaError("поле force має бути bool")
        # Примусовий вихід (без очікування busy) — лише власнику: клієнт
        # іншого профілю не має обривати чуже диктування.
        force = force and self.client_uid == self.server_uid
        self._send_json(200, {"ok": True})
        reason = (f"shutdown від uid={self.client_uid}" + (" (force)" if force else "")
                  + (f" (proto {self.foreign_proto})" if self.foreign_proto else ""))
        threading.Thread(
            target=_exit_server,
            args=(self.server, reason, 0.0 if force else SHUTDOWN_WAIT_SEC),
            daemon=True,
        ).start()


# ─── Старт ──────────────────────────────────────────────────────────────


def _open_log(path: Path) -> None:
    """Лише після успішного bind: екземпляр, що програв арбітраж порту (з
    того самого клону), не має перейменовувати живий лог переможця."""
    global _log_file
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass
    _log_file = open(path, "a", buffering=1, encoding="utf-8")
    sys.stdout = _log_file
    sys.stderr = _log_file
    # Нативний код (abort у MLX, assert у Metal) пише просто в fd 1/2, які
    # успадковано від клієнта, — без dup2 цих рядків у server.log не було б.
    os.dup2(_log_file.fileno(), 1)
    os.dup2(_log_file.fileno(), 2)
    # Segfault у Metal/MLX інакше не лишив би в лозі жодного сліду.
    faulthandler.enable(file=_log_file)


def main() -> None:
    parser = argparse.ArgumentParser(description="UkrFlow STT-сервер")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--idle-sec", type=float, default=60.0)
    parser.add_argument("--log", type=Path, default=Path(__file__).parent / "server.log")
    args = parser.parse_args()
    # Версія коду, що працює, — один раз на старті: mtime на диску після
    # git pull уже належить новому коду, якого цей процес не виконує.
    code_version = _version()

    log_path = args.log.resolve()

    # Лог відкривається (і ротується) лише переможцем арбітражу порту — до
    # того log() пише в stderr, який клієнт дописує в server.log.
    try:
        server = Server(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            log(f"порт {args.port} зайнятий — сервер уже працює (pid {os.getpid()} виходить, 3)")
            sys.exit(3)
        raise
    if server.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT) != 0:
        # Теж до відкриття лога: з REUSEPORT «переможців» може бути двоє.
        log("SO_REUSEPORT увімкнено — арбітраж порту не працюватиме, виходжу")
        sys.exit(1)
    _open_log(log_path)
    server.log_path = str(log_path)
    server.code_version = code_version

    log(f"старт: pid {os.getpid()}, uid {os.getuid()}, порт {args.port}, "
        f"ідл {args.idle_sec:g} с, версія {code_version}")

    signal.signal(signal.SIGTERM, lambda signum, frame: threading.Thread(
        target=_exit_server, args=(server, "SIGTERM", SHUTDOWN_WAIT_SEC), daemon=True,
    ).start())

    threading.Thread(target=server.serve_forever, name="serve", daemon=True).start()
    threading.Thread(target=_watch_idle, args=(server, args.idle_sec),
                     name="idle", daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        log(f"фатальна помилка:\n{traceback.format_exc()}")
        sys.exit(1)
