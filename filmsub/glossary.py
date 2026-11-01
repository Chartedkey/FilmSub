"""Накопительный словарь сленга, идиом и реалий (%APPDATA%\\FilmSub\\glossary.json).

Словарь пополняется автоматически при разборе каждого фильма (записи со статусом «new»),
пользователь может их проверить («ok»), исправить или удалить. В запрос к модели попадают
только выражения, которые встречаются в субтитрах текущего фильма.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field

from .config import app_dir

_lock = threading.Lock()


@dataclass
class Entry:
    en: str                 # выражение по-английски
    ru: str                 # как переводить / что значит
    note: str = ""          # пояснение: регион, эпоха, когда значит другое
    status: str = "new"     # new — найдено автоматически, ok — проверено пользователем
    films: list = field(default_factory=list)   # в каких фильмах встречалось
    added: str = ""

    @property
    def key(self) -> str:
        return normalize(self.en)


def normalize(text: str) -> str:
    t = text.lower().replace("’", "'").replace("‘", "'")
    t = re.sub(r"[^\w' -]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def glossary_path() -> str:
    return os.path.join(app_dir(), "glossary.json")


class Glossary:
    def __init__(self, entries: list[Entry] | None = None):
        self.entries: dict[str, Entry] = {}
        for e in entries or []:
            if e.key:
                self.entries[e.key] = e

    # --- файл ---
    @classmethod
    def load(cls) -> "Glossary":
        try:
            with open(glossary_path(), encoding="utf-8") as f:
                data = json.load(f)
            items = []
            for d in data.get("entries", []):
                try:
                    items.append(Entry(**{k: v for k, v in d.items() if k in Entry.__annotations__}))
                except TypeError:
                    continue
            return cls(items)
        except (OSError, ValueError):
            return cls()

    def save(self) -> None:
        with _lock:
            data = {"version": 1, "entries": [asdict(e) for e in
                                              sorted(self.entries.values(), key=lambda e: e.key)]}
            tmp = glossary_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, glossary_path())

    # --- изменение ---
    def merge_auto(self, found: list[Entry], film: str) -> int:
        """Добавляет найденные при разборе выражения. Проверенные пользователем не меняются.
        Возвращает число новых записей."""
        added = 0
        for e in found:
            k = e.key
            if not k or not e.ru.strip():
                continue
            cur = self.entries.get(k)
            if cur:
                if film and film not in cur.films:
                    cur.films.append(film)
                    cur.films = cur.films[-20:]
                continue
            e.status = "new"
            e.films = [film] if film else []
            e.added = time.strftime("%Y-%m-%d")
            self.entries[k] = e
            added += 1
        return added

    def upsert(self, en: str, ru: str, note: str = "", status: str = "ok") -> None:
        e = Entry(en=en.strip(), ru=ru.strip(), note=note.strip(), status=status,
                  added=time.strftime("%Y-%m-%d"))
        old = self.entries.get(e.key)
        if old:
            e.films = old.films
            e.added = old.added
        self.entries[e.key] = e

    def delete(self, key: str) -> None:
        self.entries.pop(key, None)

    # --- отбор для фильма ---
    def relevant(self, text: str, extra: list[Entry] | None = None, limit: int = 60) -> list[Entry]:
        """Выражения из словаря (и extra — найденные в этом фильме), которые встречаются в тексте."""
        norm = " " + normalize(text) + " "
        pool: dict[str, Entry] = dict(self.entries)
        for e in extra or []:
            pool.setdefault(e.key, e)
        hits = []
        for k, e in pool.items():
            if k and f" {k} " in norm:
                hits.append(e)
        hits.sort(key=lambda e: (e.status != "ok", -len(e.films)))
        return hits[:limit]


def format_for_prompt(entries: list[Entry]) -> str:
    lines = []
    for e in entries:
        line = f"- {e.en} — {e.ru}"
        if e.note:
            line += f" ({e.note})"
        lines.append(line)
    return "\n".join(lines)


GLOSSARY_MARK = "СЛОВАРЬ:"


def parse_glossary_block(text: str) -> tuple[str, list[Entry]]:
    """Отделяет от справки блок «СЛОВАРЬ:» со строками «en | ru | пояснение»."""
    idx = text.find(GLOSSARY_MARK)
    if idx < 0:
        return text.strip(), []
    notes = text[:idx].strip()
    entries = []
    for line in text[idx + len(GLOSSARY_MARK):].splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if "|" not in line:
            continue
        parts = [p.strip().strip("«»\"'") for p in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        if len(parts[0]) > 60 or len(parts[1]) > 120:
            continue
        entries.append(Entry(en=parts[0], ru=parts[1], note=parts[2] if len(parts) > 2 else ""))
    return notes, entries
