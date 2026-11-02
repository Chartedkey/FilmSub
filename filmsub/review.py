"""Оценка готового перевода другой моделью (через OpenRouter).

Модель-редактор получает английские и русские реплики частями (с соседними репликами
для контекста) и справку о фильме, и возвращает список конкретных ошибок с исправлениями.
Сам перевод не меняется: исправления, которые пользователь примет, сохраняются отдельным
файлом Фильм.ru.v2.srt (v3, v4…).
"""

from __future__ import annotations

import bisect
import html
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from .api import ApiError, ChatClient, Usage
from .config import Settings, app_dir
from .srt import Cue, ms_to_ts, read_srt, write_srt
from .translator import (FileTranslator, Stopped, _split_name, film_title_hint, load_source,
                         output_path_for)

CHUNK = 50          # реплик на один запрос
BEFORE = 6          # реплик контекста до
AFTER = 3           # и после

TYPES = ["род", "смысл", "игра слов", "единообразие", "пропуск", "грамматика", "стиль"]

REVIEW_SYSTEM = """Ты — опытный редактор русских субтитров к фильмам. Тебе дают английский оригинал и \
готовый русский перевод. Найди в переводе настоящие ошибки и предложи исправления.

Что считать ошибкой (поле "type"):
- "род" — неверный род говорящего или собеседника («я пришла», когда говорит мужчина; «ты устал», \
когда обращаются к женщине). Отмечай, только если по сюжету, справке или соседним репликам ясно, \
кто говорит и с кем; в "problem" напиши, кто это.
- "смысл" — искажён или потерян смысл, неверно понята идиома, сленг или реалия, перевод дословный \
и непонятный зрителю.
- "игра слов" — шутка или каламбур оригинала потеряны, хотя их можно передать по-русски.
- "единообразие" — одно и то же (имя, прозвище, обращение на «ты»/«вы», повторяющаяся шутка) \
переведено по-разному в пределах присланных реплик.
- "пропуск" — часть реплики не переведена или выброшена важная информация.
- "грамматика" — грамматическая или орфографическая ошибка (кроме намеренной — например, \
обыгрываемой опечатки).
- "стиль" — перевод верный, но звучит явно неестественно для русской речи («кальки»). Только \
заметные случаи.

Чего НЕ отмечать:
- вкусовые замены, если перевод верный и звучит естественно;
- сокращения ради краткости субтитров;
- выбор мата или его смягчение, транслитерацию имён (если она единообразна);
- реплики из блоков "before" и "after" — они только для контекста.

Поля ответа для каждой ошибки:
- "id" — номер реплики из блока "check";
- "type" — один из типов выше;
- "severity" — "важно" (зритель поймёт неправильно или заметит ошибку) или "мелочь";
- "problem" — коротко, по-русски, в чём ошибка;
- "fix" — ПОЛНЫЙ исправленный текст реплики (не фрагмент), той же длины примерно, с теми же \
переносами строк ("\\n") и дефисами диалога. Исправляй только ошибку, остальное оставь как было.

Если сомневаешься, ошибка ли это, — лучше не отмечай. Хороший перевод может не иметь ошибок \
вовсе — тогда верни пустой список.

Ответь строго JSON:
{"score": <оценка присланного куска перевода от 1 до 10>, "issues": [{"id": 12, "type": "род", \
"severity": "важно", "problem": "…", "fix": "…"}]}
{notes}"""


class ReviewError(Exception):
    pass


@dataclass
class Issue:
    idx: int                 # номер реплики в русском файле (с нуля)
    type: str
    severity: str
    problem: str
    fix: str
    en: str = ""
    ru: str = ""
    start_ms: int = 0
    accepted: bool = True


@dataclass
class ReviewResult:
    source: str              # английский источник (видео или .srt)
    ru_path: str
    model: str
    issues: list[Issue] = field(default_factory=list)
    score: float | None = None
    checked: int = 0
    cost: float | None = None
    report: str = ""
    data_file: str = ""

    def to_json(self) -> dict:
        d = asdict(self)
        return d


# ------------------------------------------------------------------ сопоставление реплик
def align(en: list[Cue], ru: list[Cue]) -> list[int | None]:
    """Для каждой русской реплики — номер английской. При равном числе — по порядку,
    иначе по времени с учётом общего сдвига (если русский файл синхронизировали отдельно)."""
    if len(en) == len(ru):
        return list(range(len(ru)))
    if not en:
        return [None] * len(ru)
    starts = [c.start_ms for c in en]
    # общий сдвиг: самая частая разница начала реплик (с точностью 0,2 с) в пределах ±2 минут
    votes: dict[int, int] = {}
    for r in ru[:: max(1, len(ru) // 300)]:
        lo = bisect.bisect_left(starts, r.start_ms - 120_000)
        hi = bisect.bisect_right(starts, r.start_ms + 120_000)
        for k in range(lo, hi):
            b = round((r.start_ms - starts[k]) / 200)
            votes[b] = votes.get(b, 0) + 1
    shift = max(votes, key=votes.get) * 200 if votes else 0
    out: list[int | None] = []
    for r in ru:
        a0, a1 = r.start_ms - shift, r.end_ms - shift
        k = bisect.bisect_left(starts, a0)
        best, best_ov = None, -500
        for j in range(max(0, k - 3), min(len(en), k + 3)):
            ov = min(a1, en[j].end_ms) - max(a0, en[j].start_ms)
            if ov > best_ov:
                best, best_ov = j, ov
        out.append(best)
    return out


def variant_choices(source: str) -> list[str]:
    """Русские файлы этого фильма: Фильм.ru.srt, Фильм.ru.v2.srt…"""
    base, _ = _split_name(source)
    out = []
    main = output_path_for(source)
    if os.path.exists(main):
        out.append(main)
    n = 2
    while True:
        p = f"{base}.ru.v{n}.srt"
        if os.path.exists(p):
            out.append(p)
        elif n > 30:
            break
        n += 1
    return out


def estimate(en: list[Cue], ru: list[Cue], info: dict | None) -> tuple[int, int, float | None]:
    """Примерные токены (вход, выход) и стоимость в долларах.

    Подогнано под реальную проверку (962 реплики, Claude Opus 5.5 — $0,76): русский текст
    занимает примерно вдвое больше токенов, чем английский; на каждую часть уходят правила,
    справка о фильме и разметка JSON (~2600 токенов); ответ — в среднем ~70 токенов на
    каждую четвёртую реплику."""
    ce = sum(len(c.text) for c in en)
    cr = sum(len(c.text) for c in ru)
    batches = max(1, (len(ru) + CHUNK - 1) // CHUNK)
    ctx = 1 + (BEFORE * 2 + AFTER) / CHUNK
    tin = int((ce / 4 + cr / 2.2) * ctx * 1.15 + batches * 2600)
    tout = int(len(ru) * 0.25 * 70 + batches * 120)
    cost = None
    if info and info.get("prompt") is not None and info.get("completion") is not None:
        cost = tin * info["prompt"] + tout * info["completion"]
    return tin, tout, cost


def find_english_for(ru_path: str) -> str | None:
    """Для Фильм.ru.srt / Фильм.ru.v2.srt — фильм или английские субтитры рядом с тем же именем."""
    from .translator import VIDEO_EXT, find_sidecar_srt
    base, _ = _split_name(ru_path)
    base = re.sub(r"\.ru\.v\d+$", "", base, flags=re.I)
    base = re.sub(r"\.(ru|rus|russian)$", "", base, flags=re.I)
    for ext in sorted(VIDEO_EXT):
        for cand in (base + ext, base + ext.upper()):
            if os.path.exists(cand):
                return cand
    return find_sidecar_srt(base + ".mkv")


def is_russian_file(path: str) -> bool:
    from .translator import RU_TOKENS, is_russian_variant
    if not path.lower().endswith(".srt"):
        return False
    _, tokens = _split_name(path)
    return any(t in RU_TOKENS for t in tokens) or is_russian_variant(path)


# ------------------------------------------------------------------ сама проверка
def _parse(content: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < 0:
        raise ValueError("нет JSON")
    data = json.loads(text[a:b + 1])
    if not isinstance(data, dict):
        raise ValueError("не объект")
    return data


def run_review(source: str, ru_path: str, s: Settings, log, progress, stop_flag) -> ReviewResult | None:
    key = s.keys.get("OpenRouter", "").strip()
    model = (s.review_model or "").strip()
    if not key:
        raise ReviewError("Нет ключа OpenRouter — укажите его в Настройках → «Проверка перевода»")
    if not model:
        raise ReviewError("Не выбрана модель для проверки — Настройки → «Проверка перевода»")

    log(f"▶ Проверка перевода: {os.path.basename(ru_path)}")
    log(f"  Модель-редактор: {model}")
    src = load_source(source, s, log)
    if not src:
        return None
    tr = FileTranslator(s, log, stop_flag=stop_flag, style="classic", progress=lambda *a: None)
    en = tr._prepare(src.cues)
    ru = read_srt(ru_path)
    if not ru:
        raise ReviewError("Русский файл пустой")
    pairs = align(en, ru)
    matched = sum(1 for p in pairs if p is not None)
    if matched < len(ru) * 0.7:
        raise ReviewError(f"Русские и английские реплики не сопоставились (совпало {matched} из {len(ru)}) — "
                          "похоже, субтитры от разных изданий")

    # Справка о фильме — дешёвой моделью перевода (персонажи, их пол, «ты»/«вы»)
    notes = ""
    if s.api_key and s.model:
        progress(0.02, "Справка о фильме")
        try:
            notes, _ = tr.analyze(en, film_title_hint(source))
        except (ApiError, Stopped) as e:
            if stop_flag():
                return None
            log(f"  ⚠ Справку о фильме составить не удалось ({e}) — проверяю без неё")
    system = REVIEW_SYSTEM.replace("{notes}", f"\nСправка о фильме:\n{notes}" if notes else "")

    client = ChatClient("OpenRouter", key, model, timeout=300)
    usage = Usage()
    lock = threading.Lock()
    issues: list[Issue] = []
    scores: list[tuple[float, int]] = []
    batches = [list(range(i, min(i + CHUNK, len(ru)))) for i in range(0, len(ru), CHUNK)]
    done = [0]

    def text_en(i):
        j = pairs[i]
        return en[j].text if j is not None else ""

    def one(idxs):
        if stop_flag():
            return
        first, last = idxs[0], idxs[-1]
        payload = {
            "before": [{"en": text_en(j), "ru": ru[j].text} for j in range(max(0, first - BEFORE), first)],
            "check": [{"id": i + 1, "time": ms_to_ts(ru[i].start_ms)[:8], "en": text_en(i), "ru": ru[i].text}
                      for i in idxs],
            "after": [{"en": text_en(j), "ru": ru[j].text} for j in range(last + 1, min(len(ru), last + 1 + AFTER))],
        }
        user = json.dumps(payload, ensure_ascii=False)
        data = None
        for attempt in range(3):
            content, u = client.chat_retry(system, user, max_tokens=8000, temperature=0.2,
                                           reasoning=None, stop_flag=stop_flag, log=log)
            with lock:
                usage.add(u)
            try:
                data = _parse(content)
                break
            except (ValueError, json.JSONDecodeError):
                log("  ⚠ Модель ответила не в том формате — повторяю")
        if data is None:
            log(f"  ⚠ Реплики {first + 1}–{last + 1} проверить не удалось")
            return
        got = []
        valid = set(idxs)
        for it in data.get("issues") or []:
            if not isinstance(it, dict):
                continue
            try:
                i = int(it.get("id")) - 1
            except (TypeError, ValueError):
                continue
            fix = str(it.get("fix") or "").replace("\\n", "\n").strip()
            if i not in valid or not fix or fix == ru[i].text.strip():
                continue
            typ = str(it.get("type") or "смысл").strip().lower()
            sev = "мелочь" if "мелоч" in str(it.get("severity", "")).lower() else "важно"
            got.append(Issue(i, typ if typ in TYPES else "смысл", sev, str(it.get("problem") or "").strip(),
                             fix, text_en(i), ru[i].text, ru[i].start_ms, accepted=sev == "важно"))
        sc = data.get("score")
        with lock:
            issues.extend(got)
            if isinstance(sc, (int, float)) and 1 <= sc <= 10:
                scores.append((float(sc), len(idxs)))
            done[0] += len(idxs)
            progress(0.05 + 0.95 * done[0] / len(ru), f"Проверено {done[0]} из {len(ru)}")

    log(f"  Проверяю {len(ru)} реплик ({len(batches)} частей)…")
    workers = max(1, min(3, s.workers))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(one, b) for b in batches]
        errors = []
        for f in futures:
            try:
                f.result()
            except ApiError as e:
                errors.append(e)
    if stop_flag():
        log("  Проверка остановлена")
        return None
    if errors and not issues and not scores:
        raise ReviewError(str(errors[0]))
    for e in errors[:3]:
        log(f"  ⚠ Часть реплик не проверена: {e}")

    issues.sort(key=lambda x: x.idx)
    res = ReviewResult(source=source, ru_path=ru_path, model=model, issues=issues, checked=done[0])
    if scores:
        res.score = round(sum(sc * n for sc, n in scores) / sum(n for _, n in scores), 1)
    res.cost = usage.cost_usd
    save_result(res)
    imp = sum(1 for i in issues if i.severity == "важно")
    log(f"  ✔ Найдено замечаний: {len(issues)} (важных {imp}). Оценка перевода: "
        f"{res.score if res.score is not None else '—'} из 10"
        + (f". Стоимость проверки ${res.cost:.3f}" if res.cost is not None else ""))
    return res


# ------------------------------------------------------------------ результат
def _out_dir() -> str:
    d = os.path.join(app_dir(), "review")
    os.makedirs(d, exist_ok=True)
    return d


def save_result(res: ReviewResult) -> None:
    stem = os.path.splitext(os.path.basename(res.ru_path))[0]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.join(_out_dir(), f"{stem} {stamp}")
    res.data_file = base + ".json"
    res.report = base + ".html"
    with open(res.data_file, "w", encoding="utf-8") as f:
        json.dump(res.to_json(), f, ensure_ascii=False, indent=1)
    write_report(res)


def update_saved(res: ReviewResult) -> None:
    """Сохраняет отметки и правки исправлений в файл проверки — чтобы открыть её потом такой же."""
    if res.data_file:
        try:
            with open(res.data_file, "w", encoding="utf-8") as f:
                json.dump(res.to_json(), f, ensure_ascii=False, indent=1)
        except OSError:
            pass


def load_result(path: str) -> ReviewResult:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    issues = [Issue(**{k: v for k, v in i.items() if k in Issue.__dataclass_fields__}) for i in d.get("issues", [])]
    res = ReviewResult(**{k: v for k, v in d.items() if k in ReviewResult.__dataclass_fields__ and k != "issues"})
    res.issues = issues
    res.data_file = path
    return res


def list_results(limit: int = 50) -> list[tuple[str, str]]:
    """Прошлые проверки, новые сверху: (путь к файлу, подпись для списка)."""
    folder = _out_dir()
    files = sorted((os.path.join(folder, n) for n in os.listdir(folder) if n.endswith(".json")),
                   key=os.path.getmtime, reverse=True)[:limit]
    out = []
    for p in files:
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        stamp = time.strftime("%d.%m.%Y %H:%M", time.localtime(os.path.getmtime(p)))
        score = d.get("score")
        n = len(d.get("issues") or [])
        out.append((p, f"{stamp} — {os.path.basename(d.get('ru_path', ''))} — "
                       f"оценка {score if score is not None else '—'}, замечаний {n} ({d.get('model', '')})"))
    return out


def write_report(res: ReviewResult) -> str:
    def cell(t):
        return html.escape(t or "").replace("\n", "<br>")

    rows = []
    for it in res.issues:
        rows.append(
            f'<tr class="{"imp" if it.severity == "важно" else "minor"}"><td>{it.idx + 1}<br>'
            f'<span class="t">{ms_to_ts(it.start_ms)[:8]}</span></td><td>{cell(it.type)}<br>'
            f'<span class="t">{cell(it.severity)}</span></td><td>{cell(it.en)}</td><td>{cell(it.ru)}</td>'
            f'<td class="fix">{cell(it.fix)}</td><td>{cell(it.problem)}</td></tr>')
    counts = {}
    for it in res.issues:
        counts[it.type] = counts.get(it.type, 0) + 1
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])) or "нет"
    score = f"{res.score} из 10" if res.score is not None else "—"
    cost = f" · стоимость ${res.cost:.3f}" if res.cost is not None else ""
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Проверка перевода — {html.escape(os.path.basename(res.ru_path))}</title>
<style>
:root {{ --bg:#fafaf8; --fg:#1d1d1f; --muted:#6b6b70; --line:#e2e2e0; --imp:#fff1ef; --fix:#eef8f0; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#18181a; --fg:#ececee; --muted:#9a9aa0; --line:#333336;
  --imp:#3a2522; --fix:#1f3324; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.45 system-ui, "Segoe UI", sans-serif; margin:0;
  padding:24px 16px; }}
h1 {{ font-size:20px; margin:0 0 6px; }} .sub {{ color:var(--muted); margin-bottom:18px; }}
table {{ border-collapse:collapse; width:100%; }} td, th {{ border-top:1px solid var(--line); padding:8px;
  vertical-align:top; text-align:left; }} th {{ color:var(--muted); font-weight:600; font-size:13px; }}
tr.imp td:first-child {{ border-left:3px solid #d9534f; }} tr.minor td:first-child {{ border-left:3px solid
  var(--line); }} td.fix {{ background:var(--fix); }} .t {{ color:var(--muted); font-size:12px; }}
.wrap {{ overflow-x:auto; }}
</style></head><body>
<h1>Проверка перевода: {html.escape(os.path.basename(res.ru_path))}</h1>
<div class="sub">Модель-редактор: {html.escape(res.model)} · проверено реплик: {res.checked} ·
оценка: <b>{score}</b>{cost}<br>Замечаний: {len(res.issues)} ({html.escape(summary)})</div>
<div class="wrap"><table><tr><th>№</th><th>Тип</th><th>Оригинал</th><th>Перевод</th><th>Исправление</th>
<th>В чём ошибка</th></tr>
{''.join(rows) or '<tr><td colspan="6">Ошибок не найдено.</td></tr>'}
</table></div></body></html>"""
    with open(res.report, "w", encoding="utf-8") as f:
        f.write(page)
    return res.report


def apply_fixes(res: ReviewResult, encoding: str = "utf-8-sig") -> tuple[str, int]:
    """Сохраняет принятые исправления новым вариантом (Фильм.ru.v2.srt…); исходник не трогается."""
    ru = read_srt(res.ru_path)
    n = 0
    for it in res.issues:
        if it.accepted and 0 <= it.idx < len(ru):
            ru[it.idx].text = it.fix
            n += 1
    # новый вариант кладём рядом с проверенным русским файлом и под его именем
    base, _ = _split_name(res.ru_path)
    base = re.sub(r"\.ru\.v\d+$", "", base, flags=re.I)
    base = re.sub(r"\.(ru|rus|russian)$", "", base, flags=re.I)
    k = 2
    while os.path.exists(f"{base}.ru.v{k}.srt"):
        k += 1
    out = f"{base}.ru.v{k}.srt"
    write_srt(out, ru, encoding=encoding)
    return out, n
