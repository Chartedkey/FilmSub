"""Извлечение текстовых субтитров из MKV без внешних программ.

Поддерживаются дорожки S_TEXT/UTF8 (SRT) и S_TEXT/ASS, S_TEXT/SSA.
Графические субтитры (PGS, VobSub) — это картинки, их перевести нельзя.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field

from .srt import Cue

# EBML-идентификаторы
SEGMENT = 0x18538067
INFO = 0x1549A966
TIMECODE_SCALE = 0x2AD7B1
TRACKS = 0x1654AE6B
TRACK_ENTRY = 0xAE
TRACK_NUMBER = 0xD7
TRACK_TYPE = 0x83
CODEC_ID = 0x86
LANGUAGE = 0x22B59C
LANGUAGE_IETF = 0x22B59D
NAME = 0x536E
FLAG_DEFAULT = 0x88
FLAG_FORCED = 0x55AA
CONTENT_ENCODINGS = 0x6D80
CONTENT_ENCODING = 0x6240
CONTENT_COMPRESSION = 0x5034
CONTENT_COMP_ALGO = 0x4254
CONTENT_COMP_SETTINGS = 0x4255
CLUSTER = 0x1F43B675
CLUSTER_TIMECODE = 0xE7
SIMPLE_BLOCK = 0xA3
BLOCK_GROUP = 0xA0
BLOCK = 0xA1
BLOCK_DURATION = 0x9B

MASTER_CONTAINERS = {SEGMENT, CLUSTER}
TEXT_CODECS = {"S_TEXT/UTF8", "S_TEXT/ASCII", "S_TEXT/ASS", "S_TEXT/SSA", "S_ASS", "S_SSA"}
UNKNOWN = -1


class MkvError(Exception):
    pass


@dataclass
class SubTrack:
    number: int
    codec: str
    language: str = "eng"       # по спецификации умолчание — английский
    name: str = ""
    default: bool = False
    forced: bool = False
    comp_algo: int | None = None    # 0 = zlib, 3 = header stripping
    comp_settings: bytes = b""
    cues: list[Cue] = field(default_factory=list)

    @property
    def is_text(self) -> bool:
        return self.codec in TEXT_CODECS

    @property
    def is_english(self) -> bool:
        lang = self.language.lower()
        return lang.startswith("en") or lang == "eng"

    @property
    def is_russian(self) -> bool:
        return self.language.lower() in {"ru", "rus", "ru-ru"}

    @property
    def is_sdh(self) -> bool:
        n = self.name.lower()
        return "sdh" in n or "hearing" in n or "cc" == n.strip()

    def describe(self) -> str:
        parts = [f"#{self.number}", self.codec, self.language]
        if self.name:
            parts.append(f"«{self.name}»")
        if self.forced:
            parts.append("forced")
        return " ".join(parts)


def _read_vint(f, keep_marker: bool):
    first = f.read(1)
    if not first:
        return None, 0
    b = first[0]
    length = 1
    mask = 0x80
    while length <= 8 and not (b & mask):
        mask >>= 1
        length += 1
    if length > 8:
        raise MkvError("Повреждённый MKV (неверное число переменной длины)")
    rest = f.read(length - 1)
    if len(rest) < length - 1:
        return None, 0
    value = b if keep_marker else (b & (mask - 1))
    all_ones = (b & (mask - 1)) == (mask - 1)
    for x in rest:
        value = (value << 8) | x
        all_ones = all_ones and x == 0xFF
    if not keep_marker and all_ones:
        return UNKNOWN, length
    return value, length


def _read_header(f):
    eid, l1 = _read_vint(f, True)
    if eid is None:
        return None
    size, l2 = _read_vint(f, False)
    if size is None:
        return None
    return eid, size, l1 + l2


def _vint_from(buf: bytes, pos: int):
    b = buf[pos]
    length = 1
    mask = 0x80
    while length <= 8 and not (b & mask):
        mask >>= 1
        length += 1
    value = b & (mask - 1)
    for x in buf[pos + 1: pos + length]:
        value = (value << 8) | x
    return value, length


def _iter_children(buf: bytes):
    """Перебор дочерних элементов в уже прочитанном буфере."""
    pos = 0
    n = len(buf)
    while pos < n:
        # id (с маркером)
        b = buf[pos]
        length = 1
        mask = 0x80
        while length <= 4 and not (b & mask):
            mask >>= 1
            length += 1
        eid = int.from_bytes(buf[pos:pos + length], "big")
        pos += length
        if pos >= n:
            return
        size, sl = _vint_from(buf, pos)
        pos += sl
        yield eid, buf[pos:pos + size]
        pos += size


def _uint(data: bytes) -> int:
    return int.from_bytes(data, "big") if data else 0


def _parse_tracks(buf: bytes) -> dict[int, SubTrack]:
    tracks: dict[int, SubTrack] = {}
    for eid, data in _iter_children(buf):
        if eid != TRACK_ENTRY:
            continue
        info = {}
        t = SubTrack(number=0, codec="")
        for cid, cdata in _iter_children(data):
            if cid == TRACK_NUMBER:
                t.number = _uint(cdata)
            elif cid == TRACK_TYPE:
                info["type"] = _uint(cdata)
            elif cid == CODEC_ID:
                t.codec = cdata.decode("ascii", "replace").strip("\x00")
            elif cid == LANGUAGE:
                t.language = cdata.decode("ascii", "replace").strip("\x00") or "eng"
            elif cid == LANGUAGE_IETF:
                info["ietf"] = cdata.decode("ascii", "replace").strip("\x00")
            elif cid == NAME:
                t.name = cdata.decode("utf-8", "replace").strip("\x00")
            elif cid == FLAG_DEFAULT:
                t.default = bool(_uint(cdata))
            elif cid == FLAG_FORCED:
                t.forced = bool(_uint(cdata))
            elif cid == CONTENT_ENCODINGS:
                for _, enc in _iter_children(cdata):
                    for eid2, edata in _iter_children(enc):
                        if eid2 == CONTENT_COMPRESSION:
                            t.comp_algo = 0
                            for eid3, cd in _iter_children(edata):
                                if eid3 == CONTENT_COMP_ALGO:
                                    t.comp_algo = _uint(cd)
                                elif eid3 == CONTENT_COMP_SETTINGS:
                                    t.comp_settings = cd
        if info.get("ietf"):
            t.language = info["ietf"]
        if info.get("type") == 0x11:
            tracks[t.number] = t
    return tracks


_ASS_TAG = re.compile(r"\{[^}]*\}")


def _ass_to_text(payload: str) -> str:
    # В MKV: ReadOrder,Layer,Style,Name,MarginL,MarginR,MarginV,Effect,Text
    parts = payload.split(",", 8)
    text = parts[8] if len(parts) == 9 else payload
    italic = "{\\i1}" in text
    text = _ASS_TAG.sub("", text)
    text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ").strip()
    if italic and text:
        text = f"<i>{text}</i>"
    return text


def _decode_payload(track: SubTrack, data: bytes) -> str:
    if track.comp_algo == 0:
        try:
            data = zlib.decompress(data)
        except zlib.error:
            pass
    elif track.comp_algo == 3:
        data = track.comp_settings + data
    text = data.decode("utf-8", "replace").rstrip("\x00").strip()
    if track.codec in {"S_TEXT/ASS", "S_TEXT/SSA", "S_ASS", "S_SSA"}:
        text = _ass_to_text(text)
    return text.replace("\r\n", "\n")


def read_mkv_subtitles(path: str, wanted=None, progress=None) -> list[SubTrack]:
    """Читает список субтитровых дорожек и (для дорожек из wanted) их содержимое.

    wanted — функция SubTrack -> bool; если None, только список дорожек без чтения реплик.
    progress — функция (доля 0..1).
    """
    import os

    filesize = os.path.getsize(path)
    tracks: dict[int, SubTrack] = {}
    timecode_scale = 1_000_000
    targets: dict[int, SubTrack] = {}
    tracks_read = False

    with open(path, "rb", buffering=1 << 16) as f:
        hdr = _read_header(f)
        if not hdr or hdr[0] != 0x1A45DFA3:
            raise MkvError("Это не MKV-файл")
        f.seek(hdr[1], 1)

        hdr = _read_header(f)
        while hdr and hdr[0] != SEGMENT:
            f.seek(hdr[1], 1)
            hdr = _read_header(f)
        if not hdr:
            raise MkvError("В файле нет сегмента Matroska")
        seg_end = filesize if hdr[1] == UNKNOWN else f.tell() + hdr[1]

        last_report = 0
        cluster_tc = 0
        cluster_end = None
        while f.tell() < seg_end:
            pos = f.tell()
            if cluster_end is not None and pos >= cluster_end:
                cluster_end = None
            h = _read_header(f)
            if not h:
                break
            eid, size, _ = h

            if eid == CLUSTER:
                if not tracks_read:
                    # Дорожки всегда перед первым кластером
                    tracks_read = True
                    if wanted is None:
                        break
                    targets = {n: t for n, t in tracks.items() if t.is_text and wanted(t)}
                    if not targets:
                        break
                cluster_end = None if size == UNKNOWN else f.tell() + size
                cluster_tc = 0
                if progress and pos - last_report > 50_000_000:
                    last_report = pos
                    progress(pos / filesize)
                continue  # заходим внутрь кластера

            if size == UNKNOWN:
                if eid in MASTER_CONTAINERS:
                    continue
                raise MkvError("Неподдерживаемая структура MKV")

            if eid == INFO:
                for cid, cdata in _iter_children(f.read(size)):
                    if cid == TIMECODE_SCALE:
                        timecode_scale = _uint(cdata) or 1_000_000
            elif eid == TRACKS:
                tracks = _parse_tracks(f.read(size))
            elif eid == CLUSTER_TIMECODE:
                cluster_tc = _uint(f.read(size))
            elif eid in (SIMPLE_BLOCK, BLOCK_GROUP) and targets:
                start = f.tell()
                if eid == SIMPLE_BLOCK:
                    head = f.read(min(size, 8))
                    tn, _ = _vint_from(head, 0) if head else (0, 0)
                    if tn in targets:
                        f.seek(start)
                        _handle_block(f.read(size), None, targets, cluster_tc, timecode_scale)
                else:
                    # BlockGroup: обычно первым идёт Block — смотрим номер дорожки
                    head = f.read(min(size, 16))
                    tn = _peek_group_track(head)
                    if tn in targets:
                        f.seek(start)
                        group = f.read(size)
                        block, duration = None, None
                        for cid, cdata in _iter_children(group):
                            if cid == BLOCK:
                                block = cdata
                            elif cid == BLOCK_DURATION:
                                duration = _uint(cdata)
                        if block:
                            _handle_block(block, duration, targets, cluster_tc, timecode_scale)
                f.seek(start + size)
            else:
                f.seek(size, 1)

    if progress:
        progress(1.0)
    result = list(tracks.values())
    for t in result:
        t.cues.sort(key=lambda c: c.start_ms)
        for i, c in enumerate(t.cues):
            if c.end_ms <= c.start_ms:
                nxt = t.cues[i + 1].start_ms if i + 1 < len(t.cues) else c.start_ms + 4000
                c.end_ms = min(c.start_ms + 4000, max(c.start_ms + 500, nxt - 50))
    return result


def _peek_group_track(head: bytes):
    try:
        for cid, cdata in _iter_children_partial(head):
            if cid == BLOCK:
                return _vint_from(cdata, 0)[0] if cdata else None
            return None
    except IndexError:
        return None
    return None


def _iter_children_partial(buf: bytes):
    pos = 0
    b = buf[pos]
    length = 1
    mask = 0x80
    while length <= 4 and not (b & mask):
        mask >>= 1
        length += 1
    eid = int.from_bytes(buf[pos:pos + length], "big")
    pos += length
    size, sl = _vint_from(buf, pos)
    pos += sl
    yield eid, buf[pos:pos + size]


def _handle_block(block: bytes, duration, targets, cluster_tc, scale):
    tn, l = _vint_from(block, 0)
    track = targets.get(tn)
    if not track:
        return
    rel = int.from_bytes(block[l:l + 2], "big", signed=True)
    flags = block[l + 2]
    if flags & 0x06:  # лейсинг в субтитрах не встречается на практике
        return
    payload = block[l + 3:]
    text = _decode_payload(track, payload)
    if not text:
        return
    start_ms = (cluster_tc + rel) * scale // 1_000_000
    end_ms = start_ms + (duration * scale // 1_000_000 if duration else 0)
    track.cues.append(Cue(start_ms, end_ms, text))


def pick_english_track(tracks: list[SubTrack]) -> SubTrack | None:
    """Выбирает лучшую английскую текстовую дорожку (с прочитанными репликами)."""
    cands = [t for t in tracks if t.is_text and t.is_english and t.cues]
    if not cands:
        return None
    full = [t for t in cands if not t.forced] or cands
    biggest = max(len(t.cues) for t in full)
    # Похожие по размеру дорожки: предпочитаем обычную, а не SDH
    similar = [t for t in full if len(t.cues) >= biggest * 0.6]
    similar.sort(key=lambda t: (t.is_sdh, not t.default, -len(t.cues)))
    return similar[0]
