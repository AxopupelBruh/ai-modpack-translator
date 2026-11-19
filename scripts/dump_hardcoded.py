"""Разбор jar-а на текст, захардкоженный в коде.

Отвечает на вопрос «что в моде нельзя перевести ресурспаком». Читает строковые
литералы из constant pool всех `.class`, отсекает логи, id, пути и формат-строки,
группирует остаток по категориям и заодно проверяет, какие механизмы перевода
в моде уже есть.

    python3 dump_hardcoded.py mods/linggango_tweaks-7.47.jar
    python3 dump_hardcoded.py mods/*.jar -o report.md
    python3 dump_hardcoded.py mods/foo.jar --only-text

Почему не `javap`: он выдаёт пул вместе с дескрипторами и именами методов,
на большой сборке это десятки тысяч бесполезных строк. Прямой разбор constant
pool даёт только строковые литералы. Для подсчёта вызовов `javap` нужен —
счётчик по пулу занижен (пул хранит одну ссылку на метод, сколько бы раз его
ни вызывали), поэтому там, где `javap` есть, используется он.

Методика и шаблон письма автору — в `docs/HARDCODED.md`.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from collections import OrderedDict, defaultdict
from pathlib import Path

# --- SRG-имена методов 1.20.1 (Forge/Mojmap-remapped jar) ------------------
M_LITERAL = "m_237113_"                       # Component.literal(String)
M_TRANSLATABLE = ("m_237434_", "m_237115_")   # Component.translatable(...)
M_I18N_GET = "m_118938_"                      # I18n.get(...)
M_I18N_EXISTS = "m_118936_"                   # I18n.exists(...)

BUILDER = "ForgeConfigSpec$Builder"
BUILDER_DEFINE = ("define", "defineInRange", "defineList",
                  "defineListAllowEmpty", "defineEnum", "defineInList")

# --- фильтр: что НЕ является текстом интерфейса ----------------------------
NOISE = re.compile(
    r"\{|\}|%[sdf]|failed|Failed|Exception|could not|Could not|"
    r"not found|Ignoring|Skip |Unknown |Invalid |Unable |Cannot |"
    r"https?://|\.png|\.json|\.toml|\.nbt|§[0-9a-fk-or]"
)
CLASS_PREFIX = ("java/", "net/", "com/", "org/", "javax/", "io/", "Lnet", "Lcom")
IDENT_ONLY = re.compile(r"^[\w_./:()\[\]$<>;-]+$")
SKIP_EXACT = {"both", "none", "true", "false", "back", "next", "ok", "cancel",
              "on", "off", "yes", "no", "null", "lp", "mana", "aura", "defs"}


# ─────────────────────────── constant pool ─────────────────────────────────

def utf8_constants(data: bytes) -> list[str]:
    """Все строковые литералы (тег 1) из constant pool `.class`."""
    if data[:4] != b"\xca\xfe\xba\xbe":
        raise ValueError("не .class файл")
    count = struct.unpack(">H", data[8:10])[0]
    i, out, k = 10, [], 1
    while k < count:
        tag = data[i]
        i += 1
        if tag == 1:                                     # Utf8
            ln = struct.unpack(">H", data[i:i + 2])[0]
            i += 2
            out.append(data[i:i + ln].decode("utf-8", "replace"))
            i += ln
        elif tag in (7, 8, 16, 19, 20):                   # Class, String, MethodType…
            i += 2
        elif tag == 15:                                   # MethodHandle
            i += 3
        elif tag in (3, 4, 9, 10, 11, 12, 17, 18):
            i += 4
        elif tag in (5, 6):                               # Long, Double — два слота
            i += 8
            k += 1
        else:
            raise ValueError(f"неизвестный тег constant pool: {tag}")
        k += 1
    return out


def is_ui_string(s: str) -> bool:
    """Строка похожа на текст для игрока, а не на лог/id/путь."""
    if len(s) < 4:
        return False
    # хотя бы два слова подряд — отсекает одиночные id и аббревиатуры
    if not re.search(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}", s):
        return False
    if s.startswith(CLASS_PREFIX) or IDENT_ONLY.match(s):
        return False
    if s.strip().lower() in SKIP_EXACT:
        return False
    return not NOISE.search(s)


# ─────────────────────────── категории ─────────────────────────────────────

def category(class_name: str) -> str:
    """Категория по имени класса — грубо, но стабильно и читаемо в отчёте."""
    n = class_name.replace("\\", "/")
    base = n.rsplit("/", 1)[-1]
    if "/ring/" in n:
        return "Экран выбора / первый экран игрока"
    if "/command/" in n or base.endswith("Commands.class"):
        return "Сообщения команд"
    if base.endswith(("Config.class", "Comments.class", "Options.class")):
        return "Конфиги: опции и описания"
    if ("/client/screen/" in n or "/client/config/" in n
            or base.endswith(("Screen.class", "Button.class"))):
        return "Экраны и собственные меню"
    if "Tooltip" in base or "tooltip" in base:
        return "Тултипы предметов"
    if "/mixin/" in n:
        return "Миксины: правки меню и чужих модов"
    return "Прочее: чат, тосты, прогресс"


# ─────────────────────────── вызовы Builder ────────────────────────────────

def builder_calls_via_javap(class_bytes: bytes) -> dict[str, int] | None:
    """Точный подсчёт вызовов ForgeConfigSpec.Builder через `javap -c`.

    Возвращает None, если javap недоступен — тогда считается по пулу (заниженно).
    """
    javap = shutil.which("javap")
    if not javap:
        return None
    with tempfile.NamedTemporaryFile(suffix=".class", delete=False) as tmp:
        tmp.write(class_bytes)
        path = tmp.name
    try:
        out = subprocess.run([javap, "-p", "-c", path],
                             capture_output=True, text=True, timeout=120).stdout
    except Exception:
        return None
    finally:
        Path(path).unlink(missing_ok=True)
    counts = defaultdict(int)
    for name in BUILDER_DEFINE:
        counts["values"] += len(re.findall(rf"Builder\.{name}\b", out))
    counts["comments"] += len(re.findall(rf"Builder\.comment\b", out))
    counts["translations"] += len(re.findall(rf"Builder\.translation\b", out))
    return dict(counts)


def builder_calls_via_pool(consts: set[str]) -> dict[str, int]:
    """Грубая оценка по пулу: сколько РАЗНЫХ методов Builder упомянуто.

    Для конфигов занижает — пул не различает один вызов и десять. Нужна
    как запасной вариант, когда javap недоступен.
    """
    counts = defaultdict(int)
    if not any("translation" in c for c in consts):
        counts["translations"] = 0
    for name in BUILDER_DEFINE:
        if name in consts:
            counts["values"] += 1
    if "comment" in consts:
        counts["comments"] = 1
    return dict(counts)


# ─────────────────────────── разбор одного jar ─────────────────────────────

def analyze_jar(jar: Path) -> dict:
    result = {
        "jar": jar.name,
        "classes": 0,
        "strings": 0,
        "translations": OrderedDict(),
        "literal_classes": [],
        "translatable_classes": [],
        "i18n_used": False,
        "configs": {"values": 0, "comments": 0, "translations": 0,
                    "files": [], "exact": True},
    }
    cat_strings: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    with zipfile.ZipFile(jar) as z:
        for name in z.namelist():
            if not name.endswith(".class"):
                continue
            try:
                raw = z.read(name)
                consts_list = utf8_constants(raw)
            except Exception:
                continue
            cs = set(consts_list)
            result["classes"] += 1

            if M_LITERAL in cs:
                result["literal_classes"].append(name)
            if any(m in cs for m in M_TRANSLATABLE):
                result["translatable_classes"].append(name)
            if M_I18N_GET in cs or M_I18N_EXISTS in cs:
                result["i18n_used"] = True

            # конфиги: считаем вызовы Builder, чтобы понять, включён ли перевод
            if BUILDER in cs or name.endswith(("Config.class", "Comments.class")):
                calls = builder_calls_via_javap(raw)
                if calls is None:
                    calls = builder_calls_via_pool(cs)
                    result["configs"]["exact"] = False
                if calls and any(calls.values()):
                    for key in ("values", "comments", "translations"):
                        result["configs"][key] += calls.get(key, 0)
                    result["configs"]["files"].append(name)

            cat = category(name)
            short = name.rsplit("/", 1)[-1].replace(".class", "")
            for s in cs:
                if is_ui_string(s):
                    cat_strings[cat][short].add(s)

    total = 0
    for cat in sorted(cat_strings,
                      key=lambda c: -sum(len(v) for v in cat_strings[c].values())):
        classes = cat_strings[cat]
        total += sum(len(v) for v in classes.values())
        result["translations"][cat] = OrderedDict(
            (cls, sorted(vals)) for cls, vals in
            sorted(classes.items(), key=lambda kv: -len(kv[1]))
        )
    result["strings"] = total
    return result


# ─────────────────────────── отчёт ─────────────────────────────────────────

def render(result: dict, only_text: bool) -> str:
    out = [f"# Захардкоженный текст: `{result['jar']}`", ""]
    out.append(f"Классов: **{result['classes']}**, "
               f"строк интерфейса: **{result['strings']}**.")
    out.append("")

    if not only_text:
        lit = len(result["literal_classes"])
        tr = len(result["translatable_classes"])
        c = result["configs"]
        out += ["## Есть ли механизмы перевода", ""]
        out.append(f"- классов с `Component.literal`: **{lit}**")
        out.append(f"- классов с `Component.translatable`: **{tr}**")
        out.append(f"- `I18n` используется: **{'да' if result['i18n_used'] else 'нет'}**")
        if c["files"]:
            note = "" if c["exact"] else " (оценка: `javap` недоступен)"
            if c["translations"]:
                out.append(f"- конфиги: **{c['translations']} вызовов "
                           f"`Builder.translation`** на {c['values']} определений "
                           f"и {c['comments']} комментариев{note}")
            else:
                out.append(f"- конфиги: **0 вызовов `Builder.translation`** "
                           f"на {c['values']} определений и {c['comments']} "
                           f"комментариев — перевод не включён{note}")
        out.append("")

        out.append("**Что это значит.**")
        if tr and not lit:
            out.append("Мод уже переводим — `literal` не используется вообще.")
        elif tr:
            out.append("Автор знает механизм `translatable`, просто применил не везде. "
                       "Просьба сводится к «примени то же самое здесь».")
        else:
            out.append("Мод собирает весь текст через `literal`. Механизма перевода нет, "
                       "но `I18n` в коде "
                       f"{'используется' if result['i18n_used'] else 'не используется'} — "
                       "значит переделка механическая.")
        if c["files"] and not c["translations"]:
            out.append("")
            out.append("Самое дешёвое для автора: `Builder.translation(...)` перед каждым "
                       "`Builder.comment(...)` — экран конфигов менять не нужно, "
                       "Forge читает ключи сам.")
        out.append("")

    for cat, classes in result["translations"].items():
        n = sum(len(v) for v in classes.values())
        out += [f"## {cat} — {n} строк", ""]
        for cls, vals in classes.items():
            out += [f"**{cls}** ({len(vals)})", ""]
            out += [f"- `{s}`" for s in vals]
            out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jars", nargs="+", help="jar-файлы модов")
    ap.add_argument("-o", "--out", help="куда писать отчёт (по умолчанию stdout)")
    ap.add_argument("--json", help="дополнительно сохранить разбор в JSON")
    ap.add_argument("--only-text", action="store_true",
                    help="только строки, без анализа механизмов перевода")
    args = ap.parse_args()

    parts = []
    for path in args.jars:
        jar = Path(path)
        if not jar.exists():
            print(f"нет файла: {jar}", file=sys.stderr)
            continue
        print(f"разбираю {jar.name}…", file=sys.stderr)
        result = analyze_jar(jar)
        parts.append(render(result, args.only_text))
        if args.json:
            Path(args.json).write_text(
                json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    text = "\n\n---\n\n".join(parts)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"отчёт: {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
