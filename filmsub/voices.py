"""Определение пола говорящего по высоте голоса (без нейросетей).

Звук фильма читается через ffmpeg (моно, 8 кГц). Для каждой реплики считается основная
частота голоса (F0) по автокорреляции коротких кадров: у мужчин обычно 85–155 Гц,
у женщин 165–255 Гц. Реплики-диалоги («- …\\n- …») делятся по времени пропорционально
длине строк, и каждая строка оценивается отдельно.

Граница между «низкими» и «высокими» голосами подбирается под каждый фильм: высоты голосов всех
реплик делятся на две группы, и граница ставится посередине. Возле границы пометка не ставится —
так высокий мужской голос (как у Вуди Аллена) не превращается в женский.

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
UNSURE = 0.10             # ±10 % вокруг границы — не помечаем
# Жёсткие пределы поверх подбора: высокий мужской голос в разговоре доходит до 180 Гц,
# низкий женский опускается до 170 — эту зону лучше оставить без пометки, чем ошибиться.
HARD_MALE_MAX, HARD_FEMALE_MIN = 150.0, 190.0


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


def classify(f0: float | None, voiced: int, male_max: float = MALE_MAX, female_min: float = FEMALE_MIN) -> str:
    if f0 is None or voiced < MIN_VOICED:
        return ""
    if f0 <= male_max:
        return "м"
    if female_min <= f0 <= 300:
        return "ж"
    return ""          # промежуточная зона или детский/крик — не уверены


def calibrate(f0s: list[float]) -> tuple[float, float, str]:
    """Пороги «мужской до…» и «женский от…» для этого фильма и пояснение для журнала.

    Две группы ищутся k-средними по логарифму высоты. Группа считается мужской, только если её центр
    не выше 165 Гц, и женской — если не ниже 180 Гц; иначе такой группы в фильме нет и её пометки
    не ставятся вовсе."""
    import math
    vals = sorted(math.log(f) for f in f0s if f and 70 <= f <= 320)
    if len(vals) < 20:
        return HARD_MALE_MAX, HARD_FEMALE_MIN, "мало данных — стандартные пороги"
    lo, hi = math.log(120), math.log(215)
    for _ in range(50):
        mid = (lo + hi) / 2
        a = [v for v in vals if v <= mid]
        b = [v for v in vals if v > mid]
        if not a or not b:
            break
        nlo, nhi = sum(a) / len(a), sum(b) / len(b)
        if abs(nlo - lo) < 1e-4 and abs(nhi - hi) < 1e-4:
            break
        lo, hi = nlo, nhi
    c_lo, c_hi = math.exp(lo), math.exp(hi)
    has_m, has_f = c_lo <= 165, c_hi >= 180
    if has_m and has_f:
        border = math.sqrt(c_lo * c_hi)
        border = min(185.0, max(150.0, border))
        male_max = min(HARD_MALE_MAX, border * (1 - UNSURE))
        female_min = max(HARD_FEMALE_MIN, border * (1 + UNSURE))
        if c_hi < 205:
            # «высокая» группа подозрительно низкая — в ней, видимо, и высокие мужские голоса
            female_min = max(female_min, c_hi * 1.06)
        note = (f"граница {border:.0f} Гц (низкие голоса ≈{c_lo:.0f}, высокие ≈{c_hi:.0f}); "
                f"от {male_max:.0f} до {female_min:.0f} Гц — без пометки")
    elif has_m:
        male_max, female_min = min(HARD_MALE_MAX, c_lo * 1.25), 10_000.0
        note = f"женских голосов почти нет (голоса ≈{c_lo:.0f}–{c_hi:.0f} Гц) — ставлю только «м»"
    elif has_f:
        male_max, female_min = 0.0, max(HARD_FEMALE_MIN, c_hi * 0.85)
        note = f"мужских голосов почти нет (голоса ≈{c_lo:.0f}–{c_hi:.0f} Гц) — ставлю только «ж»"
    else:
        male_max, female_min = 0.0, 10_000.0
        note = f"голоса не делятся на низкие и высокие (≈{c_lo:.0f}–{c_hi:.0f} Гц) — пометки не ставлю"
    return male_max, female_min, note


def read_audio(video: str, ffmpeg: str, start_ms: int, end_ms: int, stop_flag=None, progress=None,
               track: int | None = None):
    """Читает звук фильма в нужном диапазоне потоком (моно, 8 кГц, int16)."""
    import numpy as np

    ss = max(0.0, start_ms / 1000 - 1)
    dur = (end_ms - start_ms) / 1000 + 2
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-ss", f"{ss:.3f}", "-i", video, "-t", f"{dur:.3f}"]
    if track is not None:
        cmd += ["-map", f"0:a:{track}"]
    cmd += ["-vn", "-sn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
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


def detect(video: str, cues: list[Cue], ffmpeg: str, log=None, progress=None, stop_flag=None,
           track: int | None = None) -> list[str]:
    """Возвращает для каждой реплики пометку: «м», «ж», «м, ж» (диалог) или пусто."""
    if not cues:
        return []
    start = min(c.start_ms for c in cues)
    end = max(c.end_ms for c in cues)
    audio, offset_s = read_audio(video, ffmpeg, start, end, stop_flag,
                                 progress=(lambda f: progress(0.7 * f)) if progress else None, track=track)
    segs_all = segments_for(cues)
    measured: list[list[tuple[float | None, int]]] = []
    for i, segs in enumerate(segs_all):
        parts = []
        for a, b in segs:
            i0 = int((a / 1000 - offset_s) * SR)
            i1 = int((b / 1000 - offset_s) * SR)
            if i1 - i0 < FRAME * 2 or i0 < 0 or i1 > audio.size:
                parts.append((None, 0))
                continue
            parts.append(estimate_f0(audio[i0:i1]))
        measured.append(parts)
        if progress and i % 50 == 0:
            progress(0.7 + 0.3 * i / len(segs_all))
    f0s = [f for parts in measured for f, n in parts if f is not None and n >= MIN_VOICED]
    male_max, female_min, note = calibrate(f0s)
    if log:
        log(f"  Голоса: {note}")
    labels = []
    for parts in measured:
        marks = [classify(f, n, male_max, female_min) for f, n in parts]
        label = ", ".join(p or "?" for p in marks) if len(marks) > 1 else marks[0]
        if label.replace("?", "").replace(",", "").strip() == "":
            label = ""
        labels.append(label)
    if log:
        known = sum(1 for l in labels if l)
        m = sum(1 for l in labels if l == "м")
        f = sum(1 for l in labels if l == "ж")
        log(f"  Голоса: помечено {known} из {len(labels)} реплик (мужской {m}, женский {f}, "
            f"остальные — диалоги; без пометки {len(labels) - known})")
    return labels


def labels_srt(cues: list[Cue], labels: list[str]) -> list[Cue]:
    """Копия реплик с пометками голоса в начале — чтобы проверить определение в плеере."""
    out = []
    for c, l in zip(cues, labels):
        out.append(Cue(c.start_ms, c.end_ms, f"[{l or '—'}] " + c.text, c.prefix))
    return out
