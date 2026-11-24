# Translating Minecraft modpacks with AI

A method and a set of tools to **drive Russian all the way through a modpack**:
mod interfaces, item tooltips, quests, books — without breaking the game in the process.

**[Русская версия →](README.md)**

The method has been proven on modpacks with 300+ mods. A finished translation lives in
**[`AxopupelBruh/linggango-ru`](https://github.com/AxopupelBruh/linggango-ru)**:
300 namespaces, 110 thousand translations, 23 thousand quests, 9 thousand tooltip strings.

> **The translation was produced with the help of AI.** Same here: this guide, the
> prompts and the scripts are the tools an AI agent uses to work on a modpack.

---

## Quick start

1. Copy `docs/GUIDE.md` and `prompts/SYSTEM_PROMPT_EN_RU.txt`.
2. Give those two files and the path to the modpack instance to an AI agent.
3. The agent will ask three questions (translation backend, scope, images) — answer them.
4. After that it works on its own: recon → installing packs → auditing leftovers →
   translating → building → acceptance.

Or by hand:

```bash
# 1. recon: scope, what is already translated, where the risks are (changes nothing)
python3 scripts/audit_pack.py <instance>
python3 scripts/audit_pack.py <instance> -o work/RECON.md --json work/recon.json

# 2. what is still in English
python3 scripts/collect_text_png.py <instance> ./png-scan   # images with text
python3 scripts/audit_font_coverage.py <instance> --font-json <include/default.json>

# 3. translate (prompts in prompts/, backends in scripts/)
python3 scripts/translate_by_role.py work/pairs.json -o work/mods_ru.json
# 4. cheap filters before the audit
python3 scripts/lint_calc.py work/mods_ru.json --fix             # calques, no model needed
python3 scripts/back_translate.py work/pairs.json                # mechanics: numbers, sign, negation
# 5. check the translation before building — blockers show up immediately
python3 scripts/check_translation.py work/mods_ru.json
python3 scripts/check_translation.py work/mods_ru.json --all      # + style

# 6. build and verify
python3 build_resourcepack.py
python3 verify_all.py
```

If a mod ships a Russian file yet half of its interface stays in English, that text is
hardcoded — see [`docs/HARDCODED.md`](docs/HARDCODED.md):

```bash
python3 scripts/dump_hardcoded.py mods/<mod>.jar -o HARDCODED.md
```

---

## What you must know before starting

**1. A resource pack's lang file REPLACES the mod's file — it does not merge.**
Drop `assets/<ns>/lang/ru_ru.json` with a single line into a pack and the mod loses
**its entire** Russian translation. Same for vanilla: `assets/minecraft/lang/ru_ru.json`
must contain all ~8 800 keys, otherwise the whole game UI falls back to English.
Only put **complete** lang files into a pack.

**1a. A complete lang file is the union of every source, not just the mods.**
Our pack goes last, so it overrides the lang files of **every** source below it: mod
translations, the KubeJS pack, the `serilum` pack, third-party localization packs
(`MPLOCmods`). A missing key means English in game even though a translation exists.
Build order: `mod → KubeJS → serilum → third-party packs → our fixes`, and write the
merged result into the file as a whole. See `reference/linggango/lang_sources.py`.

**2. Our resource pack goes last in the list.** It has the highest priority.
Put it earlier and another pack overrides it; put another pack later and it overrides ours.

**3. Mod jars are never patched.** The modpack updates through CurseForge and a patch
would be wiped. The whole translation lives in external layers: resource pack, data pack,
scripts.

**4. Not a single line is handed to the translator as-is.** Everything that can break
(`§` codes, placeholders, tags, IDs, brands, invisible characters) is masked with
tokens first and restored after translation.

**5. We do not damage other people's translations.** If a mod already provides correct
Russian, we keep it. We only override empty and English values.

The full list is in [`docs/TRAPS.md`](docs/TRAPS.md): 29 traps with symptom and fix.

---

## What's inside

| Folder | What it is |
|---|---|
| [`docs/GUIDE.md`](docs/GUIDE.md) | **The main document.** The whole method: agent role, questions for the user, hard rules, recon, layers, formats, terminology, pipeline, acceptance, command cheatsheet |
| [`docs/TRAPS.md`](docs/TRAPS.md) | 29 traps: lang replacement instead of merging, pack order, truncated cache, quest encryption, ZWNJ, characters without a glyph, a mod's shifted `ru_ru`, a Cyrillic "с" inside `%s`, the false "keys still in English" metric, silently erased translations |
| [`docs/HARDCODED.md`](docs/HARDCODED.md) | The mod does not translate although `ru_ru.json` exists: how to prove the text is hardcoded, how to estimate the volume, a template for writing to the author |
| [`docs/UPGRADES.md`](docs/UPGRADES.md) | What survives a modpack update, what breaks, and what to do in each case |
| [`docs/ALTERNATIVES.md`](docs/ALTERNATIVES.md) | Comparison: off-the-shelf GUI tools vs manual work vs AI + your own pipeline |
| [`prompts/SYSTEM_PROMPT_EN_RU.txt`](prompts/SYSTEM_PROMPT_EN_RU.txt) | The translator's system prompt: the official vanilla translation (8 799 keys), § codes, grammatical cases, terms, brands, self-check. Proven on 34 500 strings |
| [`prompts/AUDIT_PROMPT.txt`](prompts/AUDIT_PROMPT.txt) | Audit prompt: find defects in a finished translation — untranslated strings, broken substitutions, calques, terms that drifted apart |
| [`prompts/PROOFREAD_PROMPT.txt`](prompts/PROOFREAD_PROMPT.txt) | Proofreading prompt: make the Russian sound natural without changing the meaning (bureaucratic phrasing, word order, gender, repetition) |
| [`prompts/RECON_PROMPT.txt`](prompts/RECON_PROMPT.txt) | Recon prompt: gather the facts about a modpack and flag the risks right away, before any translating starts |
| `scripts/` | Portable pieces: modpack recon, translation validator, hardcoded text analysis, lang/SNBT parsers, brand masking, DeepL and DeepSeek backends, KubeJS translator builder, image text finder, glyph audit, string roles and role batches, calque linter, back-translation, glossary quarantine |
| `data/` | Working dictionaries the scripts produce: `glossary.json` (active), `glossary_candidates.json` (quarantine), `adaptation_examples.json` (decisions on jokes) |
| `reference/linggango/` | A complete pipeline on a real modpack: resource pack build, quest encryption, acceptance. **Rewrite it for your own modpack** |
| `templates/` | Boilerplate for publishing your own translation: README template, `.gitignore`, release builder |
| [`IDEAS.md`](IDEAS.md) | What to add next — from a web glossary editor to CI |

---

## Translation layers the method covers

| Layer | How |
|---|---|
| Mod lang files | complete `ru_ru.json` in a resource pack, key = path |
| Vanilla UI | the complete official `ru_ru` for the right version |
| Strings from Java code | `.class` scan + tooltip interceptor (KubeJS → Architectury → Forge) |
| FTB Quests | editing text fields, ids/coordinates untouched, re-encrypted with your own key |
| Patchouli books | pages, `$(...)` macros untouched |
| Dungeon books | a data pack overriding the loot tables |
| KubeJS scripts | placeholder strings |
| Images | flat 2D text only; 3D logos are brands |

---

## What's in the translator prompt

`prompts/SYSTEM_PROMPT_EN_RU.txt` (397 lines) is the single source of rules:
`scripts/deepseek_backend.py` reads it from the file, there is no copy left in the code.

- **§0 — the official Russian Minecraft translation** as the source of truth:
  8 799 vanilla keys, `Deepslate → Глубинный сланец`, `Redstone Dust →
  Редстоуновая пыль`, `Ender Pearl → Эндер-жемчуг`, `Elytra → Элитры`;
- **§0.1 — verified vanilla terms** verbatim, so the translation does not drift from the game;
- **§1 — what kind of string this is**: an item name, a tooltip, a subtitle, a quest
  description are translated differently;
- **§3 — grammatical cases and agreement** — «повышает максимальное здоровье», not
  «Увеличивает Вашу Максимальную Здоровье»;
- **§4 — Russian capitalisation ≠ English**: a tooltip fragment that starts lowercase
  does not become a sentence;
- **§6 — § codes, placeholders, units** stay untouched, decimals keep the dot;
- **§8 — what not to translate**: brands (FTB, JEI, Create, Mekanism, Ars Nouveau),
  ids, config values (`toggle,hold,lite`), author handles, track names;
- **§9.1 — consistency within a request**: one term is translated the same way across
  all strings in a batch;
- **§9.2 — glossary candidates**: the model may propose a term, but it goes to
  quarantine, not to the active glossary;
- **§9.3 — dubious strings**: jokes and idioms are returned in a separate field for
  manual adaptation;
- **§12 — self-check** before the answer is returned.

Three work modes come with the prompt: audit (`AUDIT_PROMPT.txt`), proofreading
(`PROOFREAD_PROMPT.txt`) and recon (`RECON_PROMPT.txt`). Each prompt starts with a
`# PROMPT_NAME / # STAGE / # INPUT` header in its first three lines, so the files do not
get mixed up when an agent reads them in a batch.

---

## Requirements

```bash
pip install pillow fonttools requests        # images, fonts and API
apt install tesseract-ocr imagemagick        # OCR and contact sheets
```

Everything else is the Python 3.10+ standard library.

---

## License and attribution

The guide, prompts and scripts are MIT (see `LICENSE`). Translations, dictionaries and
guides produced while working on a particular modpack belong to that modpack's authors:
mod texts, names and icons are the property of their authors — only the translation
is published here.
