"""Выбор основного фильма в незашифрованной копии DVD / Blu-ray."""
from __future__ import annotations

import os
import re
import struct
from pathlib import Path


def child(folder: Path, name: str) -> Path | None:
    return next((p for p in folder.iterdir() if p.name.casefold() == name.casefold()), None)


def disc_folder(path: str) -> Path | None:
    folder = Path(path)
    if not folder.is_dir():
        return None
    if folder.name.upper() in {"VIDEO_TS", "BDMV"}:
        return folder
    return child(folder, "VIDEO_TS") or child(folder, "BDMV")


def _playlist(path: Path, streams: Path) -> list[tuple[Path, float, float]]:
    data = path.read_bytes()
    if data[:8] not in (b"MPLS0100", b"MPLS0200", b"MPLS0300"):
        raise ValueError("неизвестный формат MPLS")
    offset = struct.unpack_from(">I", data, 8)[0]
    length = struct.unpack_from(">I", data, offset)[0]
    end = offset + 4 + length
    if end > len(data):
        raise ValueError("обрезанный MPLS")
    count = struct.unpack_from(">H", data, offset + 6)[0]
    pos = offset + 10
    clips = []
    for _ in range(count):
        size = struct.unpack_from(">H", data, pos)[0]
        item = data[pos + 2:pos + 2 + size]
        if len(item) != size or size < 20 or pos + 2 + size > end:
            raise ValueError("повреждённый PlayItem")
        if item[5:9] != b"M2TS":
            raise ValueError("неподдерживаемый тип клипа")
        name = item[:5].decode("ascii")
        if not re.fullmatch(r"\d{5}", name):
            raise ValueError("недопустимое имя клипа")
        start, stop = struct.unpack_from(">II", item, 12)
        clip = child(streams, name + ".m2ts")
        if clip is None or stop <= start:
            raise ValueError("клип отсутствует или имеет пустой интервал")
        clips.append((clip, start / 45000, stop / 45000))
        pos += 2 + size
    if not clips:
        raise ValueError("пустой плейлист")
    return clips


def main_feature(path: str) -> tuple[str, list[tuple[Path, float | None, float | None]]]:
    folder = disc_folder(path)
    if folder is None:
        raise ValueError("Выберите папку VIDEO_TS, BDMV или корень диска")
    if folder.name.upper() == "VIDEO_TS":
        titles = {}
        for p in folder.iterdir():
            match = re.fullmatch(r"VTS_(\d{2})_([1-9])\.VOB", p.name, re.I)
            if match and p.is_file():
                titles.setdefault(match[1], []).append((int(match[2]), p))
        if not titles:
            raise ValueError("В VIDEO_TS нет VOB основного фильма")
        title, files = max(sorted(titles.items()), key=lambda pair: sum(p.stat().st_size for _, p in pair[1]))
        files.sort()
        if [n for n, _ in files] != list(range(1, len(files) + 1)):
            raise ValueError("В DVD отсутствует часть VOB основного фильма")
        return f"DVD VTS_{title}", [(p, None, None) for _, p in files]
    playlists, streams = child(folder, "PLAYLIST"), child(folder, "STREAM")
    if playlists is None or streams is None:
        raise ValueError("В BDMV нужны папки PLAYLIST и STREAM")
    candidates = []
    for p in sorted(playlists.iterdir()):
        if p.suffix.lower() == ".mpls":
            try:
                clips = _playlist(p, streams)
                candidates.append((sum(b - a for _, a, b in clips), p.name, clips))
            except (ValueError, OSError, struct.error, UnicodeError):
                continue
    if not candidates:
        raise ValueError("В BDMV нет доступного корректного плейлиста MPLS")
    _, name, clips = max(candidates, key=lambda x: (x[0], x[1]))
    return f"Blu-ray {name}", clips


def dvd_languages(path: str, title: str) -> list[str]:
    """Языки VTS-аудио из IFO: в самих VOB языковых меток нет."""
    folder = disc_folder(path)
    if folder is None:
        return []
    ifo = child(folder, title.replace("DVD ", "") + "_0.IFO")
    if ifo is None:
        return []
    data = ifo.read_bytes()
    if data[:12] != b"DVDVIDEO-VTS" or len(data) < 0x204:
        return []
    count = struct.unpack_from(">H", data, 0x202)[0]
    if count > 8 or len(data) < 0x204 + count * 8:
        return []
    langs = []
    for i in range(count):
        attr = data[0x204 + i * 8:0x20c + i * 8]
        code = attr[2:4].decode("ascii", "replace").lower()
        langs.append(code if attr[0] & 0x0c == 0x04 and re.fullmatch("[a-z]{2}", code) else "")
    return langs
