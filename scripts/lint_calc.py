"""Линтер частых калек — чинит то, что не нужно гонять через нейронку.

AUDIT_PROMPT — это вызов модели: время и токены на каждую строку. Но часть
калек повторяется буквально одними и теми же конструкциями («является
возможным» → «можно», «для того чтобы» → «чтобы»). Их ловит обычный regex
без модели, ДО аудита.

Правило: линтер чинит только однозначные, не зависящие от контекста замены.
Если замена может сломать смысл — её место в AUDIT_PROMPT, где модель видит
всю строку. Поэтому список замен намеренно короткий и консервативный.

    python3 lint_calc.py work/mods_ru.json              # отчёт
    python3 lint_calc.py work/mods_ru.json --fix        # починить на месте
    python3 lint_calc.py work/mods_ru.json --fix -o out.json
    python3 lint_calc.py --pairs work/pairs.json        # [{"en":..,"ru":..}]

Что делает:
  1. чинит однозначные кальки по словарю;
  2. ловит кириллицу внутри плейсхолдера (%с) — как `warn`, НЕ правит
     (это уже покрыто SYSTEM_PROMPT §6 и TRAPS.md п.24, здесь подстраховка);
  3. не трогает §-коды, токены [[N]], теги, числа и разметку.

Код возврата: 0 — чисто, 1 — остались `warn`-находки.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ─────────────────────────── словарь однозначных замен ────────────────────
# Только то, что нельзя испортить контекстом. Порядок важен: длинные раньше.
CALQUES: list[tuple[str, str, str]] = [
    # (regex, замена, пояснение)
    # Внимание: порядок важен — длинные конструкции раньше коротких,
    # иначе «на данный момент» превратится в «на этот момент».
    (r"\bна данный момент\b", "сейчас", "на данный момент → сейчас"),
    (r"\bв настоящее время\b", "сейчас", "в настоящее время → сейчас"),
    (r"\bявляет(?:ся|ются) возможным\b", "можно", "является возможным → можно"),
    (r"\bявляет(?:ся|ются) невозможным\b", "нельзя", "является невозможным → нельзя"),
    (r"\bделает возможным\b", "позволяет", "делает возможным → позволяет"),
    (r"\bдля того чтобы\b", "чтобы", "для того чтобы → чтобы"),
    (r"\bс целью того чтобы\b", "чтобы", "с целью того чтобы → чтобы"),
    (r"\bв том случае если\b", "если", "в том случае если → если"),
    (r"\bв случае если\b", "если", "в случае если → если"),
    (r"\bв случае когда\b", "когда", "в случае когда → когда"),
    (r"\bна постоянной основе\b", "постоянно", "на постоянной основе → постоянно"),
    (r"\bв целях\b", "чтобы", "в целях → чтобы"),
    (r"\bпо причине того что\b", "потому что", "по причине того что → потому что"),
    (r"\bв связи с тем что\b", "потому что", "в связи с тем что → потому что"),
    (r"\bтаким образом\b", "так", "таким образом → так"),
    (r"\bданный\s+(\w+)", r"этот \1", "данный → этот"),
    # «является» → «—» рискует сломать фразу: применяется только если строка
    # не становится грамматически битой (нет двух тире подряд).
    (r"\bявляется\b", "—", "является → — (перестрой фразу)"),
]

# «является» → «—» рискует сломать фразу, поэтому вынесено в отдельный
# список: применяется только в самом конце и только если строка не стала
# грамматически битой (проверка простая — нет двух тире подряд).
RISKY = {r"\bявляется\b"}

COMPILED = [(re.compile(p, re.I), r, note) for p, r, note in CALQUES]

# кириллица внутри плейсхолдера — только сигнал, не правим
CYR_PLACEHOLDER = re.compile(r"%(?:\d+\$)?[-+0#]*\d*(?:\.\d+)?[сСіїеоОдД]")

# что нельзя испортить: §-коды, токены, теги, идентификаторы
PROTECT = re.compile(
    r"(§.|&[0-9a-fk-or]|\[\[\d+\]\]|<[^>]{0,80}>|"
    r"%(?:\d+\$)?[-+0#]*\d*(?:\.\d+)?[sdif]|\{\d*\}|"
    r"[a-z_]+:[a-z0-9_/]+|https?://\S+)")


def _mask(text: str) -> tuple[str, list[str]]:
    tokens: list[str] = []

    def repl(m: re.Match) -> str:
        tokens.append(m.group(0))
        return f"\x00{len(tokens) - 1}\x00"

    return PROTECT.sub(repl, text), tokens


def _unmask(text: str, tokens: list[str]) -> str:
    return re.sub(r"\x00(\d+)\x00", lambda m: tokens[int(m.group(1))], text)


def lint_line(ru: str) -> tuple[str, list[str]]:
    """Возвращает (исправленная строка, список примененных пояснений)."""
    if not ru or not re.search(r"[а-яёА-ЯЁ]", ru):
        return ru, []
    masked, tokens = _mask(ru)
    applied: list[str] = []
    out = masked
    for rx, repl, note in COMPILED:
        if rx.pattern in RISKY:
            continue
        new, n = rx.subn(repl, out)
        if n:
            out = new
            applied.append(f"{note} ×{n}")
    # рискованная замена — только если не создаёт двойного тире
    for rx, repl, note in COMPILED:
        if rx.pattern not in RISKY:
            continue
        candidate, n = rx.subn(repl, out)
        if n and "——" not in candidate and "— —" not in candidate:
            out = candidate
            applied.append(f"{note} ×{n}")
    out = _unmask(out, tokens)
    # косметика после замен: двойные пробелы и пробел перед запятой
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    return out, applied


def warn_line(ru: str) -> list[str]:
    """Находки, которые линтер НЕ правит, а только сигналит."""
    out = []
    if CYR_PLACEHOLDER.search(ru):
        out.append(f"кириллица в плейсхолдере: {CYR_PLACEHOLDER.search(ru).group(0)!r}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Линтер частых калек до аудита")
    ap.add_argument("path", nargs="?", help="JSON-словарь {en: ru}")
    ap.add_argument("--pairs", help="JSON-список [{'en':..,'ru':..}]")
    ap.add_argument("--fix", action="store_true", help="записать исправления")
    ap.add_argument("-o", "--out", help="куда писать результат (по умолчанию на месте)")
    ap.add_argument("--limit", type=int, default=20, help="сколько примеров показать")
    args = ap.parse_args()

    if args.pairs:
        pairs = json.loads(Path(args.pairs).read_text(encoding="utf-8"))
        data = {p["en"]: p["ru"] for p in pairs if "en" in p and "ru" in p}
    elif args.path:
        data = json.loads(Path(args.path).read_text(encoding="utf-8"))
    else:
        ap.error("нужен путь к словарю или --pairs")

    if not isinstance(data, dict):
        print("ожидался объект {en: ru}", file=sys.stderr)
        return 2

    fixed = 0
    warned = 0
    shown = 0
    changes: dict[str, str] = {}

    for en, ru in data.items():
        if not isinstance(ru, str):
            continue
        new, applied = lint_line(ru)
        warns = warn_line(new)
        if applied and new != ru:
            fixed += 1
            changes[en] = new
            if shown < args.limit:
                print(f"  калька: {ru[:70]!r}\n       → {new[:70]!r}   ({', '.join(applied)})")
                shown += 1
        if warns:
            warned += 1
            if shown < args.limit:
                print(f"  warn:   {en[:50]!r} — {'; '.join(warns)}")
                shown += 1

    print(f"\nстрок: {len(data)} | починено калек: {fixed} | warn (не правится): {warned}")

    if args.fix and changes:
        for en, new in changes.items():
            data[en] = new
        dst = Path(args.out) if args.out else Path(args.path or args.pairs)
        dst.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                       encoding="utf-8")
        print(f"записано: {dst}")
    elif args.fix:
        print("чинить нечего")

    return 1 if warned else 0


if __name__ == "__main__":
    raise SystemExit(main())
