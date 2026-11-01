"""Режим сравнения: одна и та же выборка реплик переводится проверенным и новым стилем,
результат — HTML-страница «оригинал / проверенный / новый»."""

from __future__ import annotations

import html
import os
import time

from .config import Settings, app_dir
from .srt import Cue, ms_to_ts
from .translator import FileTranslator, film_title_hint, load_source

STYLE_NAMES = {"classic": "Проверенный", "new": "Новый (идиомы + словарь)"}


def pick_sample(cues: list[Cue], n: int) -> list[Cue]:
    """Три непрерывных куска из начала, середины и конца фильма — чтобы у реплик был контекст."""
    if len(cues) <= n:
        return list(cues)
    part = max(10, n // 3)
    out: list[Cue] = []
    for frac in (0.08, 0.45, 0.78):
        start = min(int(len(cues) * frac), len(cues) - part)
        out.extend(cues[start:start + part])
    return out


def run_compare(path: str, settings: Settings, log, progress, stop_flag, usage_cb=None) -> str | None:
    log(f"▶ Сравнение стилей: {os.path.basename(path)}")
    src = load_source(path, settings, log)
    if not src:
        return None
    n = max(30, int(getattr(settings, "compare_size", 150)))
    sample = pick_sample(src.cues, n)
    log(f"  Выборка: {len(sample)} реплик из {len(src.cues)} (начало, середина, конец)")
    title = film_title_hint(path)

    results = {}
    costs = {}
    for k, style in enumerate(("classic", "new")):
        if stop_flag():
            return None
        log(f"  Перевожу стилем «{STYLE_NAMES[style]}»…")
        tr = FileTranslator(settings, log, stop_flag=stop_flag, usage_cb=usage_cb, style=style,
                            progress=lambda f, t="", k=k: progress(0.5 * k + 0.5 * f, f"Стиль {k + 1} из 2 · {t}"))
        cues, _notes = tr.translate_sample(sample, title)
        results[style] = cues
        costs[style] = tr.cost()

    if stop_flag():
        return None
    out = _write_report(path, sample, results, costs)
    log(f"  ✔ Сравнение готово: {out}")
    progress(1.0, "Сравнение готово")
    return out


def _write_report(path, sample, results, costs) -> str:
    a, b = results["classic"], results["new"]
    by_text_a = {(c.start_ms, c.end_ms): c.ru for c in a}
    by_text_b = {(c.start_ms, c.end_ms): c.ru for c in b}
    rows = []
    changed = 0
    prev_end = None
    for c in sample:
        key = (c.start_ms, c.end_ms)
        if key not in by_text_a and key not in by_text_b:
            continue
        ra = by_text_a.get(key) or ""
        rb = by_text_b.get(key) or ""
        diff = ra.strip() != rb.strip()
        changed += diff
        if prev_end is not None and c.start_ms - prev_end > 60_000:
            rows.append('<tr class="gap"><td colspan="5">· · ·</td></tr>')
        prev_end = c.end_ms

        def cell(t):
            return html.escape(t).replace("\n", "<br>")

        rows.append(
            f'<tr class="{"diff" if diff else ""}"><td class="t">{ms_to_ts(c.start_ms)[:8]}</td>'
            f'<td>{cell(c.text)}</td><td>{cell(ra)}</td><td>{cell(rb)}</td>'
            f'<td class="m">{"≠" if diff else ""}</td></tr>')

    title = html.escape(os.path.basename(path))
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Сравнение — {title}</title>
<style>
body{{font:15px/1.45 "Segoe UI",system-ui,sans-serif;margin:0;background:#f6f6f4;color:#1d1d1f}}
header{{padding:20px 24px;background:#fff;border-bottom:1px solid #ddd;position:sticky;top:0}}
h1{{font-size:18px;margin:0 0 6px}} .sub{{color:#666;font-size:13px}}
label{{font-size:13px;margin-left:16px;cursor:pointer}}
table{{border-collapse:collapse;width:100%;background:#fff}}
th,td{{border-bottom:1px solid #eee;padding:8px 10px;vertical-align:top;text-align:left}}
th{{background:#fafafa;font-size:13px;color:#555;position:sticky;top:78px}}
td.t{{color:#999;font-size:12px;white-space:nowrap}} td.m{{color:#c47f00;font-weight:bold;text-align:center}}
tr.diff td:nth-child(4){{background:#fff7e0}} tr.gap td{{text-align:center;color:#bbb;background:#fafafa}}
body.only tr:not(.diff):not(.gap){{display:none}}
</style></head><body>
<header><h1>Сравнение стилей перевода: {title}</h1>
<div class="sub">Реплик: {len(sample)} · различаются: {changed} ·
стоимость: проверенный ${costs['classic']:.3f}, новый ${costs['new']:.3f} · {time.strftime('%d.%m.%Y %H:%M')}
<label><input type="checkbox" onchange="document.body.classList.toggle('only',this.checked)">
показывать только различия</label></div></header>
<table><thead><tr><th>Время</th><th>Оригинал</th><th>{STYLE_NAMES['classic']}</th>
<th>{STYLE_NAMES['new']}</th><th></th></tr></thead><tbody>
{''.join(rows)}
</tbody></table></body></html>"""
    folder = os.path.join(app_dir(), "compare")
    os.makedirs(folder, exist_ok=True)
    name = os.path.splitext(os.path.basename(path))[0][:60]
    out = os.path.join(folder, f"{name} — {time.strftime('%Y-%m-%d %H-%M')}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    return out
