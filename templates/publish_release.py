"""Шаблон: собрать релиз перевода в папку для публикации на GitHub.

Скопируй в свой репозиторий перевода, поправь FILES/DIRS/DICTS и запусти:

    python3 publish_release.py            # только собрать папку
    python3 publish_release.py --push     # собрать и залить в GitHub

Что делает:
  * кладёт готовый ресурспак, скрипт подсказок, датапак и квесты в `install/`
    (раскладка повторяет инстанс сборки — содержимое `install/` копируется
    в корень инстанса как есть);
  * кладёт итоговые словари в `data/`;
  * копирует скрипты конвейера в `tools/`;
  * **не копирует** ключи API, venv, бэкапы и кэши — проверяет перед выкладкой.

Секреты исключены жёстко: файлы с `key` в имени, `.env` и `.pem` не попадут
в репозиторий, а проверка упадёт с ошибкой, если такой файл всё же окажется
в сборке.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent          # папка скриптов
ROOT = HERE.parent                               # корень проекта
STAGE = Path("/путь/к/локальной/копии/репозитория")
REPO = "твой-логин/имя-репозитория"

# --- что копируем: (источник, путь внутри релиза) -----------------------
FILES = [
    # (ROOT / "resourcepacks/zz_linggango_ru.zip", "install/resourcepacks/zz_linggango_ru.zip"),
    # (ROOT / "kubejs/client_scripts/ru_tooltip_translator.js",
    #  "install/kubejs/client_scripts/ru_tooltip_translator.js"),
    # (ROOT / "mods/zz_linggango_ru_books.jar", "install/mods/zz_linggango_ru_books.jar"),
]

DIRS = [
    # (ROOT / "datapacks/zz_linggango_ru_books", "install/datapacks/zz_linggango_ru_books"),
    # (ROOT / "config/ftbquests/quests", "install/config/ftbquests/quests"),
]

# итоговые словари переводов
DICTS = [
    "mods_ru.json",
    "quests_ru.json",
    "patchouli_ru.json",
    "ui_strings_ru.json",
    "pack_script_ru.json",
]

# файлы, которые не копируем из папки со скриптами
TOOL_SKIP = {"generated_ru_tooltip_translator.js"}

SECRET_HINTS = ("key", ".env", ".pem", "secret", "token")


def copy(src: Path, rel: str, base: Path) -> int:
    dst = base / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        return sum(1 for _ in dst.rglob("*") if _.is_file())
    shutil.copy2(src, dst)
    return 1


def _build_into(stage: Path) -> None:
    stage.mkdir(parents=True, exist_ok=True)
    n = 0
    for src, rel in FILES:
        if not src.exists():
            print(f"  НЕТ {src}")
            continue
        n += copy(src, rel, stage)
        print(f"  {rel}")
    for src, rel in DIRS:
        if not src.exists():
            print(f"  НЕТ {src}")
            continue
        n += copy(src, rel, stage)
        print(f"  {rel}/ ({sum(1 for _ in (stage / rel).rglob('*') if _.is_file())} файлов)")
    for d in DICTS:
        p = HERE / "work" / d
        if p.exists():
            n += copy(p, f"data/{d}", stage)
            print(f"  data/{d}")

    tools_dir = stage / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    for p in sorted(HERE.glob("*.py")):
        if p.name.startswith("_"):
            continue
        shutil.copy2(p, tools_dir / p.name)
        n += 1
    for p in sorted(HERE.glob("*.js")):
        if p.name in TOOL_SKIP:
            continue
        shutil.copy2(p, tools_dir / p.name)
        n += 1
    print(f"  tools/ ({len(list(tools_dir.iterdir()))} скриптов)")
    print(f"\nвсего файлов: {n}")

    bad = [p for p in stage.rglob("*") if p.is_file() and (
        any(h in p.name.lower() for h in SECRET_HINTS) or p.suffix in (".env", ".pem"))]
    if bad:
        raise SystemExit(f"ВНИМАНИЕ: в релизе есть подозрительные файлы: {bad}")
    print("секретов в релизе нет")


def build() -> None:
    # собираем во временную папку, чтобы не снести .git в STAGE
    tmp = Path(tempfile.mkdtemp(prefix="release-"))
    try:
        _build_into(tmp)
        if STAGE.exists():
            for child in STAGE.iterdir():
                if child.name == ".git":
                    continue
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        else:
            STAGE.mkdir(parents=True)
        for child in tmp.iterdir():
            dst = STAGE / child.name
            shutil.copytree(child, dst) if child.is_dir() else shutil.copy2(child, dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def push() -> None:
    def run(*args: str) -> None:
        subprocess.run(args, cwd=STAGE, check=True)

    if not (STAGE / ".git").exists():
        run("git", "init", "-b", "main")
    run("git", "add", "-A")
    run("git", "commit", "-m", "Перевод сборки на русский")
    run("git", "push", "-u", "origin", "main")


if __name__ == "__main__":
    build()
    if "--push" in sys.argv:
        push()
