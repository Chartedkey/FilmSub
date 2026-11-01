"""Настройки программы (хранятся в %APPDATA%\\FilmSub\\settings.json)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field


def app_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    path = os.path.join(base, "FilmSub")
    os.makedirs(path, exist_ok=True)
    return path


@dataclass
class Settings:
    provider: str = "DeepSeek"
    keys: dict = field(default_factory=lambda: {"DeepSeek": "", "OpenRouter": ""})
    models: dict = field(default_factory=lambda: {
        "DeepSeek": "deepseek-v4-pro",
        "OpenRouter": "deepseek/deepseek-v4-pro",
    })
    # Цены DeepSeek ($ за 1 млн токенов) — для оценки стоимости. OpenRouter присылает точную сумму сам.
    price_in: float = 0.435
    price_cached: float = 0.0036
    price_out: float = 0.87
    profanity: str = "asis"        # asis — как в оригинале, soft — смягчать
    strip_sdh: bool = False        # убирать [звуки] и имена говорящих
    analyze_film: bool = True      # предварительный разбор персонажей и терминов
    prompt_style: str = "classic"  # classic — проверенный промпт, new — идиомы + словарь
    use_glossary: bool = True      # пополнять и использовать словарь (только для стиля new)
    compare_size: int = 150        # сколько реплик брать для сравнения стилей
    sync_after: bool = False       # синхронизировать со звуком после перевода
    batch_size: int = 40
    workers: int = 4
    overwrite: bool = False
    skip_if_russian: bool = True
    encoding: str = "utf-8-sig"    # utf-8-sig (UTF-8 с BOM) или cp1251
    last_dir: str = ""

    @property
    def api_key(self) -> str:
        return self.keys.get(self.provider, "")

    @property
    def model(self) -> str:
        return self.models.get(self.provider, "")


def settings_path() -> str:
    return os.path.join(app_dir(), "settings.json")


def load_settings() -> Settings:
    s = Settings()
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if hasattr(s, k):
                if isinstance(getattr(s, k), dict) and isinstance(v, dict):
                    getattr(s, k).update(v)
                else:
                    setattr(s, k, v)
    except (OSError, ValueError):
        pass
    return s


def save_settings(s: Settings) -> None:
    tmp = settings_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(asdict(s), f, ensure_ascii=False, indent=2)
    os.replace(tmp, settings_path())
