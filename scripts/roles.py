"""Роль строки локализации — по ключу и по форме текста.

Зачем это отдельным модулем: от роли зависит и то, как строка переводится
(тултип — фрагмент, квест — литературный текст), и то, как её батчить.
Системный промт (§1) просит модель определять роль заново на каждой строке,
но роль и так выводится из ключа — незачем отдавать это модели.

Роли:
    name    — название предмета/блока/сущности/эффекта
    tooltip — фрагмент подсказки, часто с §-кодом в начале
    ui      — кнопка, заголовок экрана, элемент интерфейса
    config  — название или описание опции конфига
    quest   — title/subtitle/description квеста
    chat    — сообщение в чат / системное уведомление
    other   — всё остальное (литература из книг, лор)

Использование:

    from roles import role_of, group_by_role
    role_of("item.create.cogwheel", "Cogwheel")        # 'name'
    group_by_role([("item.x", "Cogwheel"), ...])       # {'name': [...], ...}
"""

from __future__ import annotations

import re

ROLES = ("name", "tooltip", "ui", "config", "quest", "chat", "other")

# ─────────────────────────── признаки по ключу ────────────────────────────

RE_QUEST = re.compile(r"(?:^|\.)(?:title|subtitle|description|quest_desc)$|^quests?[._]", re.I)
RE_CONFIG = re.compile(r"(?:\.tooltip|\.desc|\.comment|\.description)$|(?:^|\.)config[._]", re.I)
RE_NAME = re.compile(
    r"^(?:item|block|entity|effect|enchantment|potion|biome|fluid|mob_effect)\.",
    re.I)
RE_UI = re.compile(
    r"^(?:gui|screen|menu|button|container|jei|emi|rei|wthit|jade|tooltip\.ui)\."
    r"|(?:^|\.)(?:button|btn|tab|label|header)$", re.I)
RE_CHAT = re.compile(r"^(?:chat|message|msg|command|commands)\.", re.I)

# ─────────────────────────── признаки по тексту ───────────────────────────

RE_SECTION_START = re.compile(r"^\s*(?:§.|&.)")
RE_NUMBER_FRAG = re.compile(r"^\s*(?:§.|&.)*\s*[+\-±]?\d")


def role_of(key: str, text: str = "") -> str:
    """Определяет роль строки. Ключ важнее формы текста."""
    key = (key or "").strip()
    text = text or ""

    if key:
        is_named_thing = bool(RE_NAME.match(key))
        # item.foo.description — это подсказка предмета, а не описание квеста
        if RE_QUEST.search(key) and not is_named_thing:
            return "quest"
        if is_named_thing:
            if re.search(r"\.(?:desc|tooltip|description|info)(?:[._]|$)", key, re.I):
                return "tooltip"
            return "name"
        if RE_CONFIG.search(key):
            return "config"
        if RE_UI.match(key):
            return "ui"
        if RE_CHAT.match(key):
            return "chat"

    # по форме: строка начинается с §-кода и короткая — почти всегда тултип
    if RE_SECTION_START.match(text) or RE_NUMBER_FRAG.match(text):
        return "tooltip"
    if "\n" in text.strip() and len(text) > 120:
        return "other"
    return "other"


def group_by_role(pairs: list[tuple[str, str]]) -> dict[str, list[tuple[str, str]]]:
    """Раскладывает пары (ключ, текст) по ролям, сохраняя порядок внутри роли."""
    out: dict[str, list[tuple[str, str]]] = {r: [] for r in ROLES}
    for key, text in pairs:
        out[role_of(key, text)].append((key, text))
    return {r: v for r, v in out.items() if v}


def dedupe_by_text(pairs: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], dict[str, list[str]]]:
    """Убирает повторы одного и того же текста внутри пачки ДО отправки в движок.

    Одна и та же английская строка в двух местах сборки должна переводиться
    один раз и получить один перевод. Кэш это тоже ловит, но только между
    прогонами; внутри одного прогона повторы надо снимать здесь.

    Возвращает (уникальные пары, {текст: [все ключи с этим текстом]}).
    """
    seen: dict[str, tuple[str, str]] = {}
    keys_by_text: dict[str, list[str]] = {}
    for key, text in pairs:
        keys_by_text.setdefault(text, []).append(key)
        if text not in seen:
            seen[text] = (key, text)
    return list(seen.values()), keys_by_text
