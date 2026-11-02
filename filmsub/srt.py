"""Чтение и запись субтитров SRT."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TIME_RE = re.compile(
    r"(\d{1,2}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
)
POS_TAG_RE = re.compile(r"^\s*(\{\\an?\d+\})")  # {\an8} и т.п. — позиция на экране


@dataclass
class Cue:
    start_ms: int
    end_ms: int
    text: str                      # исходный текст (без позиционного тега)
    prefix: str = ""               # {\an8} и т.п., возвращается в перевод как есть
    ru: str | None = None          # перевод
    extra: dict = field(default_factory=dict)


def ms_to_ts(ms: int) -> str:
    ms = max(0, int(ms))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ms(h, m, s, frac) -> int:
    frac = (frac + "00")[:3]
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(frac)


def decode_bytes(data: bytes) -> str:
    """Определяет кодировку файла субтитров (UTF-8/UTF-16, иначе Windows-1252/1251)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # Английские субтитры обычно в cp1252; если много кириллицы — cp1251
    text1251 = data.decode("cp1251", errors="replace")
    cyr = sum(1 for ch in text1251 if "Ѐ" <= ch <= "ӿ")
    if cyr > len(text1251) * 0.2:
        return text1251
    return data.decode("cp1252", errors="replace")


def parse_srt(text: str, keep_order: bool = False) -> list[Cue]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    lines = text.split("\n")
    cues: list[Cue] = []
    i, n = 0, len(lines)
    while i < n:
        m = TIME_RE.search(lines[i])
        if not m:
            i += 1
            continue
        start = _ms(*m.group(1, 2, 3, 4))
        end = _ms(*m.group(5, 6, 7, 8))
        i += 1
        body: list[str] = []
        while i < n:
            line = lines[i]
            if not line.strip():
                # пустая строка — конец реплики, если дальше номер+время или время
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j >= n:
                    break
                if TIME_RE.search(lines[j]) or (
                    lines[j].strip().isdigit() and j + 1 < n and TIME_RE.search(lines[j + 1])
                ):
                    break
                body.append("")
                i += 1
                continue
            if line.strip().isdigit() and i + 1 < n and TIME_RE.search(lines[i + 1]) and not body:
                break
            if TIME_RE.search(line):
                break
            body.append(line.rstrip())
            i += 1
        # убрать номер следующей реплики, если он прилип в конец
        while body and not body[-1].strip():
            body.pop()
        if body and body[-1].strip().isdigit() and i < n and TIME_RE.search(lines[i]):
            body.pop()
        raw = "\n".join(body).strip()
        prefix = ""
        pm = POS_TAG_RE.match(raw)
        if pm:
            prefix = pm.group(1)
            raw = raw[pm.end():].lstrip()
        if raw:
            cues.append(Cue(start, end, raw, prefix))
    if not keep_order:
        cues.sort(key=lambda c: c.start_ms)
    return cues


def read_srt(path: str, keep_order: bool = False) -> list[Cue]:
    """keep_order=True — порядок как в файле (нужно, чтобы сопоставить реплики после alass)."""
    with open(path, "rb") as f:
        return parse_srt(decode_bytes(f.read()), keep_order=keep_order)


def build_srt(cues: list[Cue], use_translation: bool = True) -> str:
    out = []
    for idx, c in enumerate(cues, 1):
        body = (c.ru if (use_translation and c.ru) else c.text).strip()
        out.append(f"{idx}\n{ms_to_ts(c.start_ms)} --> {ms_to_ts(c.end_ms)}\n{c.prefix}{body}\n")
    return "\n".join(out)


def write_srt(path: str, cues: list[Cue], encoding: str = "utf-8-sig") -> None:
    data = build_srt(cues).replace("\n", "\r\n")
    with open(path, "w", encoding=encoding, errors="replace", newline="") as f:
        f.write(data)


# --- Пометки для слабослышащих (SDH) -------------------------------------

_SDH_BRACKETS = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}")
_SDH_SPEAKER = re.compile(r"^(\s*-?\s*)[A-Z][A-Z0-9 .'\-]{1,30}:\s*", re.MULTILINE)
_MUSIC = re.compile(r"[♪♫#]+")


def strip_sdh(text: str) -> str:
    """Убирает [звуки], (смех), «ДЖОН:» и ноты. Возвращает пустую строку, если ничего не осталось."""
    t = _SDH_BRACKETS.sub("", text)
    t = _SDH_SPEAKER.sub(r"\1", t)
    lines = []
    for line in t.split("\n"):
        stripped = _MUSIC.sub("", line).strip()
        if not stripped or stripped in {"-", "<i></i>", "<i>", "</i>"}:
            continue
        lines.append(line.strip())
    t = "\n".join(lines).strip()
    t = re.sub(r"<i>\s*</i>", "", t).strip()
    return t
