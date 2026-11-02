"""Клиент OpenAI-совместимого API (DeepSeek, OpenRouter) на стандартной библиотеке."""

from __future__ import annotations

import http.client
import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import __version__

USER_AGENT = f"FilmSub/{__version__} (+https://github.com/Chartedkey/FilmSub)"

PROVIDERS = {
    "DeepSeek": {
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "key_hint": "Ключ: platform.deepseek.com → API keys",
    },
    "OpenRouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash"],
        "key_hint": "Ключ: openrouter.ai → Keys",
    },
}


class ApiError(Exception):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class Usage:
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None   # точная стоимость (OpenRouter сообщает сам)
    truncated: bool = False         # ответ упёрся в лимит длины

    def add(self, other: "Usage"):
        self.prompt_tokens += other.prompt_tokens
        self.cached_tokens += other.cached_tokens
        self.completion_tokens += other.completion_tokens
        if other.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + other.cost_usd


class ChatClient:
    def __init__(self, provider: str, api_key: str, model: str, timeout: int = 180):
        if provider not in PROVIDERS:
            raise ValueError(f"Неизвестный провайдер: {provider}")
        self.provider = provider
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.base_url = PROVIDERS[provider]["base_url"]
        self.timeout = timeout
        self.json_ok = True
        self._ctx = ssl.create_default_context()

    def _post(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": USER_AGENT,
        }
        if self.provider == "OpenRouter":
            headers["HTTP-Referer"] = "https://github.com/filmsub"
            headers["X-Title"] = "FilmSub"
        req = urllib.request.Request(self.base_url + path, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            code = e.code
            tail = f" Ответ сервера: {detail}" if detail else ""
            if code == 401:
                raise ApiError("Неверный API-ключ (401). Проверьте ключ в настройках." + tail, retryable=False)
            if code == 402:
                raise ApiError("Недостаточно средств на балансе (402). Пополните счёт." + tail, retryable=False)
            if code == 403:
                raise ApiError(f"Доступ запрещён (403): {detail}", retryable=False)
            if code == 404:
                raise ApiError(f"Модель «{self.model}» не найдена (404). Проверьте название модели." + tail,
                               retryable=False)
            if code == 400:
                raise ApiError(f"Сервер отклонил запрос (400): {detail}", retryable=False)
            raise ApiError(f"Ошибка сервера {code}: {detail}")
        except urllib.error.URLError as e:
            raise ApiError(f"Нет связи с сервером: {e.reason}")
        except (TimeoutError, OSError) as e:
            raise ApiError(f"Сетевая ошибка: {e}")
        except http.client.HTTPException as e:
            # соединение оборвалось посреди ответа (IncompleteRead, RemoteDisconnected…) — повторяем
            raise ApiError(f"Связь с сервером оборвалась ({type(e).__name__})")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError("Сервер вернул непонятный ответ")

    def chat(self, system: str, user: str, max_tokens: int = 8000,
             temperature: float = 0.6, json_mode: bool = True,
             reasoning: bool | None = False) -> tuple[str, Usage]:
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.provider == "DeepSeek":
            # Режим «размышлений» для перевода не нужен — только тратит токены
            payload["thinking"] = {"type": "disabled"}
        else:
            if reasoning is not None:      # None — оставить модели её обычный режим
                payload["reasoning"] = {"enabled": reasoning}
            payload["usage"] = {"include": True}

        data = self._post("/chat/completions", payload)
        if "error" in data:
            msg = data["error"].get("message") if isinstance(data["error"], dict) else data["error"]
            raise ApiError(f"Ошибка API: {msg}")
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            raise ApiError("В ответе нет текста перевода")
        u = data.get("usage") or {}
        details = u.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens") or u.get("prompt_cache_hit_tokens") or 0
        usage = Usage(
            prompt_tokens=int(u.get("prompt_tokens") or 0),
            cached_tokens=int(cached),
            completion_tokens=int(u.get("completion_tokens") or 0),
            cost_usd=float(u["cost"]) if isinstance(u.get("cost"), (int, float)) else None,
        )
        # Обрезанный ответ не считаем ошибкой: токены уже оплачены, а вызывающий код
        # сам решит, что делать (взять часть или разбить пачку на меньшие).
        usage.truncated = choice.get("finish_reason") == "length"
        return content, usage

    def chat_retry(self, *args, attempts: int = 4, stop_flag=None, log=None, **kwargs):
        delay = 3.0
        last: Exception | None = None
        for i in range(attempts):
            if stop_flag and stop_flag():
                raise ApiError("Остановлено", retryable=False)
            try:
                if not self.json_ok:
                    kwargs["json_mode"] = False
                return self.chat(*args, **kwargs)
            except ApiError as e:
                last = e
                if "(400)" in str(e) and self.json_ok and kwargs.get("json_mode", True):
                    # Некоторые модели не поддерживают JSON-режим — работаем без него
                    self.json_ok = False
                    continue
                if not e.retryable:
                    raise
                if log:
                    log(f"  ⚠ {e} — повтор через {int(delay)} с")
                time.sleep(delay)
                delay = min(delay * 2, 60)
        raise last if last else ApiError("Неизвестная ошибка")


def openrouter_model_info(timeout: int = 30) -> dict[str, dict]:
    """id модели → {"name", "prompt" и "completion" (цена за токен, $), "context"}."""
    req = urllib.request.Request("https://openrouter.ai/api/v1/models", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    info = {}
    for m in data.get("data", []):
        if "id" not in m:
            continue
        pr = m.get("pricing") or {}

        def num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        info[m["id"]] = {"name": m.get("name") or m["id"], "prompt": num(pr.get("prompt")),
                         "completion": num(pr.get("completion")), "context": m.get("context_length")}
    return info


def list_openrouter_models(timeout: int = 30) -> list[str]:
    req = urllib.request.Request("https://openrouter.ai/api/v1/models", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return sorted(m["id"] for m in data.get("data", []) if "id" in m)
