"""Сомнительные строки: шутки, идиомы, отсылки — на ручную адаптацию.

Проблема: перевод шутки — не техническая задача, а вопрос вкуса. Модель не
должна тихо решать его сама и выдавать свой вариант как окончательный.
AUDIT_PROMPT называет такие строки «вольный перевод, а не ошибка», но это
значит лишь «не считай их браком» — решения там всё равно нет.

Поток:
  1. модель возвращает `uncertain` в ответе (SYSTEM_PROMPT §9.3);
  2. этот скрипт складывает их отдельным списком: en и предложенный вариант
     рядом, БЕЗ попадания в основной перевод;
  3. пользователь смотрит пары и либо принимает вариант, либо пишет свой;
  4. принятое уходит в data/adaptation_examples.json — копилку решений.

    python3 collect_uncertain.py add batch.json      # накопить из ответа модели
    python3 collect_uncertain.py list                # что ждёт решения
    python3 collect_uncertain.py accept 12 --ru "Магнитная буря" --note "короче"
    python3 collect_uncertain.py accept 12          # принять вариант модели
    python3 collect_uncertain.py export              # отдать в перевод {en: ru}

Важно: adaptation_examples.json — НЕ глоссарий. Глоссарий (data/glossary.json)
это устойчивые игровые термины, а здесь — примеры вкусовой адаптации шуток
и идиом. Смешивать нельзя: у них разный жизненный цикл.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"

PENDING = DATA / "uncertain_pending.json"
EXAMPLES = DATA / "adaptation_examples.json"


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
    """Накопить сомнительные строки из ответа модели."""
    raw = json.loads(batch_path.read_text(encoding="utf-8"))
    items = raw.get("uncertain") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        print("в файле нет поля uncertain", file=sys.stderr)
        return 2

    pending = _load(PENDING, {})
    examples = _load(EXAMPLES, {})
    added = 0
    for it in items:
        en = (it.get("en") or "").strip()
        if not en:
            continue
        # уже решено раньше — не спрашиваем второй раз
        if en in examples:
            continue
        entry = pending.setdefault(en, {
            "en": en,
            "ru_suggested": (it.get("ru_suggested") or "").strip(),
            "ids": [],
            "first_seen": str(date.today()),
        })
        if it.get("ru_suggested") and not entry["ru_suggested"]:
            entry["ru_suggested"] = it["ru_suggested"].strip()
        sid = it.get("id")
        if sid is not None and sid not in entry["ids"]:
            entry["ids"].append(sid)
        added += 1

    _save(PENDING, pending)
    print(f"ждут решения: {len(pending)} (добавлено упоминаний: {added})")
    return 0


def list_pending(limit: int = 50) -> int:
    pending = _load(PENDING, {})
    if not pending:
        print("сомнительных строк нет")
        return 0
    print(f"ждут решения: {len(pending)}\n")
    for i, (en, entry) in enumerate(list(pending.items())[:limit]):
        print(f"--- {i}")
        print(f"  EN: {en}")
        print(f"  RU: {entry.get('ru_suggested') or '(модель не предложила)'}")
        if entry.get("ids"):
            print(f"  строки: {entry['ids'][:8]}")
    if len(pending) > limit:
        print(f"\n... и ещё {len(pending) - limit}")
    return 0


def accept(index: int, ru: str | None = None, note: str = "", by: str = "") -> int:
    """Принять вариант модели или записать свой."""
    pending = _load(PENDING, {})
    keys = list(pending.keys())
    if index < 0 or index >= len(keys):
        print(f"нет строки с номером {index} (всего {len(keys)})", file=sys.stderr)
        return 2
    en = keys[index]
    entry = pending[en]
    final = (ru or entry.get("ru_suggested") or "").strip()
    if not final:
        print("нечего принимать: у строки нет варианта, укажите --ru", file=sys.stderr)
        return 2

    examples = _load(EXAMPLES, {})
    examples[en] = {"ru": final, "note": note, "accepted": str(date.today()),
                    "by": by or "unknown",
                    "model_variant": entry.get("ru_suggested", "")}
    _save(EXAMPLES, examples)
    del pending[en]
    _save(PENDING, pending)
    print(f"принято: {en!r} → {final!r}")
    print(f"примеров в копилке: {len(examples)}")
    return 0


def export(path: str) -> int:
    """Отдать принятые адаптации как словарь {en: ru} для сборки."""
    examples = _load(EXAMPLES, {})
    out = {en: v["ru"] for en, v in examples.items() if v.get("ru")}
    dst = Path(path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding="utf-8")
    print(f"экспортировано: {len(out)} → {dst}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Сомнительные строки на ручную адаптацию")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="накопить из ответа модели")
    p_add.add_argument("batch", type=Path)

    p_list = sub.add_parser("list", help="показать очередь")
    p_list.add_argument("--limit", type=int, default=50)

    p_acc = sub.add_parser("accept", help="принять строку")
    p_acc.add_argument("index", type=int)
    p_acc.add_argument("--ru", help="свой вариант (иначе берётся вариант модели)")
    p_acc.add_argument("--note", default="")
    p_acc.add_argument("--by", default="")

    p_exp = sub.add_parser("export", help="выгрузить принятые в словарь")
    p_exp.add_argument("path", nargs="?", default="work/adaptation_ru.json")

    args = ap.parse_args()
    if args.cmd == "add":
        return add(args.batch)
    if args.cmd == "list":
        return list_pending(args.limit)
    if args.cmd == "accept":
        return accept(args.index, args.ru, args.note, args.by)
    return export(args.path)


if __name__ == "__main__":
    raise SystemExit(main())
