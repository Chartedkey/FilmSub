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


class SyncCancelled(SyncError):
    """Пользователь отменил выбор дорожки; субтитры не изменены."""


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
    from .disc import disc_folder
    for candidate in (stem, os.path.dirname(path)):
        folder = disc_folder(candidate)
        if folder is not None:
            return str(folder)
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


FPS_PAIRS = [(25.0, 23.976), (25.0, 24.0), (24.0, 23.976), (23.976, 25.0), (24.0, 25.0), (23.976, 24.0),
             (29.97, 25.0), (25.0, 29.97), (29.97, 23.976), (23.976, 29.97)]


def fit_speed(old: list[Cue], new: list[Cue], tol: float = 0.6):
    """Ищет прямую «сдвиг = k·время + b» (другая частота кадров), устойчиво к выбросам alass.

    Пробует известные пары частот кадров и оценку по медиане наклонов. Возвращает
    (ratio, b_ms, share, fps_pair) — ratio = 1 + k; share — доля реплик в пределах tol секунд от прямой."""
    if len(old) != len(new) or len(old) < 20:
        return None
    t = [o.start_ms / 1000 for o in old]
    d = [(n.start_ms - o.start_ms) / 1000 for o, n in zip(old, new)]
    cands: list[tuple[float, tuple | None]] = [(sub / film - 1, (film, sub)) for film, sub in FPS_PAIRS]
    # оценка наклона по медиане попарных наклонов на прореженной выборке (Тейл — Сен)
    step = max(1, len(t) // 150)
    idx = list(range(0, len(t), step))
    slopes = sorted((d[j] - d[i]) / (t[j] - t[i]) for a, i in enumerate(idx) for j in idx[a + 1:]
                    if t[j] - t[i] > 60)
    if slopes:
        cands.append((slopes[len(slopes) // 2], None))
    best = None
    for k, pair in cands:
        b = statistics.median(di - k * ti for ti, di in zip(t, d))
        share = sum(1 for ti, di in zip(t, d) if abs(di - (k * ti + b)) <= tol) / len(t)
        if best is None or share > best[2] + 0.02 or (pair and best[3] is None and share >= best[2] - 0.02):
            best = (1 + k, b * 1000, share, pair)
    return best


def apply_speed(cues: list[Cue], ratio: float, b_ms: float) -> list[Cue]:
    out = []
    for c in cues:
        s = int(round(c.start_ms * ratio + b_ms))
        e = int(round(c.end_ms * ratio + b_ms))
        out.append(Cue(max(0, s), max(0, s) + max(100, e - s), c.text, c.prefix))
    return out


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
    stream_id: int | None = None  # PID: сохраняем ту же дорожку в клипах Blu-ray

    def label(self) -> str:
        t = self.lang or "язык не указан"
        return f"{t} «{self.title}»" if self.title else t


def _stream_id(value) -> int | None:
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return None


def audio_tracks(video: str, ffprobe: str) -> list[AudioTrack]:
    cmd = [ffprobe, "-v", "error", "-select_streams", "a", "-show_entries",
           "stream=index,id:stream_tags=language,title:stream_disposition=default", "-of", "json", video]
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
                                 bool((st.get("disposition") or {}).get("default")),
                                 _stream_id(st.get("id"))))
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


def select_audio(tracks: list[AudioTrack], choose_track=None, allow_other_lang=False) -> AudioTrack:
    """Ручной выбор имеет приоритет; без него сохраняется английский режим перевода."""
    if not tracks:
        raise SyncError("В фильме нет звуковых дорожек")
    if choose_track is not None:
        number = choose_track(tracks)
        if number is None:
            raise SyncCancelled("Синхронизация отменена")
        track = next((t for t in tracks if t.n == number), None)
        if track is None:
            raise SyncError("Выбранная звуковая дорожка отсутствует")
        return track
    track = pick_english(tracks)
    if track is not None:
        return track
    if not allow_other_lang:
        raise NoEnglishTrack([t.label() for t in tracks])
    return tracks[0]


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


def _run_ffmpeg(cmd, stop_flag, error: str, cwd=None) -> None:
    """Пишет stderr в файл: подробная ошибка без риска заполнить pipe."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errors,
                                    cwd=cwd, creationflags=flags)
        except OSError as e:
            raise SyncError(f"Не удалось запустить ffmpeg: {e}") from e
        _wait(proc, stop_flag, 1800, "ffmpeg")
        if proc.returncode != 0:
            errors.seek(0, os.SEEK_END)
            errors.seek(max(0, errors.tell() - 8192))
            tail = errors.read().decode("utf-8", "replace").strip().splitlines()[-6:]
            detail = " / ".join(line.strip() for line in tail if line.strip())
            raise SyncError(f"{error} (код {proc.returncode})" + (f": {detail}" if detail else ""))


def _join_audio(pieces: list[str], ffmpeg: str, tmp: str, stop_flag=None) -> str:
    # DVD VOB уже прочитаны единым потоком: повторно объединять звук не нужно.
    if len(pieces) == 1:
        return pieces[0]
    manifest = os.path.join(tmp, "audio.ffconcat")
    with open(manifest, "w", encoding="utf-8") as f:
        f.write("ffconcat version 1.0\n")
        for p in pieces:
            f.write(f"file '{os.path.basename(p)}'\n")
    # Native FLAC совместим и со старым ffmpeg из комплекта alass.
    # Имена файлов ASCII относительно cwd: не передаём Unicode-путь через concat.
    ref = os.path.join(tmp, "audio.flac")
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "concat", "-i", "audio.ffconcat",
           "-map", "0:a:0", "-c:a", "flac", "audio.flac"]
    _run_ffmpeg(cmd, stop_flag, "ffmpeg не смог объединить звук диска", cwd=tmp)
    return ref


def _extract_track(video: str, track: int, ffmpeg: str, dest: str, stop_flag=None, interval=None) -> None:
    """Вынимает одну звуковую дорожку в отдельный файл (моно 16 кГц, FLAC) — для alass."""
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-i", video, "-map", f"0:a:{track}", "-vn", "-sn",
           "-ac", "1", "-ar", "16000", "-c:a", "flac", dest]
    if interval is not None:
        start, end = interval
        cmd[5:5] = ["-copyts"]
        cmd[-1:-1] = ["-ss", str(start), "-t", str(end - start)]
    _run_ffmpeg(cmd, stop_flag, "ffmpeg не смог извлечь звуковую дорожку")
    if not os.path.exists(dest) or os.path.getsize(dest) < 10_000:
        raise SyncError("ffmpeg извлёк пустую или слишком короткую звуковую дорожку")


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


def align(video: str, cues: list[Cue], log, stop_flag=None, allow_other_lang=False, choose_track=None):
    if not os.path.isdir(video):
        if choose_track is None:
            return _align_file(video, cues, log, stop_flag, allow_other_lang)
        return _align_file(video, cues, log, stop_flag, allow_other_lang, choose_track=choose_track)
    from .disc import main_feature, dvd_languages, bluray_languages
    try:
        name, clips = main_feature(video)
    except (OSError, ValueError) as e:
        raise SyncError(f"Не удалось прочитать диск: {e}") from e
    tools = find_tools()
    if not tools:
        raise SyncError("Инструменты синхронизации не установлены")
    _, ffmpeg, ffprobe = tools
    log(f"  Основной фильм: {name}, частей: {len(clips)}")
    languages = dvd_languages(video, name) if name.startswith("DVD ") else []
    # VOB разделены по размеру, в том числе посреди пакетов: читаем как один поток.
    if clips[0][1] is None:
        paths = [os.path.abspath(p) for p, _, _ in clips]
        if any("|" in p for p in paths):
            raise SyncError("Путь DVD не должен содержать символ |")
        clips = [("concat:" + "|".join(paths), None, None)]
    with tempfile.TemporaryDirectory() as tmp:
        pieces = []
        selected = None
        for i, (clip, start, end) in enumerate(clips):
            if stop_flag and stop_flag():
                raise SyncError("Остановлено")
            tracks = audio_tracks(str(clip), ffprobe)
            if not tracks:
                raise SyncError("В части фильма нет звуковых дорожек")
            clip_languages = bluray_languages(video, str(clip)) if start is not None else {}
            for t in tracks:
                if t.lang in UNKNOWN_LANGS and t.stream_id in clip_languages:
                    t.lang = clip_languages[t.stream_id]
            for t, lang in zip(tracks, languages):
                if lang:
                    t.lang = lang
            if selected is None:
                track = select_audio(tracks, choose_track, allow_other_lang)
                selected = track
                log(f"  Звуковая дорожка для сверки: №{track.n + 1}, {track.label()}")
            else:
                # Один выбор для всего фильма, без переключения языка между клипами.
                track = next((t for t in tracks if (
                    t.stream_id == selected.stream_id if selected.stream_id is not None
                    else t.n == selected.n)), None)
                if track is None or (track.lang not in UNKNOWN_LANGS and selected.lang not in UNKNOWN_LANGS
                                     and track.lang != selected.lang):
                    raise SyncError("Выбранная дорожка отсутствует или имеет другой язык в части фильма")
            dest = os.path.join(tmp, f"part_{i}.flac")
            _extract_track(str(clip), track.n, ffmpeg, dest, stop_flag,
                           interval=(start, end) if start is not None else None)
            pieces.append(dest)
        ref = _join_audio(pieces, ffmpeg, tmp, stop_flag)
        new, report = _align_file(ref, cues, log, stop_flag, allow_other_lang=True)
        report.track = selected.n
        return new, report


def _align_file(video: str, cues: list[Cue], log, stop_flag=None, allow_other_lang=False, choose_track=None):
    """Сверяет реплики со звуком. Возвращает (новые реплики, отчёт); реплики не меняет.

    Для перевода по умолчанию используется английская дорожка, для ручной сверки — выбранная.
    alass запускается дважды: единый сдвиг для всего фильма
    и сдвиг по частям; если результаты сильно расходятся, сверке нельзя доверять."""
    tools = find_tools()
    if not tools:
        raise SyncError("Инструменты синхронизации не установлены")
    alass, ffmpeg, ffprobe = tools
    tracks = audio_tracks(video, ffprobe)
    if not tracks:
        raise SyncError("в файле фильма нет звуковых дорожек")
    track = select_audio(tracks, choose_track, allow_other_lang)
    if choose_track is not None or len(tracks) > 1 or track.lang not in EN_LANGS:
        log(f"  Звуковая дорожка для сверки: №{track.n + 1}, {track.label()}")
    env = dict(os.environ, ALASS_FFMPEG_PATH=ffmpeg, ALASS_FFPROBE_PATH=ffprobe)
    with tempfile.TemporaryDirectory() as tmp:
        ref = video
        if len(tracks) > 1 and track.n != 0:
            ref = os.path.join(tmp, "audio.flac")
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
    # Другая частота кадров: сдвиг ровно растёт со временем. alass по частям часто даёт при этом
    # выбросы, и результат выглядит «рваным» — поэтому прямую ищем сами, устойчиво к выбросам.
    if not fps_fix and (rep.status == "rejected" or rep.segments > 1):
        fit = fit_speed(cues, parts)
        if fit and fit[2] >= 0.7 and abs(fit[0] - 1) > 0.0008 and 0.9 < fit[0] < 1.1:
            ratio, b_ms, share, pair = fit
            fixed = apply_speed(cues, ratio, b_ms)
            first = (fixed[0].start_ms - cues[0].start_ms) / 1000
            last = (fixed[-1].start_ms - cues[-1].start_ms) / 1000
            fps = (f"фильм — {pair[0]:g} кадра/с, а субтитры сделаны под {pair[1]:g}; ".replace(".", ",")
                   if pair and abs(pair[1] / pair[0] - ratio) < 0.0005 else "")

            def fmt(x):
                return f"{x:+.1f} с" if abs(x) < 90 else f"{x / 60:+.1f} мин"
            return fixed, SyncReport(
                "changed", f"Исправлена скорость (другая частота кадров): {fps}сдвиг от {fmt(first)} в начале "
                           f"до {fmt(last)} в конце; так совпадают {share:.0%} реплик",
                statistics.median([(f.start_ms - c.start_ms) / 1000 for c, f in zip(cues, fixed)]), 1, track.n)
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
                   allow_other_lang=False, choose_track=None) -> SyncReport:
    old = read_srt(srt_path)
    if not old:
        raise SyncError("Файл субтитров пустой")
    log(f"  Синхронизирую {os.path.basename(srt_path)} по звуку {os.path.basename(video)}…")
    new, report = align(video, old, log, stop_flag, allow_other_lang=allow_other_lang,
                        choose_track=choose_track)
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
