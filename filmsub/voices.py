"""Определение пола говорящего по высоте голоса (без нейросетей).

Звук фильма читается через ffmpeg (моно, 8 кГц). Для каждой реплики считается основная
частота голоса (F0) по автокорреляции коротких кадров: у мужчин обычно 85–155 Гц,
у женщин 165–255 Гц. Реплики-диалоги («- …\\n- …») делятся по времени пропорционально
длине строк, и каждая строка оценивается отдельно.

Результат — пометка «м», «ж» или пусто (не уверены: музыка, шум, шёпот, детский голос).
"""

from __future__ import annotations

import os
import subprocess

from .srt import Cue

SR = 8000                 # частота дискретизации для анализа
FRAME = 320               # 40 мс
HOP = 80                  # 10 мс
F0_MIN, F0_MAX = 70, 400  # Гц
MALE_MAX, FEMALE_MIN = 155.0, 172.0
MIN_VOICED = 8            # минимум «звонких» кадров для решения


class VoiceError(Exception):
    pass


def _dialog_lines(text: str) -> list[str]:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) >= 2 and all(l.lstrip("<i>").startswith("-") for l in lines):
        return lines
    return []


def segments_for(cues: list[Cue]) -> list[list[tuple[int, int]]]:
    """Для каждой реплики — список интервалов (мс): один, или по одному на строку диалога."""
    out = []
    for c in cues:
        lines = _dialog_lines(c.text)
        start, end = c.start_ms, max(c.end_ms, c.start_ms + 300)
        if not lines:
            out.append([(start, end)])
            continue
        total = sum(max(1, len(l)) for l in lines)
        segs, t = [], start
        for l in lines:
            dur = (end - start) * max(1, len(l)) / total
            segs.append((int(t), int(t + dur)))
            t += dur
        out.append(segs)
    return out


def estimate_f0(samples) -> tuple[float | None, int]:
    """Медианная F0 по звонким кадрам и число таких кадров."""
    import numpy as np

    x = np.asarray(samples, dtype=np.float32)
    if x.size < FRAME * 2:
        return None, 0
    x = x - x.mean()
    n_frames = 1 + (x.size - FRAME) // HOP
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = x[idx] * np.hanning(FRAME).astype(np.float32)
    energy = (frames ** 2).mean(axis=1)
    if not np.any(energy > 0):
        return None, 0
    # тихие кадры (паузы) отбрасываем
    thr = max(np.percentile(energy, 40), energy.max() * 0.02)
    loud = frames[energy >= thr]
    if loud.shape[0] == 0:
        return None, 0
    spec = np.fft.rfft(loud, n=FRAME * 2, axis=1)
    ac = np.fft.irfft(np.abs(spec) ** 2, axis=1)[:, :FRAME]
    ac = ac / np.maximum(ac[:, :1], 1e-9)
    lag_min, lag_max = SR // F0_MAX, SR // F0_MIN
    region = ac[:, lag_min:lag_max]
    best = region.argmax(axis=1)
    peak = region[np.arange(region.shape[0]), best]
    voiced = peak > 0.45
    if voiced.sum() == 0:
        return None, 0
    lags = (best[voiced] + lag_min).astype(np.float32)
    # уточнение пика параболой
    f0 = SR / lags
    # защита от удвоения периода: если F0 почти вдвое ниже медианы — считаем ошибкой октавы
    med = float(np.median(f0))
    good = f0[(f0 > med * 0.6) & (f0 < med * 1.6)]
    if good.size == 0:
        return None, 0
    return float(np.median(good)), int(good.size)


def classify(f0: float | None, voiced: int) -> str:
    if f0 is None or voiced < MIN_VOICED:
        return ""
    if f0 <= MALE_MAX:
        return "м"
    if FEMALE_MIN <= f0 <= 300:
        return "ж"
    return ""          # промежуточная зона или детский/крик — не уверены


def read_audio(video: str, ffmpeg: str, start_ms: int, end_ms: int, stop_flag=None, progress=None):
    """Читает звук фильма в нужном диапазоне потоком (моно, 8 кГц, int16)."""
    import numpy as np

    ss = max(0.0, start_ms / 1000 - 1)
    dur = (end_ms - start_ms) / 1000 + 2
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-ss", f"{ss:.3f}", "-i", video, "-t", f"{dur:.3f}",
           "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=flags)
    except OSError as e:
        raise VoiceError(f"Не удалось запустить ffmpeg: {e}")
    chunks = []
    total = int(dur * SR * 2)
    got = 0
    while True:
        if stop_flag and stop_flag():
            proc.kill()
            raise VoiceError("Остановлено")
        b = proc.stdout.read(1 << 20)
        if not b:
            break
        chunks.append(b)
        got += len(b)
        if progress and total:
            progress(min(1.0, got / total))
    proc.wait()
    data = b"".join(chunks)
    if len(data) < SR * 2:
        raise VoiceError("ffmpeg не смог прочитать звук из фильма")
    # храним как int16 (2 байта на отсчёт): двухчасовой фильм — около 115 МБ
    return np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2"), ss


def detect(video: str, cues: list[Cue], ffmpeg: str, log=None, progress=None, stop_flag=None) -> list[str]:
    """Возвращает для каждой реплики пометку: «м», «ж», «м, ж» (диалог) или пусто."""
    if not cues:
        return []
    start = min(c.start_ms for c in cues)
    end = max(c.end_ms for c in cues)
    audio, offset_s = read_audio(video, ffmpeg, start, end, stop_flag,
                                 progress=(lambda f: progress(0.8 * f)) if progress else None)
    labels = []
    segs_all = segments_for(cues)
    known = 0
    for i, segs in enumerate(segs_all):
        parts = []
        for a, b in segs:
            i0 = int((a / 1000 - offset_s) * SR)
            i1 = int((b / 1000 - offset_s) * SR)
            if i1 - i0 < FRAME * 2 or i0 < 0 or i1 > audio.size:
                parts.append("")
                continue
            f0, n = estimate_f0(audio[i0:i1])
            parts.append(classify(f0, n))
        label = ", ".join(p or "?" for p in parts) if len(parts) > 1 else parts[0]
        if label.replace("?", "").replace(",", "").strip() == "":
            label = ""
        labels.append(label)
        known += bool(label)
        if progress and i % 50 == 0:
            progress(0.8 + 0.2 * i / len(segs_all))
    if log:
        m = sum(1 for l in labels if l == "м")
        f = sum(1 for l in labels if l == "ж")
        log(f"  Голоса: определено для {known} из {len(labels)} реплик (мужской {m}, женский {f}, "
            f"остальные — диалоги или неуверенно)")
    return labels
