"""Разведка сборки одной командой.

Отвечает на вопрос «сколько работы и что сломается» до начала перевода.
Собирает версии, объём перевода по слоям, что уже закрыто чужими паками,
шифрование квестов, книги Patchouli, датапаки, порядок паков — и сразу
подсвечивает риски.

    python3 audit_pack.py ~/Documents/curseforge/minecraft/Instances/Linggango
    python3 audit_pack.py <инстанс> --json work/recon.json
    python3 audit_pack.py <инстанс> --no-ocr        # без tesseract, быстрее

Ничего не меняет: только читает файлы. Запускать до любых правок.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

CYR = re.compile(r"[а-яёА-ЯЁ]")
LAT_WORD = re.compile(r"[A-Za-z]{2,}")

# префиксы шифрования квестов, которые встречались в сборках
QUEST_MAGIC = ("LGQ1:", "LGQ2:", "FTBQ:", "FTBQ2:", "ENCRYPTED:", "SNAIL:")


def read_json_maybe(text: str):
    """lang-файл может быть с BOM, комментариями и висячими запятыми."""
    text = text.lstrip("\ufeff")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    stripped = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.S)
    stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def instance_info(root: Path) -> dict:
    info: dict = {"path": str(root)}

    # mmc-pack.json — Prism/MultiMC
    mmc = root / "mmc-pack.json"
    if mmc.exists():
        try:
            data = json.loads(mmc.read_text(encoding="utf-8"))
            for comp in data.get("components", []):
                uid, ver = comp.get("uid", ""), comp.get("version", "")
                if uid == "net.minecraft":
                    info["mc"] = ver
                elif uid in ("net.minecraftforge", "net.neoforged",
                             "org.quiltmc.quilt-loader", "net.fabricmc.fabric-loader"):
                    info["loader"] = uid.rsplit(".", 1)[-1]
                    info["loader_version"] = ver
        except Exception:
            pass

    # instance.cfg — имя сборки
    cfg = root / "instance.cfg"
    if cfg.exists():
        m = re.search(r"^name=(.+)$", cfg.read_text(encoding="utf-8", errors="replace"), re.M)
        if m:
            info["name"] = m.group(1).strip()

    # manifest.json — CurseForge: единственный надёжный источник версий
    mf = root / "manifest.json"
    if mf.exists():
        try:
            data = json.loads(mf.read_text(encoding="utf-8"))
            info.setdefault("name", data.get("name", ""))
            info["cf_version"] = data.get("version", "")
            mi = data.get("minecraft", {}) or {}
            if mi.get("version"):
                info["mc"] = mi["version"]
            for ldr in mi.get("modLoaders", []) or []:
                if ldr.get("primary", True):
                    lid = str(ldr.get("id", ""))
                    # "forge-47.4.20" → загрузчик forge, версия 47.4.20
                    if "-" in lid:
                        name, _, ver = lid.partition("-")
                        info["loader"] = name
                        info["loader_version"] = ver
                    else:
                        info["loader"] = lid
                    break
        except Exception:
            pass

    info.setdefault("name", root.name)
    info.setdefault("mc", "не найдено")
    info.setdefault("loader", "не найдено")
    return info


def mods_info(mods: Path) -> dict:
    """Сколько модов, неймспейсов, ключей и что уже переведено внутри модов."""
    jars = sorted(mods.glob("*.jar")) if mods.is_dir() else []
    ns_en: dict[str, int] = {}
    ns_ru: dict[str, int] = {}
    ns_ru_empty: dict[str, int] = {}
    dup_ru: list[str] = []
    zwnj: list[str] = []
    patchouli: dict[str, dict[str, int]] = defaultdict(lambda: {"pages": 0, "ru": 0})
    loot_text = 0

    for jar in jars:
        try:
            z = zipfile.ZipFile(jar)
        except Exception:
            continue
        with z:
            for name in z.namelist():
                if name.endswith("/lang/en_us.json"):
                    ns = name.split("/")[1]
                    data = read_json_maybe(z.read(name).decode("utf-8-sig", "replace"))
                    if isinstance(data, dict):
                        ns_en[ns] = ns_en.get(ns, 0) + len(data)
                        if any("\u200c" in str(v) for v in data.values()):
                            zwnj.append(f"{jar.name}:{ns}")
                elif name.endswith("/lang/ru_ru.json"):
                    ns = name.split("/")[1]
                    data = read_json_maybe(z.read(name).decode("utf-8-sig", "replace"))
                    if isinstance(data, dict):
                        ns_ru[ns] = ns_ru.get(ns, 0) + len(data)
                        empty = sum(1 for v in data.values()
                                    if isinstance(v, str) and not v.strip())
                        ns_ru_empty[ns] = ns_ru_empty.get(ns, 0) + empty
                        if not any(CYR.search(str(v)) for v in data.values()):
                            dup_ru.append(f"{jar.name}:{ns}")
                elif "/patchouli_books/" in name and name.endswith(".json"):
                    # assets/<ns>/patchouli_books/<book>/<lang>/<...>.json
                    parts = name.split("/")
                    if len(parts) >= 6:
                        book = parts[3]
                        lang = parts[4]
                        if lang in ("en_us", "ru_ru"):
                            patchouli[book]["pages"] += 1
                            if lang == "ru_ru":
                                patchouli[book]["ru"] += 1
                elif "/loot_table" in name.lower() or "/loot_tables/" in name.lower():
                    if name.endswith(".json"):
                        # Текст в лут-таблице — это поля name/lore с непустой
                        # строкой. Просто `"name"` встречается в каждой таблице
                        # (это id предмета), поэтому ищем именно текстовое поле.
                        blob = z.read(name)
                        if (re.search(rb'"name"\s*:\s*\{\s*"text"', blob)
                                or re.search(rb'"lore"\s*:\s*\[', blob)):
                            loot_text += 1

    return {
        "jars": len(jars),
        "namespaces": len(ns_en),
        "keys_en": sum(ns_en.values()),
        "namespaces_with_own_ru": len(ns_ru),
        "keys_own_ru": sum(ns_ru.values()),
        "own_ru_empty": sum(ns_ru_empty.values()),
        # «перевод» без единой русской буквы — мод приложил английский файл
        "own_ru_no_cyrillic": dup_ru,
        "zwnj": zwnj,
        "patchouli_books": dict(patchouli),
        "loot_with_text": loot_text,
    }


def quests_info(root: Path) -> dict:
    """Главы FTB Quests: сколько, зашифрованы ли, сколько квестов.

    Шифрованный файл — одна строка `LGQ1:<base64>`; заголовок и ключ искать
    в нём бессмысленно, поэтому для зашифрованных считаем только файлы.
    """
    qdir = root / "config" / "ftbquests" / "quests"
    out: dict = {"found": qdir.is_dir(), "chapters": 0, "quests": 0,
                 "encrypted": [], "total": 0, "chapters_encrypted": 0}
    if not out["found"]:
        return out
    files = sorted(p for p in qdir.rglob("*") if p.suffix in (".snbt", ".nbt"))
    out["total"] = len(files)
    for p in files:
        try:
            raw = p.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
        except Exception:
            continue
        head = raw[:16]
        magic = next((m for m in QUEST_MAGIC if head.startswith(m)), None)
        if magic:
            out["encrypted"].append(p.name)
            out.setdefault("magic", magic)
            continue
        out["chapters"] += 1
        out["quests"] += len(re.findall(r"\bid:\s*\"", raw))
    out["chapters_encrypted"] = len(out["encrypted"])
    return out


def _has_vanilla_ru(pack: Path) -> bool:
    """Есть ли в паке assets/minecraft/lang/ru_ru.json — без него ванильный
    интерфейс останется английским, даже если все моды переведены."""
    try:
        with zipfile.ZipFile(pack) as z:
            return "assets/minecraft/lang/ru_ru.json" in z.namelist()
    except Exception:
        return False


def packs_info(root: Path) -> dict:
    rp = root / "resourcepacks"
    packs = sorted(p.name for p in rp.glob("*")) if rp.is_dir() else []
    enabled: list[str] = []
    opts = root / "options.txt"
    if opts.exists():
        m = re.search(r"^resourcePacks:(.*)$",
                      opts.read_text(encoding="utf-8", errors="replace"), re.M)
        if m:
            enabled = re.findall(r'"file/([^"]+)"', m.group(1))
    # свой конфиг порядка паков
    order_cfg = {}
    for cand in (root / "config" / "linggango-resourcepacks.toml",
                 root / "config" / "resourcepackoverrides.toml"):
        if cand.exists():
            order_cfg[cand.name] = cand.read_text(encoding="utf-8", errors="replace")[:400]
    rp_dir = root / "resourcepacks"
    vanilla_ru = [n for n in enabled
                  if (rp_dir / n).exists() and _has_vanilla_ru(rp_dir / n)]
    return {
        "available": packs,
        "enabled": enabled,
        "last": enabled[-1] if enabled else None,
        "vanilla_ru": vanilla_ru,
        "configs": order_cfg,
    }


def risky_own_ru(root: Path) -> list[str]:
    """Свои ru_ru модов, которые могли съехать по ключам.

    Признак: в jar есть ru_ru, но он заметно короче en_us — значит переведена
    часть, а остальное осталось английским либо от прошлой версии.
    """
    flags = []
    mods = root / "mods"
    if not mods.is_dir():
        return flags
    for jar in sorted(mods.glob("*.jar")):
        try:
            z = zipfile.ZipFile(jar)
        except Exception:
            continue
        with z:
            names = z.namelist()
            for n in names:
                if not n.endswith("/lang/en_us.json"):
                    continue
                ru = n[:-len("en_us.json")] + "ru_ru.json"
                if ru not in names:
                    continue
                try:
                    en = read_json_maybe(z.read(n).decode("utf-8-sig", "replace")) or {}
                    r = read_json_maybe(z.read(ru).decode("utf-8-sig", "replace")) or {}
                except Exception:
                    continue
                if not isinstance(en, dict) or not isinstance(r, dict) or not en:
                    continue
                ratio = len(r) / len(en)
                if 0.05 < ratio < 0.9:
                    flags.append(f"{jar.name}: {len(r)}/{len(en)} ключей ({ratio:.0%})")
    return flags


def render(info: dict, mods: dict, quests: dict, packs: dict,
           risky: list[str], risks: list[str]) -> str:
    o = []
    o.append(f"# Разведка: {info.get('name')}")
    o.append("")
    o.append(f"- Minecraft: **{info.get('mc')}**")
    o.append(f"- Загрузчик: **{info.get('loader')}** "
             f"{info.get('loader_version', '')}".rstrip())
    if info.get("cf_version"):
        o.append(f"- Версия сборки: {info['cf_version']}")
    o.append(f"- Модов: **{mods['jars']}**, неймспейсов с lang: **{mods['namespaces']}**")
    o.append("")

    o.append("## Объём перевода")
    o.append("")
    o.append("| Слой | Объём | Комментарий |")
    o.append("|---|---|---|")
    o.append(f"| Lang-ключи модов | {mods['keys_en']} | "
             f"своих `ru_ru` у модов: {mods['namespaces_with_own_ru']} "
             f"неймспейсов ({mods['keys_own_ru']} ключей) |")
    if quests["found"]:
        enc = quests["chapters_encrypted"]
        qtxt = (f"**зашифрованы** ({enc} файлов)" if enc
                else f"{quests['chapters']} глав, ~{quests['quests']} квестов")
        o.append(f"| Квесты FTB | {quests['total']} файлов | {qtxt} |")
    books = mods["patchouli_books"]
    if books:
        total_pages = sum(v["pages"] for v in books.values())
        total_ru = sum(v["ru"] for v in books.values())
        o.append(f"| Книги Patchouli | {len(books)} книг, {total_pages} страниц | "
                 f"уже с `ru_ru`: {total_ru} |")
    if mods["loot_with_text"]:
        o.append(f"| Лут-таблицы с текстом | {mods['loot_with_text']} | "
                 f"датапак перекрывает файл целиком |")
    o.append("")

    o.append("## Уже переведено")
    o.append("")
    if packs["enabled"]:
        o.append(f"Включено паков: {len(packs['enabled'])}. Последний — "
                 f"`{packs['last']}`.")
    else:
        o.append("Паки не включены (или `options.txt` не найден).")
    o.append("")

    if risks:
        o.append("## Риски")
        o.append("")
        for r in risks:
            o.append(f"- {r}")
        o.append("")

    if risky:
        o.append("## Свои `ru_ru`, которые могут съехать")
        o.append("")
        for r in risky[:15]:
            o.append(f"- {r}")
        if len(risky) > 15:
            o.append(f"- … ещё {len(risky) - 15}")
        o.append("")

    return "\n".join(o)


def collect_risks(info: dict, mods: dict, quests: dict, packs: dict,
                  root: Path) -> list[str]:
    r: list[str] = []
    if quests["found"] and quests["encrypted"]:
        r.append(f"**Квесты зашифрованы** ({len(quests['encrypted'])} файлов, префикс "
                 f"`{quests.get('magic', '?')}`). Без декриптора перевести нельзя — "
                 f"искать ключ в моде-шифровщике.")
    if packs["enabled"] and packs["last"] and "ru" not in (packs["last"] or "").lower():
        r.append(f"**Наш пак не последний** (последний — `{packs['last']}`). "
                 f"Перевод не будет виден в игре.")
    if not packs["enabled"]:
        r.append("**`options.txt` без паков** — либо инстанс не запускался, "
                 "либо порядок задаётся в `config/`.")
    if not packs.get("vanilla_ru"):
        r.append("**Ванильного `ru_ru` нет ни в одном включённом паке.** Весь "
                 "интерфейс игры останется английским, даже если все моды "
                 "переведены.")
    if mods["own_ru_no_cyrillic"]:
        r.append(f"**{len(mods['own_ru_no_cyrillic'])} модов отдают `ru_ru` без единой "
                 f"русской буквы** (приложили английский файл). Нужен откат на наш перевод.")
    if mods["own_ru_empty"]:
        r.append(f"**{mods['own_ru_empty']} пустых значений** в чужих `ru_ru` — "
                 f"в игре пустые строки. Перекрывать нашими.")
    if mods["zwnj"]:
        r.append(f"**ZWNJ в {len(mods['zwnj'])} неймспейсах.** Нормализатор пробелов "
                 f"может растащить маркер — проверять после каждой сборки.")
    if quests["found"] and not quests["encrypted"] and quests["total"]:
        r.append(f"Квесты открытым текстом ({quests['total']} файлов) — "
                 f"перед правкой сделать полный бэкап каталога.")
    return r


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("instance", help="путь к инстансу сборки")
    ap.add_argument("-o", "--out", help="куда писать отчёт (по умолчанию stdout)")
    ap.add_argument("--json", help="сохранить собранные данные в JSON")
    args = ap.parse_args()

    root = Path(args.instance).expanduser()
    if not root.is_dir():
        raise SystemExit(f"нет такой папки: {root}")

    info = instance_info(root)
    mods = mods_info(root / "mods")
    quests = quests_info(root)
    packs = packs_info(root)
    risky = risky_own_ru(root)
    risks = collect_risks(info, mods, quests, packs, root)

    text = render(info, mods, quests, packs, risky, risks)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"отчёт: {args.out}", file=sys.stderr)
    else:
        print(text)

    if args.json:
        payload = {"instance": info, "mods": mods, "quests": quests,
                   "packs": packs, "risky_own_ru": risky, "risks": risks}
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"данные: {args.json}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
