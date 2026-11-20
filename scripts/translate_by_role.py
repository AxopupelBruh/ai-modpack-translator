"""Перевод пачками, сгруппированными по роли строки.

Зачем: SYSTEM_PROMPT (§1) определяет роль строки — тултип это фрагмент,
название это существительное, квест это литературный текст. Если строки
разных ролей идут вперемешку, модели приходится определять роль заново на
каждой строке, и правило одной роли просачивается в соседнюю («тултип —
не предложение» → обрубленное описание квеста).

Решение: роль и так вычисляется скриптом по ключу (roles.py) — сортируем
по ней ДО отправки и шлём отдельными вызовами.

Плюс дедупликация до отправки (TRAPS.md п.4 и ТЗ п.11): одна и та же
английская строка в двух местах сборки переводится ОДИН раз и получает
один перевод. Кэш ловит повторы между прогонами, но внутри одного прогона
повтор надо снимать здесь, иначе две пачки переведут её по-разному.

    python3 translate_by_role.py pairs.json -o work/ru.json
    python3 translate_by_role.py pairs.json -o work/ru.json --cache work/cache.json

pairs.json — [{"key": "item.create.cogwheel", "en": "Cogwheel"}, ...]
Выход — {"Cogwheel": "Зубчатое колесо", ...} плюс отдельно кандидаты
в глоссарий и сомнительные строки (если модель их вернула).

Температура фиксируется низкой (0.1): повторный прогон той же пачки без
изменений в глоссарии должен давать тот же результат.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from roles import ROLES, dedupe_by_text, group_by_role  # noqa: E402

TEMPERATURE = 0.1  # фиксированная: воспроизводимость важнее «творчества»

ROLE_NOTE = {
    "name": "Это НАЗВАНИЯ предметов/блоков/сущностей. Короткое существительное, "
            "без «штука», «вещь», «предмет», без точки в конце.",
    "tooltip": "Это ФРАГМЕНТЫ ПОДСКАЗОК. Не предложение: без точки, регистр первой "
               "буквы как в оригинале, смысл не раздувать.",
    "ui": "Это ЭЛЕМЕНТЫ ИНТЕРФЕЙСА: кнопки, вкладки, заголовки. Коротко, "
          "инфинитив или существительное.",
    "config": "Это ОПЦИИ КОНФИГА: название — короткий заголовок, описание — полное "
              "предложение, объясняющее, что делает опция.",
    "quest": "Это ТЕКСТЫ КВЕСТОВ. Заголовок — коротко, описание — литературно, "
             "можно длинно, как в книге.",
    "chat": "Это СООБЩЕНИЯ В ЧАТ. Живая фраза, как сказал бы игрок.",
    "other": "",
}


def _backend():
    from deepseek_backend import DeepSeekBackend  # noqa: PLC0415
    return DeepSeekBackend(temperature=TEMPERATURE)


def load_cache(path: Path) -> dict[str, str]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=0, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(path)


def translate_role(lines: list[str], role: str, be, batch: int = 20) -> dict[str, str]:
    """Переводит строки одной роли. Внутри пачки роль не меняется."""
    note = ROLE_NOTE.get(role, "")
    out: dict[str, str] = {}
    for i in range(0, len(lines), batch):
        part = lines[i:i + batch]
        try:
            if note:
                prompt = (f"Все строки в этой пачке — одна роль: {role}.\n{note}\n"
                          f"Строк: {len(part)}. Верни JSON "
                          f"{{\"translations\": [...]}} в том же порядке.\n"
                          + json.dumps(part, ensure_ascii=False))
                raw = be._request_raw(prompt, temperature=TEMPERATURE)  # noqa: SLF001
                got = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])["translations"]
            else:
                got = be.translate_batch(part)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! роль {role}, пачка {i}: {exc}", flush=True)
            got = list(part)
        if not isinstance(got, list) or len(got) != len(part):
            print(f"  ! роль {role}, пачка {i}: получено {len(got) if isinstance(got, list) else '?'} "
                  f"вместо {len(part)}", flush=True)
            got = list(part)
        out.update(dict(zip(part, got)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Перевод пачками по роли строки")
    ap.add_argument("pairs", type=Path)
    ap.add_argument("-o", "--out", default="work/ru_by_role.json")
    ap.add_argument("--cache", default="work/cache_by_role.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    raw = json.loads(args.pairs.read_text(encoding="utf-8"))
    pairs = [(p.get("key", ""), p["en"]) for p in raw if isinstance(p.get("en"), str)]
    if not pairs:
        print("нет строк", file=sys.stderr)
        return 2

    # 1. дедупликация по тексту ДО отправки
    unique, keys_by_text = dedupe_by_text(pairs)
    dupes = {t: k for t, k in keys_by_text.items() if len(k) > 1}

    # 2. группировка по роли
    grouped = group_by_role(unique)

    print(f"строк: {len(pairs)} | уникальных: {len(unique)} | "
          f"повторов текста: {len(dupes)}")
    for role in ROLES:
        if role in grouped:
            print(f"  {role:8} {len(grouped[role])}")

    if args.dry_run:
        for t, ks in list(dupes.items())[:10]:
            print(f"\n  повтор: {t[:60]!r}\n     ключи: {ks[:4]}")
        return 0

    cache = load_cache(Path(args.cache))
    todo: dict[str, list[str]] = {}
    for role, items in grouped.items():
        need = [t for _, t in items if t not in cache]
        if need:
            todo[role] = need

    left = sum(len(v) for v in todo.values())
    print(f"в кэше: {len(cache)} | перевести: {left}")
    if not left:
        print("всё уже в кэше")
    else:
        be = _backend()
        for role, lines in todo.items():
            print(f"  роль {role}: {len(lines)}", flush=True)
            cache.update(translate_role(lines, role, be))
            save_cache(Path(args.cache), cache)

    result = {t: cache[t] for t in keys_by_text if t in cache}
    missing = [t for t in keys_by_text if t not in cache]
    dst = Path(args.out)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding="utf-8")
    print(f"\nпереведено: {len(result)} | без перевода: {len(missing)}")
    print(f"записано: {dst}")
    if dupes:
        print(f"дедуплицировано текстов: {len(dupes)} — все ключи получат один перевод")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
