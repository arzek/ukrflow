#!/usr/bin/env python3
"""
Бенчмарк шліфування UkrFlow — ad-hoc інструмент, а не частина застосунку.

Бере останні реальні сирі розшифровки з ukrflow.log (режими clean/prompt),
проганяє кожну через кілька конфігурацій бекенд × модель × effort і міряє
стінний час, щоб користувач обрав типові моделі per-режим за таблицею, а якість
оцінив вручну за REVIEW-файлами.

УВАГА: кожен виклик — справжній `codex exec` / `claude -p`, тобто витрачає
квоту підписок ChatGPT і Claude. Тому:
  * спершу завжди `--dry-run` — план і загальна кількість викликів;
  * `--max-calls` (типово 45) — аварійна стеля: більший план не запускається;
  * виклики строго послідовні з паузою — паралельний прогін швидше впирається
    в ліміти CLI і спотворює час.

Виклики йдуть через ті самі функції бекендів, що й у продакшені
(`ukrflow.POLISH_BACKENDS`), а не через копію команд: інакше бенчмарк міряв би
не ті прапорці, з якими реально працює диктування.

Запуск:
  .venv/bin/python bench_polish.py --dry-run
  .venv/bin/python bench_polish.py [--modes clean,prompt] [--texts-per-mode 5]
      [--configs matrix.json] [--out runs/<дата>-bench-polish] [--max-calls 45]
"""

import argparse
import csv
import datetime
import json
import re
import statistics
import sys
import time
from pathlib import Path

from ukrflow import (
    DEFAULT_CONFIG,
    LOG_PATH,
    POLISH_BACKENDS,
    _brief_error,
    effective_config,
)

ROOT = Path(__file__).parent

# Пауза між викликами — щоб не впиратися в rate limit CLI і щоб наступний
# виклик не стартував, поки попередній процес ще завершується
PAUSE_SEC = 1.0

# Пресет матриці: для clean важлива швидкість (короткі тексти, старт CLI —
# більша частина часу), для prompt — якість переструктурування
DEFAULT_MATRIX = {
    "clean": [
        {"backend": "codex", "model": "gpt-6-luna", "effort": "low"},
        {"backend": "claude-code", "model": "haiku", "effort": "low"},
        {"backend": "claude-code", "model": "sonnet", "effort": "low"},
    ],
    "prompt": [
        {"backend": "codex", "model": "gpt-6-luna", "effort": "low"},
        {"backend": "claude-code", "model": "sonnet", "effort": "low"},
        {"backend": "claude-code", "model": "opus", "effort": "low"},
    ],
}

# Фіксований старт CLI: короткий вхід (але ≥ polish_min_chars, як у продакшені)
# з інструкцією clean — час такого виклику майже весь іде на запуск інструмента
STARTUP_TEXT = "Тестова фраза для виміру старту інструмента."
STARTUP_PROMPT_FILE = "prompts/clean.md"
STARTUP_PROBES = 2
# Псевдорежим проб старту в timings.csv і в теці результатів
STARTUP_MODE = "_startup"

# Ключі моделі/effort у конфігу для кожного бекенда
BACKEND_KEYS = {"codex": "codex", "claude-code": "claude_code"}

HEADER_RE = re.compile(r"^════ ")
# Після дужки ukrflow.py дописує « ═ stt: …» (pid/uid сервера або «локально»);
# хвіст допускається, інакше кожен новий запис мовчки йшов би в «старий формат»
MODE_RE = re.compile(r"═ режим: (\S+) \(шліфування: ([^)]*)\)(?: ═ .*)?\s*$")
RAW_START = "── Розпізнано ──"
# Будь-який наступний заголовок блоку закриває текст розпізнавання
BLOCK_END_RE = re.compile(r"^(── .*──|════ |❌ ЗБІЙ)")


# ── Парсер ukrflow.log ─────────────────────────────────────────────────────

def parse_log(path: Path) -> tuple[list[dict], int]:
    """Повертає (записи, кількість пропущених записів старого формату).
    Запис = {"mode", "polish", "header", "raw"}. Заголовки липня 2026
    («… шліфування 4.3 с (claude-code)», «… ═ шліфування: claude-code») режиму
    не мають — до появи режимів невідомо, якою інструкцією їх шліфували,
    тож вони пропускаються, а не вгадуються."""
    entries: list[dict] = []
    skipped_legacy = 0
    current: dict | None = None
    raw_lines: list[str] | None = None

    def close_raw():
        nonlocal raw_lines
        if current is not None and raw_lines is not None:
            current["raw"] = "\n".join(raw_lines).strip()
        raw_lines = None

    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if HEADER_RE.match(line):
                close_raw()
                match = MODE_RE.search(line)
                if match:
                    current = {"mode": match.group(1), "polish": match.group(2),
                               "header": line, "raw": ""}
                    entries.append(current)
                else:
                    skipped_legacy += 1
                    current = None
                continue
            if line == RAW_START:
                close_raw()
                raw_lines = []
                continue
            if raw_lines is not None:
                if BLOCK_END_RE.match(line):
                    close_raw()
                else:
                    raw_lines.append(line)
    close_raw()
    return entries, skipped_legacy


def select_texts(entries: list[dict], modes: list[str], per_mode: int,
                 min_chars: int) -> dict[str, list[str]]:
    """Останні per_mode унікальних текстів кожного режиму (у хронологічному
    порядку). Коротші за polish_min_chars продакшен не шліфує взагалі —
    міряти їх немає сенсу."""
    selected: dict[str, list[str]] = {}
    for mode in modes:
        texts: list[str] = []
        for entry in reversed(entries):
            raw = entry["raw"]
            if entry["mode"] != mode or len(raw) < min_chars or raw in texts:
                continue
            texts.append(raw)
            if len(texts) == per_mode:
                break
        selected[mode] = list(reversed(texts))
    return selected


# ── Матриця конфігурацій ───────────────────────────────────────────────────

def slugify(entry: dict) -> str:
    # Необов'язковий суфікс "name" розрізняє конфігурації з однаковими бекендом,
    # моделлю й effort, але різними prompt_file (A/B стара ↔ нова інструкція):
    # без нього їхні теки й рядки таблиці злилися б в одну.
    slug = f"{entry['backend']}-{entry['model']}-{entry.get('effort', 'low')}"
    if entry.get("name"):
        slug += f"-{entry['name']}"
    return re.sub(r"[^A-Za-z0-9._-]+", "_", slug)


def validate_matrix(matrix: dict, modes: list[str]) -> None:
    for mode in modes:
        if mode not in matrix:
            raise SystemExit(f"У матриці конфігурацій немає режиму {mode!r}.")
        for entry in matrix[mode]:
            if entry.get("backend") not in POLISH_BACKENDS:
                raise SystemExit(
                    f"Невідомий бекенд {entry.get('backend')!r} у режимі {mode!r}. "
                    f"Доступні: {', '.join(POLISH_BACKENDS)}"
                )
            if not entry.get("model"):
                raise SystemExit(f"Конфігурація без model у режимі {mode!r}: {entry}")


def build_config(mode: str, entry: dict) -> dict:
    """Конфіг одного виклику: DEFAULT_CONFIG + перекриття режиму (prompt_file
    тощо) + бекенд/модель/effort з матриці. Береться DEFAULT_CONFIG, а не
    load_config(): той записує config.json користувача. Fallback вимкнено —
    інакше збій одного бекенда тихо міряв би інший."""
    cfg = effective_config(dict(DEFAULT_CONFIG), mode)
    key = BACKEND_KEYS[entry["backend"]]
    cfg["polish"] = entry["backend"]
    cfg[f"polish_{key}_model"] = entry["model"]
    cfg[f"polish_{key}_effort"] = entry.get("effort", "low")
    cfg["prompt_file"] = (entry.get("prompt_file") or cfg.get("prompt_file")
                          or f"prompts/{mode}.md")
    cfg["polish_fallback"] = "off"
    return cfg


def startup_entries(matrix: dict, modes: list[str]) -> list[dict]:
    """Унікальні (бекенд, модель) з матриці — старт міряється один раз на пару."""
    seen: dict[tuple, dict] = {}
    for mode in modes:
        for entry in matrix[mode]:
            seen.setdefault((entry["backend"], entry["model"]), entry)
    return list(seen.values())


def build_plan(texts: dict, matrix: dict, modes: list[str]) -> list[dict]:
    """Список викликів у порядку виконання: спершу проби старту, далі для
    кожного тексту — усі конфігурації поспіль (так зміна навантаження сервісу
    з часом б'є по всіх конфігураціях однаково, а не по одній)."""
    plan = []
    for entry in startup_entries(matrix, modes):
        cfg = build_config("clean", entry)
        cfg["prompt_file"] = STARTUP_PROMPT_FILE
        for i in range(1, STARTUP_PROBES + 1):
            plan.append({"mode": STARTUP_MODE, "slug": slugify(entry), "i": i,
                         "text": STARTUP_TEXT, "config": cfg})
    for mode in modes:
        for i, text in enumerate(texts[mode], 1):
            for entry in matrix[mode]:
                plan.append({"mode": mode, "slug": slugify(entry), "i": i,
                             "text": text, "config": build_config(mode, entry)})
    return plan


# ── Виконання ──────────────────────────────────────────────────────────────

def fence(text: str) -> str:
    """Вихід prompt-режиму сам містить «## Контекст» тощо — без огородження він
    ламав би структуру REVIEW-файлу."""
    return f"~~~~markdown\n{text}\n~~~~"


def run_call(call: dict) -> dict:
    """Один виклик функції бекенда напряму — без with_retries і fallback:
    повтори приховали б справжній час і справжні збої."""
    cfg = call["config"]
    backend = POLISH_BACKENDS[cfg["polish"]]
    t0 = time.monotonic()
    try:
        output = backend(call["text"], cfg)
        ok, error = True, ""
    except Exception as exc:
        output, ok, error = "", False, _brief_error(exc)
    seconds = time.monotonic() - t0
    return {**{k: call[k] for k in ("mode", "slug", "i", "text")},
            "chars_in": len(call["text"]), "chars_out": len(output),
            "seconds": seconds, "ok": ok, "error": error, "output": output}


def write_call_file(out: Path, result: dict) -> None:
    path = out / result["mode"] / result["slug"] / f"{result['i']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    status = (f"{result['seconds']:.1f} с" if result["ok"]
              else f"ПОМИЛКА за {result['seconds']:.1f} с")
    body = fence(result["output"]) if result["ok"] else result["error"]
    path.write_text(
        f"# {result['mode']} · {result['slug']} · текст {result['i']}\n\n"
        f"## Вхід ({result['chars_in']} символів)\n\n{fence(result['text'])}\n\n"
        f"## Вихід ({status})\n\n{body}\n",
        encoding="utf-8",
    )


def run_plan(plan: list[dict], out: Path) -> tuple[list[dict], bool]:
    """Послідовний прогін. Кожен результат одразу пишеться на диск (файл
    виклику + рядок timings.csv) — Ctrl+C чи збій посеред прогону не губить
    уже витрачену квоту. Повертає (результати, чи перервано)."""
    results: list[dict] = []
    interrupted = False
    with (out / "timings.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["mode", "config", "i", "chars_in", "chars_out",
                         "seconds", "status"])
        f.flush()
        try:
            for n, call in enumerate(plan, 1):
                if n > 1:
                    time.sleep(PAUSE_SEC)
                result = run_call(call)
                results.append(result)
                write_call_file(out, result)
                writer.writerow([
                    result["mode"], result["slug"], result["i"],
                    result["chars_in"], result["chars_out"],
                    f"{result['seconds']:.2f}",
                    "ok" if result["ok"] else f"error: {result['error']}",
                ])
                f.flush()
                status = "ok" if result["ok"] else f"ПОМИЛКА: {result['error']}"
                print(f"[{n}/{len(plan)}] {result['mode']} {result['slug']} "
                      f"#{result['i']} ({result['chars_in']} симв.) → "
                      f"{result['seconds']:.1f} с, {status}", flush=True)
        except KeyboardInterrupt:
            interrupted = True
            print(f"\nПерервано: збережено {len(results)} з {len(plan)} викликів.")
    return results, interrupted


# ── Звіти ──────────────────────────────────────────────────────────────────

def _p90(values: list[float]) -> float:
    if len(values) < 2:
        return values[0]
    return statistics.quantiles(values, n=10, method="inclusive")[-1]


def startup_seconds(results: list[dict]) -> dict[str, float]:
    """slug моделі (бекенд-модель) → медіана успішних проб старту."""
    by_pair: dict[str, list[float]] = {}
    for r in results:
        if r["mode"] == STARTUP_MODE and r["ok"]:
            by_pair.setdefault(_pair(r["slug"]), []).append(r["seconds"])
    return {pair: statistics.median(v) for pair, v in by_pair.items()}


def _pair(slug: str) -> str:
    """Проби старту робляться на (бекенд, модель) — effort у slug відкидаємо,
    щоб зіставити пробу з конфігурацією будь-якого effort."""
    return slug.rsplit("-", 1)[0]


def write_review(out: Path, mode: str, texts: list[str], matrix_mode: list[dict],
                 results: list[dict]) -> None:
    by_key = {(r["slug"], r["i"]): r for r in results if r["mode"] == mode}
    parts = [f"# Ручна оцінка якості: режим {mode}\n",
             "Для кожного тексту — оригінал, далі вихід кожної конфігурації. "
             "Дивитися: повноту, вигадані пункти, формат, терміни.\n"]
    for i, text in enumerate(texts, 1):
        parts.append(f"## Текст {i} ({len(text)} символів)\n\n### Оригінал\n\n"
                     f"{fence(text)}\n")
        for entry in matrix_mode:
            slug = slugify(entry)
            r = by_key.get((slug, i))
            if r is None:
                parts.append(f"### {slug}\n\n(не виконано)\n")
            elif r["ok"]:
                parts.append(f"### {slug} ({r['seconds']:.1f} с, "
                             f"{r['chars_out']} символів)\n\n{fence(r['output'])}\n")
            else:
                parts.append(f"### {slug} — ПОМИЛКА ({r['seconds']:.1f} с)\n\n"
                             f"{r['error']}\n")
    (out / f"REVIEW-{mode}.md").write_text("\n".join(parts), encoding="utf-8")


def write_report(out: Path, modes: list[str], matrix: dict, plan_size: int,
                 results: list[dict], interrupted: bool) -> None:
    startup = startup_seconds(results)
    lines = [f"# Бенчмарк шліфування — {datetime.date.today().isoformat()}\n"]
    if interrupted:
        lines.append(f"**Прогін перервано:** виконано {len(results)} з "
                     f"{plan_size} викликів.\n")
    lines += [
        "Час — стінний, секунди, лише успішні виклики. «с/1000» — сумарний час "
        "на 1000 символів входу; «без старту» — те саме після віднімання "
        "startup_sec з кожного виклику (нахил, що залежить від довжини).\n",
        "| config | mode | n | помилок | медіана, с | p90, с | с/1000 | "
        "с/1000 без старту | startup_sec |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for mode in modes:
        for entry in matrix[mode]:
            slug = slugify(entry)
            rows = [r for r in results if r["mode"] == mode and r["slug"] == slug]
            ok = [r for r in rows if r["ok"]]
            errors = len(rows) - len(ok)
            st = startup.get(_pair(slug))
            st_cell = f"{st:.1f}" if st is not None else "—"
            if not ok:
                lines.append(f"| {slug} | {mode} | 0 | {errors} | — | — | — | — "
                             f"| {st_cell} |")
                continue
            secs = [r["seconds"] for r in ok]
            chars = sum(r["chars_in"] for r in ok)
            per_k = sum(secs) / chars * 1000
            net = (f"{max(sum(secs) - st * len(ok), 0.0) / chars * 1000:.1f}"
                   if st is not None else "—")
            lines.append(
                f"| {slug} | {mode} | {len(ok)} | {errors} | "
                f"{statistics.median(secs):.1f} | {_p90(secs):.1f} | "
                f"{per_k:.1f} | {net} | {st_cell} |"
            )

    lines.append("\n## Старт CLI\n")
    lines.append(f"{STARTUP_PROBES} проби на (бекенд, модель), вхід "
                 f"«{STARTUP_TEXT}», інструкція {STARTUP_PROMPT_FILE}; "
                 "startup_sec — медіана успішних.\n")
    lines.append("| бекенд-модель | проби, с | startup_sec |")
    lines.append("|---|---|---|")
    for entry in startup_entries(matrix, modes):
        pair = _pair(slugify(entry))
        probes = [r for r in results
                  if r["mode"] == STARTUP_MODE and _pair(r["slug"]) == pair]
        cells = ", ".join(f"{r['seconds']:.1f}" if r["ok"] else "помилка"
                          for r in probes) or "—"
        st = startup.get(pair)
        lines.append(f"| {pair} | {cells} | {f'{st:.1f}' if st is not None else '—'} |")

    lines.append("\n## Помилки\n")
    failed = [r for r in results if not r["ok"]]
    if not failed:
        lines.append("Немає.")
    for r in failed:
        lines.append(f"- {r['mode']} · {r['slug']} · #{r['i']} "
                     f"({r['seconds']:.1f} с): {r['error']}")
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── CLI ────────────────────────────────────────────────────────────────────

def print_plan(texts: dict, matrix: dict, modes: list[str], plan: list[dict],
               skipped_legacy: int, max_calls: int) -> None:
    print(f"Записів старого формату (без режиму) пропущено: {skipped_legacy}")
    for mode in modes:
        print(f"\nРежим {mode}: {len(texts[mode])} текстів")
        for i, text in enumerate(texts[mode], 1):
            preview = text[:60].replace("\n", " ")
            print(f"  {i}. {len(text):5d} симв. │ {preview}")
        print("  Конфігурації:")
        for entry in matrix[mode]:
            print(f"    - {slugify(entry)}")
    starts = startup_entries(matrix, modes)
    print(f"\nПроби старту: {len(starts)} пар × {STARTUP_PROBES} = "
          f"{len(starts) * STARTUP_PROBES}")
    for entry in starts:
        print(f"    - {_pair(slugify(entry))}")
    breakdown = " + ".join(
        [f"{len(texts[m])}×{len(matrix[m])}" for m in modes]
        + [f"{STARTUP_PROBES}×{len(starts)}"]
    )
    print(f"\nУсього викликів: {breakdown} = {len(plan)} (стеля --max-calls {max_calls})")
    if len(plan) > max_calls:
        print("⚠️ План перевищує стелю — без --dry-run запуск буде відхилено.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Бенчмарк шліфування UkrFlow (витрачає квоту підписок!)")
    parser.add_argument("--log", type=Path, default=LOG_PATH,
                        help="звідки брати сирі тексти (типово ukrflow.log)")
    parser.add_argument("--modes", default="clean,prompt")
    parser.add_argument("--texts-per-mode", type=int, default=5)
    parser.add_argument("--configs", type=Path,
                        help="JSON {режим: [{backend, model, effort}, …]}; "
                             "типово — вбудований пресет")
    parser.add_argument("--out", type=Path,
                        help="тека результатів (типово runs/<дата>-bench-polish)")
    parser.add_argument("--max-calls", type=int, default=45)
    parser.add_argument("--dry-run", action="store_true",
                        help="лише надрукувати план, нічого не викликати")
    args = parser.parse_args(argv)

    modes = []
    for mode in (m.strip() for m in args.modes.split(",")):
        if not mode:
            continue
        if mode == "raw":
            # raw не шліфується за визначенням — порівнювати нічого
            print("Режим raw пропущено: у ньому немає шліфування.")
            continue
        modes.append(mode)
    if not modes:
        raise SystemExit("Немає режимів для бенчмарку.")

    matrix = (json.loads(args.configs.read_text(encoding="utf-8"))
              if args.configs else DEFAULT_MATRIX)
    validate_matrix(matrix, modes)

    entries, skipped_legacy = parse_log(args.log)
    texts = select_texts(entries, modes, args.texts_per_mode,
                         DEFAULT_CONFIG["polish_min_chars"])
    plan = build_plan(texts, matrix, modes)

    print_plan(texts, matrix, modes, plan, skipped_legacy, args.max_calls)
    if args.dry_run:
        return 0
    if len(plan) > args.max_calls:
        print(f"Відмова: план із {len(plan)} викликів перевищує --max-calls "
              f"{args.max_calls}. Зменште матрицю/--texts-per-mode або підніміть стелю.")
        return 2

    out = args.out or (ROOT / "runs"
                       / f"{datetime.date.today().isoformat()}-bench-polish")
    if (out / "timings.csv").exists():
        # Попередній прогін — це вже витрачена квота; не затирати його мовчки
        raise SystemExit(f"{out} уже містить результати — вкажіть іншу --out.")
    out.mkdir(parents=True, exist_ok=True)
    print(f"\nРезультати: {out}\n")

    results, interrupted = run_plan(plan, out)
    for mode in modes:
        write_review(out, mode, texts[mode], matrix[mode], results)
    write_report(out, modes, matrix, len(plan), results, interrupted)
    print(f"\nЗвіт: {out / 'REPORT.md'}")
    return 130 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
