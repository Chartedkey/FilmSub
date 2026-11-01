"""Синхронизация субтитров со звуковой дорожкой фильма.

Работает программа alass (без нейросетей): она находит в звуке участки речи и подбирает
сдвиги таймингов, при которых субтитры лучше всего совпадают с речью — в том числе
нарастающий сдвиг (другая частота кадров) и сдвиги кусками (вырезанные сцены).

FilmSub проверяет результат: показывает найденный сдвиг, не трогает файл при подозрительном
результате и всегда сохраняет копию исходного файла.
"""

from __future__ import annotations

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
    from .translator import _split_name
    stem, _ = _split_name(path)
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


def _run_alass(video: str, cues: list[Cue], stop_flag=None) -> list[Cue]:
    """Прогоняет alass над копией реплик и возвращает реплики с новыми таймингами."""
    tools = find_tools()
    if not tools:
        raise SyncError("Инструменты синхронизации не установлены")
    alass, ffmpeg, ffprobe = tools
    env = dict(os.environ, ALASS_FFMPEG_PATH=ffmpeg, ALASS_FFPROBE_PATH=ffprobe)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory() as tmp:
        # alass получает копию в UTF-8 — исходный файл и его формат он не трогает
        src = os.path.join(tmp, "in.srt")
        out = os.path.join(tmp, "out.srt")
        write_srt(src, [Cue(c.start_ms, c.end_ms, c.text, c.prefix) for c in cues], encoding="utf-8")
        cmd = [alass, video, src, out]
        logfile = open(os.path.join(tmp, "alass.log"), "w+b")  # в файл, чтобы вывод не забил канал
        try:
            proc = subprocess.Popen(cmd, stdout=logfile, stderr=subprocess.STDOUT, env=env,
                                    creationflags=flags)
        except OSError as e:
            logfile.close()
            raise SyncError(f"Не удалось запустить alass: {e}")
        start = time.time()
        while proc.poll() is None:
            if stop_flag and stop_flag():
                proc.kill()
                proc.wait()
                logfile.close()
                raise SyncError("Остановлено")
            if time.time() - start > 1800:
                proc.kill()
                proc.wait()
                logfile.close()
                raise SyncError("alass работает слишком долго — прервано")
            time.sleep(0.3)
        logfile.seek(0)
        output = logfile.read().decode("utf-8", "replace")
        logfile.close()
        if proc.returncode != 0 or not os.path.exists(out):
            tail = output.strip().splitlines()[-3:]
            raise SyncError("alass завершился с ошибкой: " + " / ".join(tail))
        return read_srt(out)


def sync_cues(video: str, cues: list[Cue], log, stop_flag=None) -> SyncReport:
    """Подгоняет тайминги реплик в памяти (без записи файла). Подозрительный результат не применяет."""
    new = _run_alass(video, cues, stop_flag)
    report = analyze_shift(cues, new)
    if report.status == "changed":
        for o, n in zip(cues, new):
            o.start_ms, o.end_ms = n.start_ms, n.end_ms
        log(f"  ✔ Тайминги подогнаны под звук: {report.summary}")
    elif report.status == "unchanged":
        log("  ✔ Субтитры совпадают со звуком")
    else:
        log(f"  ⚠ {report.summary}")
    return report


def sync_subtitles(video: str, srt_path: str, log, encoding="utf-8-sig", stop_flag=None) -> SyncReport:
    old = read_srt(srt_path)
    if not old:
        raise SyncError("Файл субтитров пустой")
    log(f"  Синхронизирую {os.path.basename(srt_path)} по звуку {os.path.basename(video)}…")
    new = _run_alass(video, old, stop_flag)
    report = analyze_shift(old, new)
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
