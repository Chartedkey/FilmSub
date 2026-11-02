"""Синхронизация субтитров со звуковой дорожкой фильма.

Работает программа alass (без нейросетей): она находит в звуке участки речи и подбирает
сдвиги таймингов, при которых субтитры лучше всего совпадают с речью — в том числе
нарастающий сдвиг (другая частота кадров) и сдвиги кусками (вырезанные сцены).

FilmSub проверяет результат: показывает найденный сдвиг, не трогает файл при подозрительном
результате и всегда сохраняет копию исходного файла.
"""

from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass

from .config import app_dir
from .srt import Cue, read_srt, write_srt

ALASS_URL = "https://github.com/kaegi/alass/releases/latest/download/alass-windows64.zip"
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".webm")

# Пороги «подозрительного» результата
MAX_SHIFT_S = 300          # сдвиг больше 5 минут — скорее всего ошибка
MAX_SEGMENTS = 20          # слишком много отдельных кусков — алгоритм «поплыл»


class SyncError(Exception):
    pass


class NoEnglishTrack(SyncError):
    """В фильме нет английской звуковой дорожки (например, только русская озвучка)."""

    def __init__(self, langs: list[str]):
        self.langs = langs
        names = ", ".join(langs) or "без пометки языка"
        super().__init__(f"в фильме нет английской звуковой дорожки (есть: {names})")


EN_LANGS = {"eng", "en", "english", "en-us", "en-gb"}
UNKNOWN_LANGS = {"", "und", "unk", "unknown", "mis", "zxx"}


def tools_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or app_dir()
    path = os.path.join(base, "FilmSub", "tools")
    os.makedirs(path, exist_ok=True)
    return path


def _candidates():
    roots = [os.path.join(tools_dir(), "alass-windows64")]
    exe_dir = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    roots.append(os.path.join(exe_dir, "alass-windows64"))
    roots.append(os.path.join(exe_dir, "tools", "alass-windows64"))
    return roots


def find_tools() -> tuple[str, str, str] | None:
    """Возвращает (alass-cli, ffmpeg, ffprobe) или None."""
    for root in _candidates():
        alass = os.path.join(root, "bin", "alass-cli.exe")
        ff = os.path.join(root, "ffmpeg", "bin", "ffmpeg.exe")
        fp = os.path.join(root, "ffmpeg", "bin", "ffprobe.exe")
        if os.path.exists(alass) and os.path.exists(ff) and os.path.exists(fp):
            return alass, ff, fp
    alass = shutil.which("alass-cli") or shutil.which("alass")
    ff, fp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if alass and ff and fp:
        return alass, ff, fp
    return None


def download_tools(log, progress=None) -> None:
    """Скачивает alass для Windows (вместе с ffmpeg) в %LOCALAPPDATA%\\FilmSub\\tools."""
    if sys.platform != "win32":
        raise SyncError("Автоматическая загрузка alass есть только для Windows")
    dest = tools_dir()
    tmp = os.path.join(dest, "alass-download.zip")
    log("  Скачиваю инструменты синхронизации (alass + ffmpeg, ~40 МБ, один раз)…")
    req = urllib.request.Request(ALASS_URL, headers={"User-Agent": "FilmSub"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress and total:
                    progress(got / total, f"Загрузка alass: {got // 1_000_000} из {total // 1_000_000} МБ")
    except OSError as e:
        raise SyncError(f"Не удалось скачать alass: {e}")
    try:
        with zipfile.ZipFile(tmp) as z:
            z.extractall(dest)
    except zipfile.BadZipFile:
        raise SyncError("Скачанный архив alass повреждён — попробуйте ещё раз")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if not find_tools():
        raise SyncError("alass скачан, но не найден в ожидаемом месте")
    log("  Инструменты синхронизации установлены")


def find_video_for(path: str) -> str | None:
    """Для .srt ищет фильм с тем же именем (Фильм.ru.srt → Фильм.mkv)."""
    if path.lower().endswith(VIDEO_EXT):
        return path
    import re
    from .translator import _split_name
    stem, _ = _split_name(path)
    # Фильм.ru.v2.srt → Фильм
    stem = re.sub(r"\.(ru|rus|russian)\.v\d+$", "", stem, flags=re.I)
    for ext in VIDEO_EXT:
        for cand in (stem + ext, stem + ext.upper()):
            if os.path.exists(cand):
                return cand
    return None


@dataclass
class SyncReport:
    status: str            # changed / unchanged / rejected
    summary: str
    median_shift: float = 0.0
    segments: int = 0
    track: int | None = None      # номер звуковой дорожки (среди аудио), по которой сверяли


def analyze_shift(old: list[Cue], new: list[Cue]) -> SyncReport:
    """Описывает, как изменились тайминги, и решает, можно ли доверять результату."""
    if len(old) != len(new) or not old:
        return SyncReport("rejected", "Число реплик после синхронизации изменилось — результат отброшен")
    deltas = [(n.start_ms - o.start_ms) / 1000 for o, n in zip(old, new)]
    med = statistics.median(deltas)
    spread = max(deltas) - min(deltas)
    if max(abs(d) for d in deltas) < 0.15:
        return SyncReport("unchanged", "Субтитры уже совпадают со звуком — файл не менялся", med, 1)

    # Сколько участков с разным сдвигом (скачок больше 0,5 с между соседними репликами)
    segments = 1 + sum(1 for a, b in zip(deltas, deltas[1:]) if abs(a - b) > 0.5)
    if abs(med) > MAX_SHIFT_S or max(abs(d) for d in deltas) > MAX_SHIFT_S * 2:
        return SyncReport("rejected", f"Найден слишком большой сдвиг ({med:+.1f} с) — похоже на ошибку, "
                                      "файл не изменён", med, segments)
    jumbled = sum(1 for a, b in zip(new, new[1:]) if b.start_ms < a.start_ms - 1000)
    if jumbled:
        return SyncReport("rejected", "После синхронизации реплики перепутались местами — результату нельзя "
                                      "доверять, файл не изменён", med, segments)
    if segments > MAX_SEGMENTS or (segments > 3 and segments > len(old) / 4):
        return SyncReport("rejected", f"Сдвиг получился «рваным» ({segments} участков) — "
                                      "результату нельзя доверять, файл не изменён", med, segments)

    # Нарастающий сдвиг: разница линейно растёт со временем (другая частота кадров).
    # Проверяем линейной подгонкой: все реплики должны лечь на прямую с точностью 0,3 с.
    times = [o.start_ms / 1000 for o in old]
    if spread >= 0.3 and len(times) > 2 and times[-1] > times[0]:
        mt, md = statistics.fmean(times), statistics.fmean(deltas)
        var = sum((t - mt) ** 2 for t in times)
        slope = sum((t - mt) * (d - md) for t, d in zip(times, deltas)) / var if var else 0.0
        resid = max(abs(d - (md + slope * (t - mt))) for t, d in zip(times, deltas))
        ratio = 1 + slope
        if resid < 0.3 and 0.9 < ratio < 1.1 and abs(ratio - 1) > 0.001:
            return SyncReport("changed", f"Исправлена скорость (другая частота кадров): сдвиг от "
                                         f"{deltas[0]:+.1f} с в начале до {deltas[-1]:+.1f} с в конце",
                              med, segments)
    if spread < 0.3:
        return SyncReport("changed", f"Постоянный сдвиг {med:+.2f} с", med, 1)
    return SyncReport("changed", f"Сдвиг по частям: {segments} участка(ов), в среднем {med:+.1f} с",
                      med, segments)


def backup(path: str) -> str:
    folder = os.path.join(app_dir(), "backup")
    os.makedirs(folder, exist_ok=True)
    name = os.path.basename(path)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    dest = os.path.join(folder, f"{stamp} {name}")
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(folder, f"{stamp}-{n} {name}")
        n += 1
    shutil.copy2(path, dest)
    return dest


@dataclass
class AudioTrack:
    n: int              # номер среди звуковых дорожек (для -map 0:a:n)
    lang: str
    title: str
    default: bool

    def label(self) -> str:
        t = self.lang or "язык не указан"
        return f"{t} «{self.title}»" if self.title else t


def audio_tracks(video: str, ffprobe: str) -> list[AudioTrack]:
    cmd = [ffprobe, "-v", "error", "-select_streams", "a", "-show_entries",
           "stream=index:stream_tags=language,title:stream_disposition=default", "-of", "json", video]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=120, creationflags=flags).stdout
        streams = json.loads(out.decode("utf-8", "replace") or "{}").get("streams", [])
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        raise SyncError(f"Не удалось прочитать звуковые дорожки фильма: {e}")
    tracks = []
    for n, st in enumerate(streams):
        tags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}
        tracks.append(AudioTrack(n, str(tags.get("language", "")).strip().lower(),
                                 str(tags.get("title", "")).strip(),
                                 bool((st.get("disposition") or {}).get("default"))))
    return tracks


def pick_english(tracks: list[AudioTrack]) -> AudioTrack | None:
    """Английская дорожка (не комментарий); если языки не размечены — первая неразмеченная."""
    def is_comment(t):
        return any(w in t.title.lower() for w in ("comment", "коммент"))
    eng = [t for t in tracks if (t.lang in EN_LANGS or "english" in t.title.lower()) and not is_comment(t)]
    if eng:
        return sorted(eng, key=lambda t: (not t.default, t.n))[0]
    unk = [t for t in tracks if t.lang in UNKNOWN_LANGS and not is_comment(t)
           and not any(w in t.title.lower() for w in ("rus", "рус", "дубл", "mvo", "dvo", "avo"))]
    return unk[0] if unk else None


def _wait(proc, stop_flag, limit, name):
    start = time.time()
    while proc.poll() is None:
        if stop_flag and stop_flag():
            proc.kill()
            proc.wait()
            raise SyncError("Остановлено")
        if time.time() - start > limit:
            proc.kill()
            proc.wait()
            raise SyncError(f"{name} работает слишком долго — прервано")
        time.sleep(0.3)


def _extract_track(video: str, track: int, ffmpeg: str, dest: str, stop_flag=None) -> None:
    """Вынимает одну звуковую дорожку в отдельный файл (моно 16 кГц, FLAC) — для alass."""
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-i", video, "-map", f"0:a:{track}", "-vn", "-sn",
           "-ac", "1", "-ar", "16000", "-c:a", "flac", dest]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    except OSError as e:
        raise SyncError(f"Не удалось запустить ffmpeg: {e}")
    _wait(proc, stop_flag, 1800, "ffmpeg")
    if proc.returncode != 0 or not os.path.exists(dest) or os.path.getsize(dest) < 10_000:
        raise SyncError("ffmpeg не смог извлечь звуковую дорожку")


def _alass_once(alass, ref, cues, tmp, env, stop_flag, extra, tag) -> list[Cue]:
    src = os.path.join(tmp, f"in_{tag}.srt")
    out = os.path.join(tmp, f"out_{tag}.srt")
    write_srt(src, [Cue(c.start_ms, c.end_ms, c.text, c.prefix) for c in cues], encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with open(os.path.join(tmp, f"alass_{tag}.log"), "w+b") as logfile:  # в файл, чтобы не забить канал
        try:
            proc = subprocess.Popen([alass, *extra, ref, src, out], stdout=logfile, stderr=subprocess.STDOUT,
                                    env=env, creationflags=flags)
        except OSError as e:
            raise SyncError(f"Не удалось запустить alass: {e}")
        _wait(proc, stop_flag, 1800, "alass")
        logfile.seek(0)
        output = logfile.read().decode("utf-8", "replace")
    if proc.returncode != 0 or not os.path.exists(out):
        tail = output.strip().splitlines()[-3:]
        raise SyncError("alass завершился с ошибкой: " + " / ".join(tail))
    return read_srt(out, keep_order=True)   # порядок реплик как на входе — сопоставляем по номеру


_NOSPLIT_FLAG: dict[str, str] = {}


def nosplit_flag(alass: str) -> str:
    """Флаг «без разрывов» в этой версии alass: в выпуске 2.0 — --no-split, в новых — --no-splits."""
    if alass not in _NOSPLIT_FLAG:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            out = subprocess.run([alass, "--help"], capture_output=True, timeout=30, creationflags=flags)
            text = (out.stdout + out.stderr).decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            text = ""
        _NOSPLIT_FLAG[alass] = "--no-splits" if "--no-splits" in text else "--no-split"
    return _NOSPLIT_FLAG[alass]


def align(video: str, cues: list[Cue], log, stop_flag=None, allow_other_lang=False):
    """Сверяет реплики со звуком. Возвращает (новые реплики, отчёт); реплики не меняет.

    Сверка идёт по английской дорожке. alass запускается дважды: единый сдвиг для всего фильма
    и сдвиг по частям; если результаты сильно расходятся, сверке нельзя доверять."""
    tools = find_tools()
    if not tools:
        raise SyncError("Инструменты синхронизации не установлены")
    alass, ffmpeg, ffprobe = tools
    tracks = audio_tracks(video, ffprobe)
    if not tracks:
        raise SyncError("в файле фильма нет звуковых дорожек")
    track = pick_english(tracks)
    if track is None:
        if not allow_other_lang:
            raise NoEnglishTrack([t.label() for t in tracks])
        track = tracks[0]
        log(f"  ⚠ Английской дорожки нет — сверяю по дорожке {track.label()}")
    elif len(tracks) > 1 or track.lang not in EN_LANGS:
        log(f"  Звуковая дорожка для сверки: №{track.n + 1}, {track.label()}")
    env = dict(os.environ, ALASS_FFMPEG_PATH=ffmpeg, ALASS_FFPROBE_PATH=ffprobe)
    with tempfile.TemporaryDirectory() as tmp:
        ref = video
        if len(tracks) > 1 and track.n != 0:
            ref = os.path.join(tmp, "audio.mkv")
            _extract_track(video, track.n, ffmpeg, ref, stop_flag)
        whole = _alass_once(alass, ref, cues, tmp, env, stop_flag, [nosplit_flag(alass)], "whole")
        parts = _alass_once(alass, ref, cues, tmp, env, stop_flag, [], "parts")
    rep_whole = analyze_shift(cues, whole)
    rep = analyze_shift(cues, parts)
    rep.track = track.n
    if len(cues) != len(whole) or len(cues) != len(parts):
        return parts, rep
    shifts = [(b.start_ms - a.start_ms) / 1000 for a, b in zip(cues, whole)]
    w_med = statistics.median(shifts)
    # доля реплик, у которых подгонка по частям совпала с единым сдвигом (±0,6 с)
    agree = sum(1 for a, b in zip(whole, parts) if abs(a.start_ms - b.start_ms) <= 600) / len(cues)
    deltas = sorted((b.start_ms - a.start_ms) / 1000 for a, b in zip(cues, parts))
    spread = f"{deltas[len(deltas) // 20]:+.0f}…{deltas[-1 - len(deltas) // 20]:+.0f} с"

    def use_whole(why: str):
        if rep_whole.status == "unchanged":
            r = SyncReport("unchanged", f"Субтитры уже совпадают со звуком ({why})", 0.0, 1, track.n)
        else:
            r = SyncReport("changed", f"Применён единый сдвиг {w_med:+.2f} с — так совпадают {agree:.0%} реплик; "
                                      f"{why}", w_med, 1, track.n)
        return whole, r

    fps_fix = rep.summary.startswith("Исправлена скорость")
    if rep.status == "rejected" and rep_whole.status != "rejected":
        if agree >= 0.6:
            return use_whole(f"подгонка по частям отброшена ({rep.summary.split(' — ')[0].lower()})")
        return parts, SyncReport(
            "rejected", f"Сверка со звуком ненадёжна: единый сдвиг {w_med:+.1f} с подходит только {agree:.0%} "
                        f"реплик, у остальных сдвиг от {spread} — похоже на другое издание фильма (другой "
                        "монтаж). Тайминги не меняю", rep.median_shift, rep.segments, track.n)
    if rep.status == "changed" and rep.segments > 1 and not fps_fix:
        # по частям и целиком должно получаться похоже: большинство реплик — с тем же сдвигом
        near = sum(1 for a, b in zip(whole, parts) if abs(a.start_ms - b.start_ms) <= 2000)
        if near < len(cues) * 0.6:
            alt = f"единый сдвиг {w_med:+.1f} с" if rep_whole.status != "rejected" else "единого сдвига нет"
            return parts, SyncReport(
                "rejected", f"Сверка со звуком ненадёжна: по частям в среднем {rep.median_shift:+.1f} с, "
                            f"а для всего фильма — {alt}. Вероятно, другое издание фильма или звук "
                            "не той дорожки — тайминги не меняю", rep.median_shift, rep.segments, track.n)
    if rep.status == "rejected":
        rep.summary += f" (разброс сдвигов {spread})"
    return parts, rep


def sync_cues(video: str, cues: list[Cue], log, stop_flag=None) -> SyncReport:
    """Подгоняет тайминги реплик в памяти (без записи файла). Подозрительный результат не применяет."""
    new, report = align(video, cues, log, stop_flag)
    if report.status == "changed":
        for o, n in zip(cues, new):
            o.start_ms, o.end_ms = n.start_ms, n.end_ms
        log(f"  ✔ Тайминги подогнаны под звук: {report.summary}")
    elif report.status == "unchanged":
        log("  ✔ Субтитры совпадают со звуком")
    else:
        log(f"  ⚠ {report.summary}")
    return report


def sync_subtitles(video: str, srt_path: str, log, encoding="utf-8-sig", stop_flag=None,
                   allow_other_lang=False) -> SyncReport:
    old = read_srt(srt_path)
    if not old:
        raise SyncError("Файл субтитров пустой")
    log(f"  Синхронизирую {os.path.basename(srt_path)} по звуку {os.path.basename(video)}…")
    new, report = align(video, old, log, stop_flag, allow_other_lang=allow_other_lang)
    if report.status == "changed":
        saved = backup(srt_path)
        for o, n in zip(old, new):
            o.start_ms, o.end_ms = n.start_ms, n.end_ms
        write_srt(srt_path, old, encoding=encoding)
        log(f"  ✔ {report.summary}. Копия исходного файла: {saved}")
    elif report.status == "unchanged":
        log(f"  ✔ {report.summary}")
    else:
        log(f"  ⚠ {report.summary}")
    return report
