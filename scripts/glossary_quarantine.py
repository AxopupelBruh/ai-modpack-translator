"""Карантин кандидатов в глоссарий: один удачный перевод — ещё не истина.

Проблема: если термин попадает в активный глоссарий по одному упоминанию,
кривой перевод одной строки становится «источником истины» для всей сборки
и расходится по сотням строк, которые модель уже не перепроверит.

Решение — три слоя:
    data/glossary.json            активный глоссарий, подставляется в перевод
    data/glossary_candidates.json черновик: копится сам, в перевод НЕ идёт
    data/adaptation_examples.json решения пользователя по шуткам и идиомам

Промоушен кандидата в активный глоссарий — только вручную:

    python3 glossary_quarantine.py add batch.json      # накопить кандидатов
    python3 glossary_quarantine.py list                # что готово к ревью
    python3 glossary_quarantine.py check "Soul Forge"  # одиночная проверка через AUDIT
    python3 glossary_quarantine.py promote "Soul Forge" --by Ax
    python3 glossary_quarantine.py reject "Soul Forge" --why "кривой перевод"

batch.json — ответ модели: {"candidates": [{"en":..,"ru":..,"source_id":N}]}

Проверка `check` выдаёт готовый вход для AUDIT_PROMPT: одна пара без контекста
и с пометкой `flag: "glossary-candidate"`. Если модель без подсказки о
«принятости» термина выдаёт тот же вариант — это подтверждение, что перевод
не случаен. Запуск аудита:

    python3 glossary_quarantine.py check "Soul Forge" > work/cand.json
    # отдать work/cand.json в prompts/AUDIT_PROMPT.txt

Правила (жёсткие, из ТЗ):
  • термин не промотируется, если встретился меньше 3 раз в разных строках;
  • термин не промотируется, если для него накопилось больше одного
    ru-варианта (это конфликт — его решает человек или AUDIT_PROMPT);
  • происхождение хранится: дата, source_id, кто подтвердил — чтобы
    термин можно было найти и откатить.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"

ACTIVE = DATA / "glossary.json"
CANDIDATES = DATA / "glossary_candidates.json"

MIN_HITS = 3
MIN_SOURCES = 3


def _load(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return default


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                    encoding="utf-8")


def add(batch_path: Path) -> int:
    """Накопить кандидатов из ответа модели. В активный глоссарий не пишет."""
    raw = json.loads(batch_path.read_text(encoding="utf-8"))
    cands = raw.get("candidates") if isinstance(raw, dict) else raw
    if not isinstance(cands, list):
        print("в файле нет поля candidates", file=sys.stderr)
        return 2

    active = _load(ACTIVE, {})
    store = _load(CANDIDATES, {})
    added = 0
    for c in cands:
        en = (c.get("en") or "").strip()
        ru = (c.get("ru") or "").strip()
        if not en or not ru:
            continue
        if en in active:
            continue  # уже принят — кандидат не нужен
        entry = store.setdefault(en, {
            "variants": {},
            "source_ids": [],
            "first_seen": str(date.today()),
        })
        entry["variants"].setdefault(ru, 0)
        entry["variants"][ru] += 1
        sid = c.get("source_id")
        if sid is not None and sid not in entry["source_ids"]:
            entry["source_ids"].append(sid)
        added += 1

    _save(CANDIDATES, store)
    print(f"кандидатов в файле: {len(store)} (добавлено упоминаний: {added})")
    return 0


def _status(entry: dict) -> tuple[str, str]:
    """Готов ли кандидат к промоушену, и если нет — почему."""
    variants = entry.get("variants", {})
    total = sum(variants.values())
    ids = entry.get("source_ids", [])
    if len(variants) > 1:
        names = ", ".join(f"{k!r}×{v}" for k, v in sorted(variants.items(), key=lambda x: -x[1]))
        return "conflict", f"разные варианты: {names}"
    if total < MIN_HITS:
        return "young", f"встретился {total} раз(а), нужно {MIN_HITS}"
    if len(ids) < MIN_SOURCES:
        return "young", f"строк-источников {len(ids)}, нужно {MIN_SOURCES}"
    return "ready", "готов к ревью"


def list_candidates(show_all: bool = False) -> int:
    store = _load(CANDIDATES, {})
    if not store:
        print("кандидатов нет")
        return 0
    buckets: dict[str, list[tuple[str, str, dict]]] = {"ready": [], "conflict": [], "young": []}
    for en, entry in store.items():
        st, why = _status(entry)
        buckets[st].append((en, why, entry))

    print(f"кандидатов: {len(store)} | готово: {len(buckets['ready'])} | "
          f"конфликтов: {len(buckets['conflict'])} | мало данных: {len(buckets['young'])}")

    for name, title in (("ready", "ГОТОВЫ К РЕВЬЮ"), ("conflict", "КОНФЛИКТ — решает человек"),
                        ("young", "МАЛО ДАННЫХ")):
        items = buckets[name]
        if not items:
            continue
        print(f"\n=== {title} ({len(items)})")
        for en, why, entry in items[:50 if show_all else 12]:
            best = max(entry["variants"].items(), key=lambda x: x[1])[0]
            print(f"  {en!r} → {best!r}")
            print(f"     {why}; источник: {entry['source_ids'][:6]}")
    return 0


def check(en: str) -> int:
    """Отдать кандидата на одиночную проверку через AUDIT_PROMPT.

    Печатает JSON-массив из одной пары с пометкой `flag: "glossary-candidate"`.
    Модель не знает, что термин «принят»: если без подсказки выдаёт тот же
    вариант — перевод не случайный. Перенаправь вывод в файл и отдай аудиту.
    """
    store = _load(CANDIDATES, {})
    entry = store.get(en)
    if not entry:
        print(f"кандидата {en!r} нет в карантине", file=sys.stderr)
        return 2
    variants = entry.get("variants", {})
    best = max(variants.items(), key=lambda x: x[1])[0] if variants else ""
    payload = [{"id": 0, "en": en, "ru": best, "flag": "glossary-candidate"}]
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


def promote(en: str, ru: str | None = None, by: str = "", force: bool = False) -> int:
    store = _load(CANDIDATES, {})
    entry = store.get(en)
    if not entry:
        print(f"кандидата {en!r} нет в карантине", file=sys.stderr)
        return 2
    st, why = _status(entry)
    if st != "ready" and not force:
        print(f"нельзя промотировать: {why}\n(если уверены — повторите с --force)",
              file=sys.stderr)
        return 1

    variants = entry["variants"]
    chosen = ru or max(variants.items(), key=lambda x: x[1])[0]
    if chosen not in variants:
        print(f"вариант {chosen!r} не встречался у кандидата", file=sys.stderr)
        return 2

    active = _load(ACTIVE, {})
    active[en] = chosen
    _save(ACTIVE, active)

    # происхождение — отдельным файлом, чтобы термин можно было откатить
    prov_path = DATA / "glossary_provenance.json"
    prov = _load(prov_path, {})
    prov[en] = {
        "ru": chosen,
        "variants": variants,
        "source_ids": entry.get("source_ids", []),
        "first_seen": entry.get("first_seen"),
        "promoted": str(date.today()),
        "promoted_by": by or "unknown",
        "forced": bool(force),
    }
    _save(prov_path, prov)

    del store[en]
    _save(CANDIDATES, store)
    print(f"промотировано: {en!r} → {chosen!r} (подтвердил: {by or 'unknown'})")
    print("откат: удалить запись из data/glossary.json и data/glossary_provenance.json")
    return 0


def reject(en: str, why: str = "") -> int:
    store = _load(CANDIDATES, {})
    if en not in store:
        print(f"кандидата {en!r} нет", file=sys.stderr)
        return 2
    rejected_path = DATA / "glossary_rejected.json"
    rej = _load(rejected_path, {})
    rej[en] = {"why": why, "date": str(date.today()),
               "variants": store[en].get("variants", {})}
    _save(rejected_path, rej)
    del store[en]
    _save(CANDIDATES, store)
    print(f"отклонено: {en!r} — {why or 'без причины'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Карантин кандидатов в глоссарий")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="накопить кандидатов из ответа модели")
    p_add.add_argument("batch", type=Path)

    p_list = sub.add_parser("list", help="показать карантин")
    p_list.add_argument("--all", action="store_true")

    p_chk = sub.add_parser("check", help="отдать кандидата на проверку в AUDIT_PROMPT")
    p_chk.add_argument("en")

    p_pro = sub.add_parser("promote", help="перенести кандидата в активный глоссарий")
    p_pro.add_argument("en")
    p_pro.add_argument("--ru", help="какой вариант принять (по умолчанию частый)")
    p_pro.add_argument("--by", default="", help="кто подтвердил")
    p_pro.add_argument("--force", action="store_true", help="обойти порог и конфликт")

    p_rej = sub.add_parser("reject", help="отклонить кандидата")
    p_rej.add_argument("en")
    p_rej.add_argument("--why", default="")

    args = ap.parse_args()
    if args.cmd == "add":
        return add(args.batch)
    if args.cmd == "list":
        return list_candidates(args.all)
    if args.cmd == "check":
        return check(args.en)
    if args.cmd == "promote":
        return promote(args.en, args.ru, args.by, args.force)
    return reject(args.en, args.why)


if __name__ == "__main__":
    raise SystemExit(main())
