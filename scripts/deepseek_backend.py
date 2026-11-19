"""Бэкенд перевода через DeepSeek V4.1 Flash (OpenAI-совместимый API).

Провайдер: https://100milionseek.top/v1 , модель deepseek-v4.1-flash-free
(100 млн токенов в сутки бесплатно).

Интерфейс совпадает с GoogleBackend/DeepLBackend/GeminiBackend из tr_engine:
    translate_batch(lines: list[str]) -> list[str]
Возвращает столько же строк, сколько было на входе.

Ключ берётся из HMS_API_KEY / DEEPSEEK_API_KEY или из файла .deepseek_key рядом со скриптом.
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

SYSTEM_RULES_PATH = HERE.parent / "prompts" / "SYSTEM_PROMPT_EN_RU.txt"

FALLBACK_RULES = """Ты — профессиональный переводчик игровых локализаций Minecraft на русский.
Переводи коротко, живым игровым языком, без калек и канцелярита.
Используй официальный русский перевод Minecraft (Ice → Лёд, Deepslate → Глубинный сланец,
Ender Pearl → Эндер-жемчуг, Redstone Dust → Редстоуновая пыль).
Не трогай §-коды, плейсхолдеры (%s, %d, {0}), теги, id, URL и названия модов.
Десятичный разделитель — точка, не запятая. Регистр первой буквы и пунктуацию
в конце сохраняй как в оригинале. Строку, которую переводить нельзя, верни как есть.
Верни только JSON {"translations": [...]} — по одному элементу на входную строку."""


def load_system_rules() -> str:
    """Системный промт берётся из prompts/, чтобы не держать вторую копию в коде.

    Копия в коде расходится с файлом: правки в docs/prompts не доезжают до
    бэкенда, и модель тихо работает по устаревшим правилам.
    """
    try:
        return SYSTEM_RULES_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return FALLBACK_RULES


SYSTEM_RULES = load_system_rules()

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.M)

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class DeepSeekBackend:
    name = "deepseek"
    URL = "https://100milionseek.top/v1/chat/completions"
    max_items = 80
    max_chars = 20000

    def __init__(self, api_key: str | None = None, model: str = "deepseek-v4.1-flash-free",
                 base_url: str | None = None, delay: float = 0.4, temperature: float = 0.1,
                 thinking: bool = False):
        key = (api_key or os.environ.get("HMS_API_KEY")
               or os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        if not key:
            key_file = HERE / ".deepseek_key"
            if key_file.exists():
                key = key_file.read_text(encoding="utf-8").strip()
        if not key:
            raise RuntimeError("deepseek: нет ключа (HMS_API_KEY или .deepseek_key)")
        self.api_key = key
        self.model = model
        self.base_url = (base_url or os.environ.get("HMS_BASE_URL")
                         or "https://100milionseek.top/v1").rstrip("/")
        self.delay = delay
        self.temperature = temperature
        self.thinking = thinking
        self._lock = threading.Lock()
        self._last = 0.0
        self.calls = 0
        self.input_chars = 0
        self.output_chars = 0

        import requests
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": UA,
        })

    def _throttle(self) -> None:
        with self._lock:
            gap = time.time() - self._last
            if gap < self.delay:
                time.sleep(self.delay - gap)
            self._last = time.time()

    def _prompt(self, lines: list[str]) -> str:
        payload = json.dumps(lines, ensure_ascii=False)
        return (
            "Переведи английские строки на русский по правилам из системного сообщения.\n"
            f"Строк: {len(lines)}. Верни JSON-объект {{\"translations\": [...]}}.\n"
            f"Исходные строки:\n{payload}"
        )

    def _parse(self, text: str, n: int) -> list[str]:
        raw = _FENCE.sub("", text or "").strip()
        data = None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            start, end = raw.find("["), raw.rfind("]")
            if start >= 0 and end > start:
                try:
                    data = json.loads(raw[start:end + 1])
                except json.JSONDecodeError:
                    data = None
            if data is None:
                # мог прийти объект-обёртка
                ob = raw.find("{")
                cb = raw.rfind("}")
                if ob >= 0 and cb > ob:
                    try:
                        obj = json.loads(raw[ob:cb + 1])
                        for v in obj.values():
                            if isinstance(v, list) and len(v) == n:
                                data = v
                                break
                    except json.JSONDecodeError:
                        data = None
        if not isinstance(data, list) or len(data) != n:
            got = len(data) if isinstance(data, list) else "не список"
            raise RuntimeError(f"deepseek: ожидалось {n} строк, получено {got}")
        return [str(x) for x in data]

    def _request(self, lines: list[str]) -> list[str]:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_RULES},
                {"role": "user", "content": self._prompt(lines)},
            ],
            "temperature": self.temperature,
            "max_tokens": 8192,
            "stream": False,
        }
        if not self.thinking:
            body["thinking"] = {"type": "disabled"}
        last = ""
        for attempt in range(6):
            try:
                self._throttle()
                r = self.session.post(self.base_url + "/chat/completions", json=body, timeout=300)
                self.calls += 1
                if r.status_code == 200:
                    data = r.json()
                    msg = data["choices"][0]["message"]
                    text = msg.get("content") or ""
                    if not text:
                        # модель ушла в tool_calls — повторяем с явным запретом
                        raise RuntimeError("deepseek: пустой content")
                    self.input_chars += len(self._prompt(lines))
                    self.output_chars += len(text)
                    return self._parse(text, len(lines))
                last = f"HTTP {r.status_code}: {r.text[:300]}"
                if r.status_code in (400, 401, 403, 404):
                    raise RuntimeError(f"deepseek: {last}")
                if r.status_code in (429, 500, 502, 503, 504, 520, 521, 522, 523, 524):
                    time.sleep(min(60, 8 * (attempt + 1)) + random.uniform(0, 3))
                    continue
                raise RuntimeError(f"deepseek: {last}")
            except RuntimeError as exc:
                last = str(exc)
                if attempt == 5:
                    raise
                time.sleep(5 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
                if attempt == 5:
                    raise RuntimeError(f"deepseek: {last}") from exc
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"deepseek: {last}")

    def translate_batch(self, lines: list[str]) -> list[str]:
        if not lines:
            return []
        if len(lines) <= self.max_items and sum(len(x) for x in lines) <= self.max_chars:
            return self._request(lines)
        out: list[str] = []
        chunk: list[str] = []
        size = 0
        for ln in lines:
            if chunk and (len(chunk) >= self.max_items or size + len(ln) > self.max_chars):
                out.extend(self._request(chunk))
                chunk, size = [], 0
            chunk.append(ln)
            size += len(ln)
        if chunk:
            out.extend(self._request(chunk))
        return out

    # ------------------------------------------------------------------
    # Перевод с подсказками терминов
    # ------------------------------------------------------------------

    def _prompt_hinted(self, items: list[dict]) -> str:
        payload = json.dumps(items, ensure_ascii=False)
        return (
            "Входные строки (en — английский текст с защищёнными токенами [[N]], "
            "terms — принятые переводы терминов):\n" + payload
        )

    def _request_hinted(self, items: list[dict]) -> list[str]:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_RULES},
                {"role": "user", "content": self._prompt_hinted(items)},
            ],
            "temperature": self.temperature,
            "max_tokens": 8192,
            "stream": False,
        }
        last = ""
        for attempt in range(6):
            try:
                self._throttle()
                r = self.session.post(self.base_url + "/chat/completions", json=body, timeout=300)
                self.calls += 1
                if r.status_code == 200:
                    data = r.json()
                    msg = data["choices"][0]["message"]
                    text = msg.get("content") or ""
                    if not text:
                        raise RuntimeError("deepseek: пустой content")
                    self.input_chars += len(self._prompt_hinted(items))
                    self.output_chars += len(text)
                    return self._parse_obj(text, len(items))
                last = f"HTTP {r.status_code}: {r.text[:300]}"
                if r.status_code in (400, 401, 403, 404):
                    raise RuntimeError(f"deepseek: {last}")
                if r.status_code in (429, 500, 502, 503, 504, 520, 521, 522, 523, 524):
                    time.sleep(min(60, 8 * (attempt + 1)) + random.uniform(0, 3))
                    continue
                raise RuntimeError(f"deepseek: {last}")
            except RuntimeError as exc:
                last = str(exc)
                if attempt == 5:
                    raise
                time.sleep(5 * (attempt + 1))
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
                if attempt == 5:
                    raise RuntimeError(f"deepseek: {last}") from exc
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"deepseek: {last}")

    def _parse_obj(self, text: str, n: int) -> list[str]:
        raw = _FENCE.sub("", text or "").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            ob, cb = raw.find("{"), raw.rfind("}")
            if ob < 0 or cb <= ob:
                raise RuntimeError(f"deepseek: не разобрал JSON: {raw[:200]}")
            data = json.loads(raw[ob:cb + 1])
        if isinstance(data, dict):
            for key in ("translations", "result", "results", "ru", "items"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list) or len(data) != n:
            got = len(data) if isinstance(data, list) else "не список"
            raise RuntimeError(f"deepseek: ожидалось {n} строк, получено {got}")
        return [str(x) for x in data]

    def translate_batch_hinted(self, items: list[dict]) -> list[str]:
        """items: [{"en": str, "terms": {EN: RU}}, ...] -> список переводов."""
        if not items:
            return []
        out: list[str] = []
        chunk: list[dict] = []
        size = 0
        for it in items:
            ln = it["en"]
            if chunk and (len(chunk) >= self.max_items or size + len(ln) > self.max_chars):
                out.extend(self._request_hinted(chunk))
                chunk, size = [], 0
            chunk.append(it)
            size += len(ln)
        if chunk:
            out.extend(self._request_hinted(chunk))
        return out
