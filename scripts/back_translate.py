"""Обратный перевод как дешёвый фильтр смысловых ошибок.

Проблема: в подсказках предметов ошибка в знаке («уменьшает» вместо
«увеличивает») или переставленные числа дороже всего — игрок покупает
предмет с неверной механикой. AUDIT_PROMPT такие ошибки находит (у него
пункт «Смысловая ошибка»), но гонять аудит по всем строкам дорого.

Решение: до аудита прогнать только строки с механикой — перевести готовый
русский обратно на английский отдельным маленьким промтом (RU→EN, без
подсказки «это уже правильно») и сравнить с оригиналом по сути:
  • знак направления эффекта (усиливает / ослабляет);
  • порядок чисел и условий;
  • наличие отрицания.

Несовпадение → строка помечается `mismatch` и уходит в аудит точечно.

    python3 back_translate.py pairs.json -o work/mismatch.json
    python3 back_translate.py pairs.json --dry-run

pairs.json — список [{"en": "...", "ru": "...", "key": "..."}].
Нужен ключ движка (HMS_API_KEY / .deepseek_key), как у deepseek_backend.py.

Файл на выходе — готовый вход для AUDIT_PROMPT: у каждой находки есть `id`,
`en`, `ru`, `flag: "back-translation"` и `problems` (что разошлось). Аудит
по этой пометке тратит время точечно, а не на весь пакет заново.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ─────────────────────────── отбор строк с механикой ──────────────────────

MECH_WORDS = (
    "damage", "dmg", "speed", "chance", "increase", "decrease", "reduce",
    "resistance", "armor", "health", "heal", "attack", "rate", "duration",
    "cooldown", "radius", "range", "multiply", "bonus", "penalty", "negate",
    "immune", "ignore", "steal", "drain", "amplif", "weaken", "boost",
    "percent", "probability", "per second", "per hit", "per level",
)
MECH_RE = re.compile(r"\b(?:" + "|".join(MECH_WORDS) + r")\b", re.I)
NUMBER = re.compile(r"[+\-±]?\d+(?:[.,]\d+)?\s*%?")
NEGATION_EN = re.compile(r"\b(?:not|no|never|cannot|can't|won't|doesn't|isn't|without)\b", re.I)
NEGATION_RU = re.compile(r"\b(?:не|нет|никогда|нельзя|без)\b", re.I)
UP_EN = re.compile(r"\b(?:increase|increases|raise|raises|boost|boosts|amplif\w*|more|add|adds|gain|gains|up)\b", re.I)
DOWN_EN = re.compile(r"\b(?:decrease|decreases|reduce|reduces|lower|lowers|less|weaken\w*|drain\w*|down|penalty)\b", re.I)
UP_RU = re.compile(r"\b(?:увелич\w*|повыш\w*|усил\w*|прибав\w*|больше|добав\w*)\b", re.I)
DOWN_RU = re.compile(r"\b(?:уменьш\w*|пониж\w*|ослаб\w*|сниж\w*|меньше|штраф\w*)\b", re.I)


def looks_mechanical(en: str) -> bool:
    """Есть ли в строке механика, которую дорого переврать."""
    if not en or len(en) < 8:
        return False
    if "%" in en or NUMBER.search(en):
        return True
    return bool(MECH_RE.search(en))


# ─────────────────────────── сравнение по сути ────────────────────────────

def _numbers(text: str) -> list[str]:
    return [n.replace(",", ".").strip() for n in NUMBER.findall(text or "")]


def compare(en: str, back: str) -> list[str]:
    """Возвращает список расхождений между оригиналом и обратным переводом."""
    problems: list[str] = []

    # 1. знак направления эффекта
    en_up, en_down = bool(UP_EN.search(en)), bool(DOWN_EN.search(en))
    bk_up, bk_down = bool(UP_RU.search(back)), bool(DOWN_RU.search(back))
    # обратный перевод идёт на английский, но модель может ответить и по-русски
    bk_up_en, bk_down_en = bool(UP_EN.search(back)), bool(DOWN_EN.search(back))
    bk_up = bk_up or bk_up_en
    bk_down = bk_down or bk_down_en
    if (en_up and bk_down and not bk_up) or (en_down and bk_up and not bk_down):
        problems.append("направление эффекта перевёрнуто")

    # 2. отрицание
    if bool(NEGATION_EN.search(en)) != bool(NEGATION_RU.search(back) or NEGATION_EN.search(back)):
        problems.append("отрицание потеряно или добавлено")

    # 3. числа
    en_n, bk_n = _numbers(en), _numbers(back)
    if sorted(en_n) != sorted(bk_n):
        problems.append(f"числа: было {en_n}, стало {bk_n}")

    return problems


# ─────────────────────────── обратный перевод ─────────────────────────────

BACK_SYSTEM = ("Ты — проверяющий переводчик. Твоя работа — понять, что написано "
               "в русской строке, и передать это по-английски буквально. "
               "Не улучшай, не додумывай, не адаптируй.")

BACK_PROMPT = """Переведи русские строки игровых подсказок Minecraft обратно на английский.
Это проверка: нужно передать смысл буквально, а не сделать красиво.
Верни ТОЛЬКО английский текст, без пояснений и кавычек. Форматирование
(§-коды, %s, <теги>) сохрани как есть.

Строк: {n}. Верни JSON-массив строк в том же порядке."""


def _backend():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from deepseek_backend import DeepSeekBackend  # noqa: PLC0415
    return DeepSeekBackend(temperature=0.0)


def back_translate(lines: list[str], batch: int = 20) -> list[str]:
    """Переводит русские строки обратно на английский."""
    import concurrent.futures as cf

    be = _backend()
    out: list[str] = [""] * len(lines)
    chunks = [(i, lines[i:i + batch]) for i in range(0, len(lines), batch)]

    def do(item: tuple[int, list[str]]) -> tuple[int, list[str]]:
        start, part = item
        prompt = BACK_PROMPT.format(n=len(part)) + "\n" + json.dumps(part, ensure_ascii=False)
        for _ in range(3):
            try:
                raw = be._request_raw(prompt, system=BACK_SYSTEM, temperature=0.0)  # noqa: SLF001
                data = json.loads(raw[raw.find("["):raw.rfind("]") + 1])
                if isinstance(data, list) and len(data) == len(part):
                    return start, [str(x) for x in data]
            except Exception:  # noqa: BLE001
                pass
        return start, list(part)

    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        for start, got in pool.map(do, chunks):
            out[start:start + len(got)] = got
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Обратный перевод как фильтр смысловых ошибок")
    ap.add_argument("pairs", help="JSON: [{'en':..,'ru':..,'key':..}]")
    ap.add_argument("-o", "--out", default="work/mismatch.json")
    ap.add_argument("--dry-run", action="store_true", help="только отобрать строки")
    ap.add_argument("--limit", type=int, default=15, help="сколько примеров показать")
    args = ap.parse_args()

    pairs = json.loads(Path(args.pairs).read_text(encoding="utf-8"))
    cand = [p for p in pairs
            if isinstance(p.get("en"), str) and isinstance(p.get("ru"), str)
            and p["ru"].strip() and p["ru"] != p["en"]
            and looks_mechanical(p["en"])]

    print(f"всего пар: {len(pairs)} | с механикой: {len(cand)}")
    if args.dry_run or not cand:
        for p in cand[:args.limit]:
            print(f"  {p.get('key', '')[:50]:50} {p['en'][:60]!r}")
        return 0

    backs = back_translate([p["ru"] for p in cand])
    findings = []
    for p, back in zip(cand, backs):
        problems = compare(p["en"], back)
        if problems:
            findings.append({
                "id": len(findings),
                "key": p.get("key", ""),
                "en": p["en"],
                "ru": p["ru"],
                "back": back,
                "flag": "back-translation",
                "problems": problems,
            })

    dst = Path(args.out)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(findings, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"проверено: {len(cand)} | расхождений: {len(findings)}")
    for f in findings[:args.limit]:
        print(f"\n  {f['key'][:60]}")
        print(f"    EN:   {f['en'][:90]!r}")
        print(f"    RU:   {f['ru'][:90]!r}")
        print(f"    BACK: {f['back'][:90]!r}")
        print(f"    → {', '.join(f['problems'])}")
    print(f"\nзаписано: {dst}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
