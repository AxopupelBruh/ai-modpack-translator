# Текст, захардкоженный в моде

Что делать, когда lang-файлы закрыты на 100 %, а в игре всё равно английский
текст: экраны, тултипы, конфиги.

Коротко: **ресурспаком это не достаётся**. Нужен либо скрипт-перехватчик
(работает не для всего), либо участие автора мода. Этот документ — про то,
как понять масштаб и как составить предметную просьбу автору.

---

## 1. Сначала убедись, что дело именно в этом

Три источника «английского при полном переводе», и путать их нельзя:

| Что видно | Настоящая причина | Куда смотреть |
|---|---|---|
| Текст есть в `en_us.json`, в паке переведён, в игре английский | пак не последний, или lang-файл неполный | `TRAPS.md`, п. 1–2 |
| Текста нет в `en_us.json` вообще | захардкожено в коде **или** приходит из конфига/датапака | этот документ |
| Текст есть, но показывается старое значение | чужой `ru_ru` съехал по ключам | `TRAPS.md`, п. 21 |

Быстрая проверка: возьми строку и поищи её в `en_us.json` всех jar-ов.

```bash
python3 - <<'EOF'
import json, zipfile
from pathlib import Path
needle = "Collect"          # кусок строки, которую видно в игре
for jar in Path("mods").glob("*.jar"):
    with zipfile.ZipFile(jar) as z:
        for n in z.namelist():
            if n.endswith("/lang/en_us.json"):
                data = json.loads(z.read(n).decode("utf-8-sig"))
                for k, v in data.items():
                    if needle.lower() in str(v).lower():
                        print(jar.name, k, "=", v)
EOF
```

Пусто → строка захардкожена. Есть → проблема в сборке пака, не в моде.

---

## 2. Как вытащить строки из `.class`

`javap` показывает пул констант, но с шумом: имена методов, дескрипторы, пути.
Нужен фильтр. Читать constant pool напрямую чище — вот самодостаточный парсер.

```python
import struct, zipfile

def utf8_constants(data: bytes) -> list[str]:
    """Все строки из constant pool класса (тег 1)."""
    assert data[:4] == b"\xca\xfe\xba\xbe"
    count = struct.unpack(">H", data[8:10])[0]
    i, out, k = 10, [], 1
    while k < count:
        tag = data[i]; i += 1
        if tag == 1:                                    # Utf8
            ln = struct.unpack(">H", data[i:i+2])[0]; i += 2
            out.append(data[i:i+ln].decode("utf-8", "replace")); i += ln
        elif tag in (7, 8, 16, 19, 20): i += 2          # Class, String, MethodType...
        elif tag == 15: i += 3                          # MethodHandle
        elif tag in (3, 4, 9, 10, 11, 12, 17, 18): i += 4
        elif tag in (5, 6): i += 8; k += 1              # Long, Double занимают 2 слота
        else: raise ValueError(f"неизвестный тег {tag}")
        k += 1
    return out
```

### Что отсекать

Без фильтра на 430 модов выйдет больше 100 тысяч «строк», из которых текста —
проценты. Рабочий фильтр:

```python
import re

LOG = re.compile(r"\{|\}|%[sdf]|failed|Failed|Exception|could not|Could not|"
                 r"not found|Ignoring|Skip |Unknown |Invalid |Unable |Cannot |"
                 r"https?://|\.png|\.json|\.toml|§[0-9a-fk-or]")
SKIP_PREFIX = ("java/", "net/", "com/", "org/", "Lnet", "Lcom")

def is_ui_string(s: str) -> bool:
    if len(s) < 4 or not re.search(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}", s):
        return False                      # меньше двух слов — не фраза
    if s.startswith(SKIP_PREFIX) or re.match(r"^[\w_./:()\[]+$", s):
        return False                      # дескрипторы и id
    return not LOG.search(s)              # логи, формат-строки, пути
```

Готовый скрипт с группировкой по категориям — `scripts/dump_hardcoded.py`.

---

## 3. Что искать в коде помимо строк

Масштаб проблемы определяется не количеством строк, а тем, **какие механизмы
перевода уже есть**. Проверь три вещи.

### 3.1. Есть ли `Component.translatable`

```python
s = utf8_constants(open(cls, "rb").read())
has_translatable = "m_237434_" in s or "m_237115_" in s   # SRG-имена 1.20.1
has_literal      = "m_237113_" in s
```

Соотношение `literal` / `translatable` — главный показатель. Если `translatable`
уже используется хотя бы где-то, автор знает механизм, и просьба сводится
к «примени то же самое здесь».

### 3.2. Есть ли `Builder.translation(...)` у конфигов

Forge сам умеет переводить конфиги — но только если мод вызывает
`Builder.translation(key)` перед `Builder.comment(...)`. Проверка:

```bash
javap -p -c MyConfig.class | grep -cE 'Builder\.translation'
javap -p -c MyConfig.class | grep -cE 'Builder\.(define|defineInRange|defineList|defineEnum)'
```

`0` вызовов `translation` при сотнях `define` — значит переводить нечего
не потому, что нельзя, а потому что не включили. Это самая дешёвая для автора
правка: одна строка на опцию, экран менять не надо.

⚠️ Но проверь, читает ли **экран** эти ключи. Стандартный Forge-экран конфигов
делает это сам. Свой экран — только если написан через `I18n`:

```
ValueSpec.getTranslationKey() → I18n.exists(key) → I18n.get(key)
```

Если своего экрана нет, а конфиг открывается через `ConfigScreenHandler` —
`translation` достаточно.

### 3.3. Участвует ли `I18n` в тултипах конфигов

Форжовый `ValueSpec.getComment()` возвращает **сырой комментарий из кода**.
Даже при заполненном `translation` описание опции останется английским, если
экран печатает `getComment()` напрямую. Обход, который стоит предложить:
откат на `I18n.get(key + ".tooltip")`, если ключ существует.

---

## 4. Как оценить объём для письма автору

Не «много строк» и не «весь мод». Нужны три числа и одна таблица.

**Числа:**
1. всего строк интерфейса (после фильтра из §2);
2. сколько из них достижимо **без** правок экранов (конфиги через `translation`,
   тултипы через `translatable`);
3. сколько требует переделки структуры (конструкторы, принимающие `String`).

**Таблица по категориям** — она же становится вложением:

| Категория | Строк | Что нужно от автора |
|---|---|---|
| Конфиги: опции и описания | 986 | `Builder.translation(...)` + откат тултипа на `I18n` |
| Тултипы предметов | 127 | `literal` → `translatable` |
| Экраны настроек | 185 | вынести строки в ключи |
| Экран выбора (первый у игрока) | 81 | структура: конструктор принимает ключи |
| Чат, тосты, загрузка | 343 | `literal` → `translatable` |
| Команды | 32 | то же |

Порядок в таблице — по соотношению «объём / трудозатраты автора». Конфиги идут
первыми не потому, что их больше, а потому что это **одна строка на опцию**
против переписывания экрана.

---

## 5. Что делать, если автор не ответит

Реальные варианты, по убыванию надёжности:

1. **Скрипт-перехватчик тултипов** (KubeJS → Architectury → Forge
   `ItemTooltipEvent`). Закрывает тултипы предметов, которые формируются
   в коде, — часто это самая заметная часть. Не закрывает экраны и конфиги.
   Сборка — `scripts/build_tooltip_script.py`.
2. **Пак-локализация GUI**: ресурспак с JSON-моделями интерфейсов. Работает,
   только если мод рисует экран через данные, а не через код. Редко.
3. **Ждать.** Экран на английском — неприятно, но не ломает игру. Патч jar
   ломает: обновление сборки его сотрёт, а расхождение версий может уронить мод.

Чего делать **нельзя**: править `.class` в jar и класть его обратно. Даже если
сегодня работает — при следующем обновлении сборки файл перезапишется,
а сохранённые настройки могут не совпасть с новой структурой.

---

## 6. Шаблон письма автору

Письмо работает, когда в нём есть: факт (сколько и где), доказательство
(что механизм уже есть), минимальная просьба (одна правка, не «перепишите всё»)
и предложение помощи (готовый `ru_ru.json`).

```
Subject: Make <mod> UI translatable?

I maintain the Russian translation of the <pack> pack as a resourcepack (<link>).
Every mod is covered except <mod>.

I decompiled <version> and pulled the user-facing strings out of the constant
pool: N strings in M classes (logs, ids and format strings excluded). Full list
attached.

The good news — the machinery is already there: <пример: ConfigEditScreen already
resolves getTranslationKey() through I18n>, and Component.translatable is already
used in K places.

What's missing:
1. Builder.translation(...) is never called in any of the N config files — one
   call per option makes X options + Y descriptions translatable, no screen changes
2. the config tooltip body reads raw getComment(), so it needs an I18n fallback
3. <Class> (N strings) uses Component.literal — translatable is a drop-in
4. <Screen> (N strings) and its constructor take raw strings — that's the first
   screen a new player sees

Key naming is your call — I'll adapt, and I can send a ready ru_ru.json for the
main screen and the tooltips so you can check the scheme.

Full string list attached. Thanks either way.
```

Правила, без которых письмо не работает:
- **не просить доступ к исходникам** — это отпугивает и создаёт обязательства;
- **не предлагать «я сам всё сделаю»** — автору проще добавить ключи самому,
  чем принимать чужой PR в закрытый мод;
- **приложить список**, а не описывать его словами: «1808 строк» без вложения
  звучит как преувеличение;
- **дать выбор по схеме ключей** — автору важно, чтобы пространство имён
  совпадало с его конвенцией.

---

## 7. Куда это класть

Разбор мода — это артефакт, а не разовый процесс. Держи в репозитории:

- `docs/HARDCODED_<mod>.md` — таблица категорий и полный список строк;
- в `IDEAS.md` — пункт «мод X: ждём ответа автора, дата обращения».

Тогда через полгода видно, что уже просили, что ответили и что изменилось
в новой версии мода.
