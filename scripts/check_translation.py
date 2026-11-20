"""Проверка готового перевода по правилам системного промта.

Берёт словарь `{английский: русский}` (или пары из lang-файлов) и говорит,
что в нём нарушено: плейсхолдеры, §-коды, регистр, пунктуация, запятые
в числах, кириллица внутри `%s`, непереведённая проза.

    python3 check_translation.py work/mods_ru.json
    python3 check_translation.py work/mods_ru.json --strict --limit 50
    python3 check_translation.py --lang assets/foo/lang/en_us.json assets/foo/lang/ru_ru.json
    python3 check_translation.py pack.zip            # проверить весь ресурспак

Зачем отдельный скрипт: валидация внутри переводчика видит только то, что
сгенерировала сама. Чужой перевод, вычитку из другого пака и словарь,
правленный руками, она не проверяет. А именно там и появляются «%с»
и съехавшие `§`-коды.

Коды возврата: 0 — чисто, 1 — есть ошибки уровня error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path


def placeholder_slots(text: str) -> tuple[Counter, bool]:
    """Нормализует плейсхолдеры к номерам аргументов.

    `%s %s` — то же самое, что `%1$s %2$s`: Java подставляет аргументы по
    порядку. А `%2$s … %1$s` — законная перестановка слов (так делает и
    официальная русская локализация Minecraft: `%2$s: %1$s`).

    Возвращает (мультимножество номеров аргументов, была ли перестановка).
    """
    slots: Counter = Counter()
    implicit = 0
    explicit = False
    for m in PLACEHOLDER.finditer(text):
        idx = re.match(r"%(\d+)\$", m.group(0))
        if idx:
            explicit = True
            slots[int(idx.group(1))] += 1
        else:
            implicit += 1
            slots[implicit] += 1
    # при смешивании Java требует только позиционные — это отдельная поломка,
    # но такие строки редки и ловятся тем, что порядок номеров не совпал
    reordered = explicit and implicit > 0
    return slots, reordered

# ─────────────────────────── что считаем нарушением ────────────────────────

# %s / %d / %1$s / %.2f — но не %% и не проценты
PLACEHOLDER = re.compile(r"%(?:\d+\$)?[-+0#]*\d*(?:\.\d+)?[sdif]")
# кириллическая буква на месте латинской внутри плейсхолдера: %с, %д
CYR_PLACEHOLDER = re.compile(r"%(?:\d+\$)?[-+0#]*\d*(?:\.\d+)?[сСіїеоОдД]")
SECTION_CODE = re.compile(r"§[0-9a-fk-orA-FK-OR]")
CYR = re.compile(r"[а-яёА-ЯЁ]")
LAT_WORD = re.compile(r"[A-Za-z]{2,}")
LEAD = re.compile(r"^(\s*(?:[§&][0-9a-fk-orA-FK-OR]|\[[^\]]{0,40}\]|<[^>]{0,40}>)*)")

# латиница, которую разрешено оставлять: бренды, единицы, технические токены
ALLOWED_LATIN = {
    "hp", "xp", "mp", "lp", "dps", "cd", "aoe", "pvp", "pve", "npc", "ui", "gui",
    "api", "id", "url", "json", "png", "nbt", "snbt", "mc", "ftb", "jei", "rei",
    "emi", "wthit", "jade", "curios", "create", "mekanism", "botania", "patchouli",
    "geckolib", "embeddium", "apotheosis", "occultism", "ad", "astra", "tinkers",
    "t", "s", "m", "h", "mb", "rf", "fe", "k", "ok", "pvp",
}
ALLOWED_TOKENS = re.compile(
    r"(?:https?://\S+|[\w.-]+\.(?:json|png|jar|toml|nbt|snbt|js|zip)\b|"
    r"[a-z0-9_]+:[a-z0-9_/.]+|/[\w.@-]+(?:\s[\w.@-]+)*|#?[0-9a-fA-F]{6}\b|"
    r"\[\[\d+\]\]|\$\([^)]*\)\{?|&[0-9a-fk-orA-FK-OR])"
)


def _lead(text: str) -> str:
    m = LEAD.match(text)
    return m.group(1) if m else ""


def _first_letter(text: str) -> str:
    """Первая настоящая буква строки — без ведущей разметки и плейсхолдеров.

    `%s's Stone Statue` начинается с плейсхолдера, и наивный поиск буквы даёт
    's' из '%s' — это ложное «в оригинале с маленькой».
    """
    rest = text[len(_lead(text)):]
    rest = PLACEHOLDER.sub(" ", rest)
    rest = re.sub(r"\{\d*\}", " ", rest)
    for ch in rest:
        if ch.isalpha():
            return ch
    return ""


def _strip_allowed(text: str) -> str:
    """Убирает из строки всё, что не является обычным текстом."""
    out = ALLOWED_TOKENS.sub(" ", text)
    out = SECTION_CODE.sub(" ", out)
    out = PLACEHOLDER.sub(" ", out)
    return out


def find_violations(en: str, ru: str, strict: bool = False) -> dict[str, list[str]]:
    """Все нарушения в одной паре строк. Пустой словарь — всё чисто.

    Категории разделены по последствиям:

    * `error` — ломает рендер в игре или подстановку (плейсхолдер, код, токен,
      пустая строка). Это блокер, его правят всегда.
    * `warn`  — не ломает, но видно игроку (регистр, отступ, точка во фрагменте,
      латиница в переводе). Правят по возможности.
    * `info`  — статистика, повод посмотреть глазами.

    Разделение важно: если валить всё в один список, на 34 тысячах строк
    получается 1700 «ошибок» из которых настоящих — единицы, и отчёт перестают
    читать.
    """
    v: dict[str, list[str]] = defaultdict(list)

    if not ru.strip():
        # Пустой перевод — ошибка только если в оригинале есть текст.
        # Моды намеренно присылают пустые значения: субтитры без подписи,
        # пустые строки в книгах, `description_5` у предметов без описания.
        if LAT_WORD.search(en):
            v["empty"].append("перевод пустой, а в оригинале текст")
        return dict(v)

    # ── 1. Плейсхолдеры: несовпадение = падение или «%s» на экране ──────────
    pe, pr = sorted(PLACEHOLDER.findall(en)), sorted(PLACEHOLDER.findall(ru))
    if pe != pr:
        # Сравниваем не текст, а номера аргументов: `%1$s`/`%2$s` — тот же
        # набор, что `%s`/`%s`, только с перестановкой слов под русский порядок.
        se_, _ = placeholder_slots(en)
        sr_, mixed = placeholder_slots(ru)
        if se_ != sr_ or mixed:
            v["placeholder"].append(f"{pe} → {pr}")
    for m in CYR_PLACEHOLDER.finditer(ru):
        v["placeholder_cyr"].append(f"{m.group(0)!r} набрано кириллицей")
    # Плейсхолдеры вида {0}/{name} — несовпадение ломает подстановку.
    # Но бывает и литерал вроде «{35} animals» (текст, не аргумент): тогда
    # расхождение означает, что в переводе поменяли число.
    be = sorted(re.findall(r"\{\d*\}", en))
    br = sorted(re.findall(r"\{\d*\}", ru))
    if be != br:
        kind = "brace_number" if all(re.fullmatch(r"\{\d+\}", x) for x in be + br) else "placeholder"
        if kind == "brace_number":
            v[kind].append(f"число в фигурных скобках: {be} → {br}")
        else:
            v[kind].append(f"фигурные скобки: {be} → {br}")

    # ── 2. §-коды: набор должен сохраниться, порядок может меняться ─────────
    se, sr = Counter(SECTION_CODE.findall(en)), Counter(SECTION_CODE.findall(ru))
    if se != sr:
        lost, added = se - sr, sr - se
        if lost:
            v["section_lost"].append(f"пропали: {dict(lost)}")
        if added:
            v["section_added"].append(f"лишние: {dict(added)}")

    # ── 3. Защищённые токены [[N]] ─────────────────────────────────────────
    # Токены — разделители слов защищённых фрагментов. В переводе их может
    # стать больше (русский текст другой длины) и это нормально. Ломается
    # только когда токен ПРОПАЛ: значит защищённый фрагмент потерян.
    #
    # Но есть второй случай: некоторые моды (Punchy) сами нарезают строку
    # на слова токенами [[N]] при рендере — тогда токен стоит почти после
    # каждого слова, и их количество зависит от длины перевода. Такие
    # токены не защищённые, сравнивать их нельзя.
    tok_en = re.findall(r"\[\[\d+\]\]", en)
    tok_ru = re.findall(r"\[\[\d+\]\]", ru)
    words_en = len(en.split())
    dense = bool(tok_en) and len(tok_en) >= max(3, words_en * 0.5)
    if tok_en and not dense:
        lost = sorted(set(tok_en) - set(tok_ru), key=lambda t: int(t[2:-2]))
        if lost:
            v["token"].append(f"пропали: {', '.join(lost)}")
    elif dense:
        v["token_dense"].append(f"токенов {len(tok_en)} → {len(tok_ru)} "
                                f"(разделители слов, не защищённые)")

    # ── 4. Отступ по краям: в тултипах он часть вёрстки ────────────────────
    lead_en = len(en) - len(en.lstrip())
    lead_ru = len(ru) - len(ru.lstrip())
    if lead_en != lead_ru:
        v["spaces"].append(f"отступ {lead_en} → {lead_ru}")

    # ── 5. Регистр первой буквы ────────────────────────────────────────────
    fe, fr = _first_letter(en), _first_letter(ru)
    if fe and fr:
        if fe.isupper() and fr.islower():
            v["case"].append(f"в оригинале {fe!r} с большой, в переводе {fr!r} с маленькой")
        elif fe.islower() and fr.isupper():
            v["case"].append(f"в оригинале {fe!r} с маленькой, в переводе {fr!r} с большой")

    # ── 6. Пунктуация в конце ──────────────────────────────────────────────
    # Потеря точки — не косметика: строка описания и строка-фрагмент тултипа
    # выглядят по-разному, и потеря читается как обрыв текста.
    # Хвостовые §-коды в сравнении не участвуют: `...damage.` и
    # `...урон§5` — это одна и та же точка, просто код закрывает её.
    def _tail(s: str) -> str:
        # токены [[N]] и §-коды могут стоять после знака — снимаем их,
        # иначе «ползёте.[[5]]» считается строкой без точки
        s = re.sub(r"(?:\[\[\d+\]\]|§[0-9a-fk-orA-FK-OR])+$", "", s)
        s = SECTION_CODE.sub("", s).rstrip()
        return s[-1:]

    tail_en, tail_ru = _tail(en), _tail(ru)
    # многоточие: «...» == «…», это один и тот же знак
    en_ell = en.rstrip().endswith(("...", "…"))
    ru_ell = ru.rstrip().endswith(("...", "…"))
    if en_ell and ru_ell:
        pass                                     # всё на месте
    elif en_ell or ru_ell:
        v["punct_mismatch"].append("многоточие есть только с одной стороны")
    elif tail_en in ".!?" and tail_ru not in ".!?":
        # «W.I.P.» — аббревиатура, точка в ней не завершает предложение
        abbrev = "." in en.strip()[:-1] and " " not in en.strip()
        if not abbrev:
            v["punct_lost"].append(f"потерян {tail_en!r} в конце")
    elif tail_en not in ".!?" and tail_ru in ".!?":
        # Точка в конце длинного предложения — нормальный русский.
        # Приделанная точка к короткому фрагменту тултипа — ошибка вёрстки.
        fragment = len(en.strip()) <= 40 or en.lstrip().startswith("§")
        v["punct_added" if fragment else "punct_added_long"].append(
            f"лишний {tail_ru!r} в конце")

    # ── 7. Запятая в десятичном числе ─────────────────────────────────────
    # Опасно только там, где значение проходит через String.format: «%s» →
    # «2,5» подставится, но «%.2f» с запятой уже нет, а любой разбор числа
    # через Double.parseDouble на запятой падает. В чисто текстовой строке
    # запятая — правильная русская типографика, и трогать её не нужно.
    if re.search(r"\d\.\d", en):
        has_args = bool(PLACEHOLDER.search(en) or PLACEHOLDER.search(ru))
        for m in re.finditer(r"\d,\d", ru):
            kind = "comma_number" if has_args else "comma_number_text"
            v[kind].append(f"{m.group(0)!r}" +
                           (" — нужна точка (строка идёт через String.format)"
                            if has_args else " — для текста допустимо"))

    # ── 8. Осталась ли английская проза ───────────────────────────────────
    if CYR.search(ru):
        left = _strip_allowed(ru)
        words = [w for w in LAT_WORD.findall(left) if w.lower() not in ALLOWED_LATIN]
        if len(words) >= 3:
            v["english_left"].append(" ".join(words[:8]))
    elif en.strip() and len(LAT_WORD.findall(en)) >= 2 and ru.strip() == en.strip():
        # перевод совпал с оригиналом — строку не переводили
        v["untranslated"].append("перевод совпадает с оригиналом")

    # ── 9. Двойной пробел внутри ──────────────────────────────────────────
    if re.search(r"[^\s]  +[^\s]", ru) and not re.search(r"[^\s]  +[^\s]", en):
        v["double_space"].append("двойной пробел внутри строки")

    # ── 10. Strict: непереведённая строка с текстом ───────────────────────
    if strict and not CYR.search(ru) and len(LAT_WORD.findall(en)) >= 2:
        v["untranslated_strict"].append("в переводе нет кириллицы")

    return dict(v)


# ─────────────────────────── источники строк ──────────────────────────────

def load_dict(path: Path) -> dict[str, str]:
    """Словарь переводов: {en: ru}."""
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise SystemExit(f"{path}: ожидался объект JSON")
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}


def load_lang_pair(en_path: Path, ru_path: Path) -> dict[str, str]:
    """Пара lang-файлов: ключ общий, значение берём из en_us и ru_ru."""
    en = load_dict(en_path)
    ru = load_dict(ru_path)
    pairs = {}
    for key, e in en.items():
        if key in ru:
            pairs[e] = ru[key]
    return pairs


def load_pack(path: Path, mods_dir: Path | None = None) -> dict[str, str]:
    """Пары строк из zip-ресурспака.

    В паке лежат только `ru_ru.json` — английские оригиналы надо брать
    из jar-ов модов (`--mods`). Проверяем то, что реально поедет в игру:
    наш пак заменяет lang-файл целиком, поэтому каждый `ru_ru` должен быть
    полным и чистым.
    """
    pairs: dict[str, str] = {}
    en_from_jars: dict[tuple[str, str], str] = {}

    if mods_dir and mods_dir.is_dir():
        for jar in sorted(mods_dir.glob("*.jar")):
            try:
                z = zipfile.ZipFile(jar)
            except Exception:
                continue
            with z:
                for name in z.namelist():
                    if not name.endswith("/lang/en_us.json"):
                        continue
                    ns = name.split("/")[1]
                    try:
                        data = json.loads(z.read(name).decode("utf-8-sig"))
                    except Exception:
                        continue
                    for key, val in data.items():
                        if isinstance(val, str):
                            en_from_jars[(ns, key)] = val

    with zipfile.ZipFile(path) as z:
        for name in sorted(z.namelist()):
            if not name.endswith("/lang/ru_ru.json"):
                continue
            ns = name.split("/")[1]
            try:
                ru = json.loads(z.read(name).decode("utf-8-sig"))
            except Exception:
                continue
            if not isinstance(ru, dict):
                continue
            # 1) оригинал из jar-а мода
            for key, val in ru.items():
                en = en_from_jars.get((ns, key))
                if isinstance(val, str) and isinstance(en, str):
                    pairs[en] = val
            # 2) ванильный minecraft — оригинала в jar-ах нет, пропускаем
    return pairs


# ─────────────────────────── отчёт ────────────────────────────────────────

TITLES = {
    "empty": "Пустой перевод",
    "placeholder": "Плейсхолдеры не совпадают",
    "placeholder_cyr": "Кириллица внутри плейсхолдера",
    "section_lost": "Потеряны §-коды",
    "section_added": "Лишние §-коды",
    "token": "Потеряны токены [[N]]",
    "token_dense": "Токены-разделители слов [[N]]",
    "spaces": "Пробелы по краям",
    "case": "Регистр первой буквы",
    "punct_lost": "Потеряна пунктуация в конце",
    "punct_mismatch": "Многоточие только с одной стороны",
    "brace_number": "Число в фигурных скобках изменено",
    "punct_added": "Лишняя точка в конце фрагмента",
    "punct_added_long": "Точка в конце (возможно, норма)",
    "comma_number": "Запятая в числе, идущем через String.format",
    "comma_number_text": "Запятая в числе (текст, допустимо)",
    "english_left": "Английская проза в переводе",
    "untranslated": "Строка не переведена",
    "untranslated_strict": "Строка не переведена (strict)",
    "double_space": "Двойной пробел",
}

SEVERITY = {
    # ломает игру или подстановку — править обязательно
    "placeholder": "error", "placeholder_cyr": "error", "token": "error",
    "empty": "error", "comma_number": "error",
    # видно игроку, но ничего не ломает — править по возможности
    "brace_number": "warn",
    "section_lost": "warn", "section_added": "warn", "spaces": "warn",
    "case": "warn", "punct_lost": "warn", "punct_added": "warn",
    "punct_mismatch": "warn", "english_left": "warn", "untranslated": "warn",
    "untranslated_strict": "warn",
    # статистика, повод посмотреть глазами
    "punct_added_long": "info", "comma_number_text": "info",
    "double_space": "info", "token_dense": "info",
}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", nargs="+",
                    help="словарь {en: ru}, пара lang-файлов (с --lang) или zip-пак")
    ap.add_argument("--lang", action="store_true",
                    help="первый аргумент — en_us.json, второй — ru_ru.json")
    ap.add_argument("--all", action="store_true",
                    help="показать также предупреждения и статистику "
                         "(по умолчанию только то, что ломает игру)")
    ap.add_argument("--strict", action="store_true", help="включить строгие проверки")
    ap.add_argument("--limit", type=int, default=8,
                    help="сколько примеров показывать на категорию (0 — все)")
    ap.add_argument("--mods", help="папка mods/ с jar-ами — источник en_us "
                                   "для проверки zip-пака")
    ap.add_argument("--json", help="сохранить отчёт в JSON")
    args = ap.parse_args()

    if args.lang:
        if len(args.source) != 2:
            raise SystemExit("--lang требует ровно два файла: en_us и ru_ru")
        pairs = load_lang_pair(Path(args.source[0]), Path(args.source[1]))
        label = f"{Path(args.source[0]).name} / {Path(args.source[1]).name}"
    else:
        src = Path(args.source[0])
        if src.suffix == ".zip":
            pairs = load_pack(src, Path(args.mods) if args.mods else None)
            if not pairs:
                raise SystemExit("в паке нет пар en/ru — укажи --mods <папка mods/>")
        else:
            pairs = load_dict(src)
        label = src.name

    if not pairs:
        print("не найдено ни одной пары строк")
        return 0

    buckets: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for en, ru in pairs.items():
        for kind, msgs in find_violations(en, ru, args.strict).items():
            for msg in msgs:
                buckets[kind].append((en, ru, msg))

    counts = Counter()
    for kind, rows in buckets.items():
        counts[SEVERITY[kind]] += len(rows)

    print(f"# Проверка перевода: {label}")
    print(f"строк: {len(pairs)} | блокеров: {counts['error']} | "
          f"предупреждений: {counts['warn']} | заметок: {counts['info']}")
    print()

    shown_kinds = [k for k in buckets
                   if args.all or SEVERITY[k] == "error"]
    shown_kinds.sort(key=lambda k: (-len(buckets[k]), k))

    if not shown_kinds:
        print("Блокеров нет. Запусти с --all, чтобы увидеть стилистику.")
        if args.json:
            Path(args.json).write_text(
                json.dumps({"source": label, "pairs": len(pairs), "violations": {}},
                           ensure_ascii=False, indent=1), encoding="utf-8")
        return 0

    for kind in shown_kinds:
        rows = buckets[kind]
        mark = {"error": "✗", "warn": "!", "info": "·"}[SEVERITY[kind]]
        print(f"{mark} {TITLES.get(kind, kind)} — {len(rows)}")
        shown = rows if args.limit == 0 else rows[:args.limit]
        for en, ru, msg in shown:
            print(f"    EN: {en[:110]}")
            print(f"    RU: {ru[:110]}")
            print(f"    →  {msg}")
        if len(rows) > len(shown):
            print(f"    … ещё {len(rows) - len(shown)}")
        print()

    if not args.all and (counts["warn"] or counts["info"]):
        print(f"Ещё {counts['warn']} предупреждений и {counts['info']} заметок "
              f"скрыто — смотри с --all.")

    if args.json:
        data = {k: [{"en": e, "ru": r, "msg": m} for e, r, m in v]
                for k, v in buckets.items()}
        Path(args.json).write_text(
            json.dumps({"source": label, "pairs": len(pairs),
                        "counts": dict(counts), "violations": data},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"отчёт: {args.json}")

    return 1 if counts["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
