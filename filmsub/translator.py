"""Художественный перевод субтитров: подготовка, разбор фильма, перевод пачками, сборка."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .api import ApiError, ChatClient, Usage
from .config import Settings, app_dir
from .glossary import Entry, Glossary, format_hints, parse_glossary_block, parse_meta
from .mkv import MkvError, pick_english_track, read_mkv_subtitles
from .srt import POS_TAG_RE, Cue, read_srt, strip_sdh, write_srt

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".webm"}
SUB_EXT = {".srt"}
LANG_TOKENS = {"en", "eng", "english", "en-us", "en-gb", "sdh", "hi", "cc", "forced", "full"}
RU_TOKENS = {"ru", "rus", "russian"}
MAX_LINE = 42


class Stopped(Exception):
    pass


# --------------------------------------------------------------------------
# Поиск исходных субтитров
# --------------------------------------------------------------------------

_VARIANT_RE = re.compile(r"\.(?:ru|rus|russian)\.v\d+$", re.I)


def is_russian_variant(path: str) -> bool:
    """«Фильм.ru.v2.srt» — второй вариант перевода, не исходник."""
    return bool(_VARIANT_RE.search(os.path.splitext(path)[0]))


def alt_output_path(path: str) -> str:
    """Свободное имя для второго варианта перевода: Фильм.ru.v2.srt, Фильм.ru.v3.srt…"""
    base, _ = _split_name(path)
    n = 2
    while os.path.exists(f"{base}.ru.v{n}.srt"):
        n += 1
    return f"{base}.ru.v{n}.srt"


def _split_name(path: str) -> tuple[str, list[str]]:
    """'Movie.2010.en.sdh.srt' -> ('…/Movie.2010', ['en', 'sdh'])"""
    base, _ = os.path.splitext(path)
    tokens = []
    while True:
        stem, ext = os.path.splitext(base)
        tok = ext[1:].lower()
        if ext and tok in LANG_TOKENS | RU_TOKENS:
            tokens.insert(0, tok)
            base = stem
        else:
            break
    return base, tokens


def output_path_for(path: str) -> str:
    base, _ = _split_name(path)
    return base + ".ru.srt"


def film_year(path: str) -> int | None:
    """Год выхода из имени файла: «Take the Money and Run (1969) 1080p…» → 1969."""
    name = os.path.basename(_split_name(path)[0])
    years = re.findall(r"(?<![0-9])(19[2-9][0-9]|20[0-4][0-9])(?![0-9])", name)
    return int(years[-1]) if years else None


def film_title_hint(path: str) -> str:
    name = os.path.basename(_split_name(path)[0])
    name = re.sub(r"[._]+", " ", name)
    m = re.search(r"\b(19\d\d|20\d\d)\b", name)
    if m:
        name = name[: m.end()]
    name = re.split(r"\b(1080p|720p|2160p|4k|bluray|bdrip|web-?dl|webrip|hdrip|x264|x265|hevc)\b",
                     name, flags=re.I)[0]
    return name.strip(" -([")


def find_sidecar_srt(video_path: str) -> str | None:
    folder = os.path.dirname(video_path) or "."
    base = os.path.splitext(os.path.basename(video_path))[0].lower()
    best = None
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for name in names:
        if not name.lower().endswith(".srt"):
            continue
        stem, tokens = _split_name(os.path.join(folder, name))
        if os.path.basename(stem).lower() != base:
            continue
        if any(t in RU_TOKENS for t in tokens):
            continue
        score = 2 if any(t in {"en", "eng", "english"} for t in tokens) else 1
        if "sdh" in tokens or "hi" in tokens:
            score -= 0.5
        if "forced" in tokens:
            continue
        if not best or score > best[0]:
            best = (score, os.path.join(folder, name))
    return best[1] if best else None


def text_language(cues: list[Cue]) -> str:
    """Грубо определяет язык субтитров по буквам: 'en', 'ru' или 'other'."""
    sample = " ".join(c.text for c in cues[: min(len(cues), 400)])
    latin = sum(1 for ch in sample if "a" <= ch.lower() <= "z")
    cyr = sum(1 for ch in sample if "\u0400" <= ch <= "\u04ff")
    total = latin + cyr
    if total < 20:
        return "other"
    if cyr / total > 0.3:
        return "ru"
    return "en" if latin / total > 0.7 else "other"


NOT_ENGLISH_HINT = ("  Нужны настоящие английские субтитры: положите их рядом с фильмом как "
                    "«<имя фильма>.en.srt» или добавьте сам .mkv — программа поищет английскую дорожку внутри.")


@dataclass
class Source:
    cues: list[Cue]
    description: str
    output: str


def load_source(path: str, settings: Settings, log, progress=None) -> Source | None:
    ext = os.path.splitext(path)[1].lower()
    out = output_path_for(path)

    if ext in SUB_EXT:
        _, tokens = _split_name(path)
        if any(t in RU_TOKENS for t in tokens) or is_russian_variant(path):
            log("  Это уже русские субтитры — пропускаю")
            return None
        cues = read_srt(path)
        lang = text_language(cues)
        if lang == "ru":
            log("  ✖ Внутри этого файла не английский, а русский текст (похоже, машинный перевод) — "
                "пропускаю.")
            log(NOT_ENGLISH_HINT)
            return None
        if lang == "other":
            log("  ⚠ Не похоже на английский текст — проверьте, те ли это субтитры")
        return Source(cues, f"файл {os.path.basename(path)}", out)

    if ext not in VIDEO_EXT:
        log("  Неподдерживаемый формат — пропускаю")
        return None

    side = find_sidecar_srt(path)
    if side:
        side_cues = read_srt(side)
        if text_language(side_cues) == "ru":
            log(f"  Рядом лежит {os.path.basename(side)}, но в нём русский текст, а не английский — "
                "не использую")
        else:
            return Source(side_cues, f"файл {os.path.basename(side)}", out)

    if ext != ".mkv":
        log("  Рядом нет английских .srt, а извлекать субтитры можно только из .mkv — пропускаю")
        return None

    log("  Ищу английские субтитры внутри MKV…")
    try:
        tracks = read_mkv_subtitles(path, wanted=None)
        if settings.skip_if_russian and not settings.overwrite and any(t.is_russian for t in tracks):
            log("  В фильме уже есть русские субтитры — пропускаю "
                "(можно отключить в настройках)")
            return None
        text_en = [t for t in tracks if t.is_text and t.is_english]
        if not text_en:
            if any(t.is_english for t in tracks):
                log("  Английские субтитры в фильме графические (PGS/VobSub) — их нельзя перевести как текст")
            else:
                log("  В фильме нет английских субтитров")
            return None
        log(f"  Читаю дорожки: {', '.join(t.describe() for t in text_en)}")
        tracks = read_mkv_subtitles(path, wanted=lambda t: t.is_english, progress=progress)
    except MkvError as e:
        log(f"  Ошибка чтения MKV: {e}")
        return None
    best = pick_english_track(tracks)
    if not best:
        log("  Не удалось прочитать английские субтитры")
        return None
    if text_language(best.cues) == "ru":
        log(f"  ✖ Дорожка {best.describe()} помечена как английская, но в ней русский текст — пропускаю")
        return None
    return Source(best.cues, f"дорожка {best.describe()} из MKV", out)


# --------------------------------------------------------------------------
# Промпты
# --------------------------------------------------------------------------

ANALYSIS_SYSTEM = """Ты — опытный переводчик кино. Тебе дают английские субтитры фильма или серии.
Составь краткую справку для переводчика, который будет переводить их на русский.

Пиши обычным текстом (не JSON), по-русски, не длиннее 2000 символов. Первая строка — строго
«Фильм: <название и год, если удалось понять>». Дальше:
1. Жанр, эпоха, место действия, общий тон и манера речи.
2. Персонажи: как писать имя по-русски, пол, кто это, как обычно говорит (сленг, грубость, официальность).
3. Кто с кем на «ты», кто на «вы» (по отношениям персонажей).
4. Устойчивые названия, прозвища, термины, повторяющиеся шутки — и как их переводить единообразно.
Если фильм тебе известен и есть устоявшиеся русские варианты имён и названий — укажи их.
Пиши сжато, только то, что поможет переводу."""

ANALYSIS_GLOSSARY_ADDON = """

После справки добавь блок, который начинается отдельной строкой «СЛОВАРЬ:». В нём — выражения из ЭТИХ
субтитров, которые легко перевести неправильно буквально: сленг, диалектизмы, идиомы, профессиональный
жаргон, реалии эпохи и страны. По одному в строке, формат строго:
английское выражение | как переводить по-русски | короткое пояснение
Английское выражение пиши так, как оно стоит в субтитрах, без лишних слов вокруг. Только то, что реально
встречается в тексте, не больше 40 строк. Обычные слова, имена и названия не включай.

В самом начале справки, второй строкой после «Фильм: …», добавь строку строго в формате:
МЕТКИ: страна=<UK, US, AU, IE или другая — где происходит действие и откуда герои>; год=<год выхода фильма, а не год, \
когда происходит действие>"""

NEW_STYLE_RULES = """Сленг, идиомы и реалии:
- Если буквальный перевод звучит странно или бессмысленно — это почти наверняка идиома, сленг, диалект \
или реалия. Определи настоящий смысл по сцене, эпохе и месту действия и переводи его.
- Учитывай среду героев: профессиональный жаргон (чертёжники, военные, полиция, спорт), региональный говор, \
речь эпохи.
- Реалии (деньги, меры, газеты, магазины, передачи) оставляй узнаваемыми: «полкроны», «три пенса», «Гардиан».
- Обращения вроде love, mate, lad, duck переводи по ситуации («милая», «приятель», «сынок»), а не одним словом.
- В поле "hints" запроса — подсказки из словаря к выражениям, которые встречаются в этой пачке. Это частые \
значения: проверяй их по контексту — иногда слово значит буквально то, что сказано.

Род и обращения:
- В субтитрах не видно, кто говорит. Определи говорящего по контексту, справке о фильме и соседним репликам. \
Если уверенности нет — строй фразу без рода, например в настоящем времени или безлично: \
«Не в курсе, что он ушёл» вместо «Я не знал(а), что он ушёл».
- Не меняй «ты»/«вы» между персонажами внутри одной сцены без явной причины (знакомство, ссора, сближение). \
Смотри, как они обращались друг к другу в блоке "before".
- Следи за единообразием: одна и та же реалия в фильме переводится одинаково.
- Если у реплики есть поле "voice" — это пол голоса, определённый по звуку фильма: «м» — мужской, \
«ж» — женский; для реплики-диалога — по строкам («м, ж»; «?» — не определён). Используй его для рода \
говорящего («я пришёл»/«я пришла»). Пометка обычно верна, но может ошибаться на шуме и музыке — \
если она явно противоречит сюжету, доверяй сюжету.

Игра слов:
- Если реплики обыгрывают написание или звучание слова (опечатка, перепутанные буквы, оговорка, рифма, \
каламбур), не переводи слово «правильно» — придумай русский аналог с той же шуткой: например, если \
в записке «gub» вместо «gun», пусть будет «пистолед» вместо «пистолет». Выбранный вариант \
выдерживай одинаково во всех репликах сцены; если шутка уже переведена в блоке "before", \
продолжай тот же вариант.

Примеры из других фильмов — как не надо и как надо:
- "Break a leg!" — не «Сломай ногу!», а «Ни пуха ни пера!»
- "It's not my cup of tea." — не «Это не моя чашка чая», а «Это не по мне».
- "Don't get your knickers in a twist." — не «Не перекручивай трусы», а «Не кипятись».
- "He's a few sandwiches short of a picnic." — не «Ему не хватает бутербродов», а «У него не все дома».
- "Fancy a cuppa?" — не «Хочешь чашку?», а «Чайку?»
- "And Bob's your uncle." — не «И Боб — твой дядя», а «И дело в шляпе».

"""

HINTS_PER_BATCH = 12
TEMPERATURE = {"classic": 0.6, "new": 0.3}

PROFANITY = {
    "asis": "Нецензурную лексику оригинала передавай равноценным русским матом — не смягчай и не "
            "усиливай. Грубость персонажа — часть его характера.",
    "soft": "Грубые выражения передавай экспрессивно и сочно, но без нецензурных слов — "
            "как в хорошем телевизионном дубляже.",
}

TRANSLATE_SYSTEM = """Ты — профессиональный литературный переводчик кино, автор лучших русских субтитров.
Переводишь английские субтитры на русский так, чтобы зритель забыл, что это перевод.

Принципы:
- Переводи смысл, интонацию и характер, а не слова. Русский текст должен звучать естественно, \
как живая речь людей в такой ситуации.
- Идиомы, шутки, игру слов, сленг заменяй русскими эквивалентами того же уровня. Никакого канцелярита \
и кальки («сделать смысл», «это так мило с твоей стороны», «окей» на каждом шагу).
- Сохраняй голос каждого персонажа: грубияна — грубым, интеллигента — книжным, подростка — подростком.
- Следи за родом (пришёл/пришла) и за обращениями на «ты»/«вы» по справке о фильме и контексту.
- {profanity}
- Песни переводи по смыслу, сохраняя настроение; ритм и рифма — по возможности.
- Пометки для слабослышащих в скобках ([стук в дверь], (смеётся)) переводи кратко в тех же скобках. \
Имена говорящих перед двоеточием («JOHN:») — транслитерируй.

Правила субтитров:
- Каждую реплику переводи в своей записи с тем же id. Не объединяй реплики и не переноси смысл \
между id. Если фраза начинается в одной реплике и заканчивается в другой — так же раздели и перевод.
- Коротко: зритель должен успеть прочитать. Не длиннее 2 строк по ~42 символа; можно сокращать без \
потери смысла. Перенос строки — символ \\n.
- Диалог двух говорящих в одной реплике (строки с «-») оформляй так же: «- …\\n- …».
- Сохраняй HTML-теги <i>…</i>, <b>…</b> там, где они есть.
- Блоки "before" и "after" — только контекст, их не переводи.

{extra}Справка о фильме:
{notes}

Ответ — только JSON-объект вида {{"t": [{{"id": 1, "ru": "перевод"}}, ...]}} — по одной записи на каждый id \
из "translate"."""


# --------------------------------------------------------------------------
# Разбор ответа и пост-обработка
# --------------------------------------------------------------------------

def parse_translations(content: str) -> dict[int, str]:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end < 0:
            raise ValueError("нет JSON в ответе")
    data = json.loads(text[start:end + 1])
    items = data
    if isinstance(data, dict):
        for key in ("t", "translations", "translate", "items", "result"):
            if key in data:
                items = data[key]
                break
        else:
            # формат {"12": "текст"}
            if all(str(k).isdigit() for k in data):
                return {int(k): str(v) for k, v in data.items()}
    if not isinstance(items, list):
        raise ValueError("неверная структура JSON")
    out = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        val = it.get("ru", it.get("text", it.get("translation")))
        if isinstance(val, str):
            out[idx] = val
    return out


def _wrap_line(line: str) -> list[str]:
    """Делит длинную строку на две примерно посередине по пробелу."""
    plain = re.sub(r"<[^>]+>", "", line)
    if len(plain) <= MAX_LINE + 3:
        return [line]
    mid = len(line) // 2
    candidates = [m.start() for m in re.finditer(r" ", line)]
    if not candidates:
        return [line]
    # предпочитаем разрыв после знака препинания
    def cost(p):
        penalty = 0 if p > 0 and line[p - 1] in ",.!?;:—…" else 6
        return abs(p - mid) + penalty
    p = min(candidates, key=cost)
    return [line[:p].rstrip(), line[p + 1:].lstrip()]


def finalize(ru: str, en: str) -> str:
    ru = ru.replace("\\n", "\n").replace("\r", "").strip()
    ru = re.sub(r"[ \t]+", " ", ru)
    lines = [l.strip() for l in ru.split("\n") if l.strip()]
    if len(lines) == 1:
        lines = _wrap_line(lines[0])
    elif len(lines) > 2 and not all(l.startswith("-") for l in lines):
        joined = " ".join(lines)
        lines = _wrap_line(joined)
    # курсив: если весь оригинал курсивом, а перевод потерял тег
    en_s = en.strip()
    out = "\n".join(lines)
    if en_s.startswith("<i>") and en_s.endswith("</i>") and "<i>" not in out:
        out = f"<i>{out}</i>"
    return out


# --------------------------------------------------------------------------
# Перевод файла
# --------------------------------------------------------------------------

class FileTranslator:
    def __init__(self, settings: Settings, log, progress=None, stop_flag=None, usage_cb=None,
                 style: str | None = None):
        self.s = settings
        # classic — проверенный промпт без изменений; new — с правилами про идиомы и словарём
        self.style = style or getattr(settings, "prompt_style", "classic")
        self.log = log
        self.progress = progress or (lambda frac, text="": None)
        self.stop_flag = stop_flag or (lambda: False)
        self.usage_cb = usage_cb or (lambda usage: None)
        self.client = ChatClient(settings.provider, settings.api_key, settings.model)
        self.usage = Usage()
        self._lock = threading.Lock()
        self._abort = False

    # --- учёт стоимости ---
    def cost(self, u: Usage | None = None) -> float:
        u = u or self.usage
        if u.cost_usd is not None:
            return u.cost_usd
        fresh = max(0, u.prompt_tokens - u.cached_tokens)
        return (fresh * self.s.price_in + u.cached_tokens * self.s.price_cached
                + u.completion_tokens * self.s.price_out) / 1_000_000

    def _add_usage(self, u: Usage):
        with self._lock:
            self.usage.add(u)
        self.usage_cb(u)

    def _check_stop(self):
        if self._abort or self.stop_flag():
            raise Stopped()

    # --- кэш прогресса (чтобы продолжить после остановки) ---
    def _cache_path(self, cues: list[Cue]) -> str:
        h = hashlib.sha1()
        for c in cues:
            h.update(c.text.encode("utf-8"))
            h.update(b"\x00")
        h.update(f"{self.s.provider}|{self.s.model}|{self.s.profanity}|{self.style}".encode())
        folder = os.path.join(app_dir(), "progress")
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, h.hexdigest() + ".json")

    def _save_cache(self, path, notes, cues, entries=None):
        with self._lock:
            data = {"notes": notes, "ru": {str(i): c.ru for i, c in enumerate(cues) if c.ru},
                    "entries": [[e.en, e.ru, e.note] for e in (entries or [])],
                    "voices": getattr(self, "_voices", None),
                    "timing": getattr(self, "_timing", None),
                    "sync": getattr(self, "_sync", None)}
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)

    # --- шаги ---
    def analyze(self, cues: list[Cue], title_hint: str) -> tuple[str, list[Entry]]:
        lines = []
        prev = None
        for c in cues:
            t = re.sub(r"<[^>]+>", "", c.text).replace("\n", " ").strip()
            if t and t != prev:
                lines.append(t)
            prev = t
        text = "\n".join(lines)
        limit = 150_000
        if len(text) > limit:  # очень длинный фильм — берём равномерную выборку
            step = len(lines) / (limit / 60)
            text = "\n".join(lines[int(i * step)] for i in range(int(len(lines) / step)))
        user = f"Имя файла: {title_hint}\n\nСубтитры:\n{text}"
        system = ANALYSIS_SYSTEM + (ANALYSIS_GLOSSARY_ADDON if self.style == "new" else "")
        content, usage = self.client.chat_retry(
            system, user, max_tokens=8000, temperature=0.3, json_mode=False,
            stop_flag=self.stop_flag, log=self.log)
        self._add_usage(usage)
        notes = re.sub(r"^```\w*\s*|\s*```$", "", content.strip()).strip()
        if usage.truncated:
            # Справка вышла длинной — берём то, что успело прийти, до последней целой строки
            notes = notes.rsplit("\n", 1)[0] if "\n" in notes else notes
            self.log("  (справка получилась длинной — использую её начало)")
        entries: list[Entry] = []
        if self.style == "new":
            notes, entries = parse_glossary_block(notes)
            # В словарь берём только выражения, которые действительно есть в субтитрах
            entries = Glossary().relevant("\n".join(c.text for c in cues), extra=entries, limit=100,
                                          use_base=False)
        return notes[:4000], entries

    def build_system(self, notes: str, cues: list[Cue], film_entries: list[Entry]) -> str:
        """Системный промпт одинаков для всех пачек (кэшируется у провайдера).
        Подсказки словаря идут в каждую пачку отдельно — только для её реплик."""
        extra = ""
        self._glossary = None
        self._film_entries = film_entries or []
        self._region, self._year = parse_meta(notes)
        if getattr(self, "_file_year", None):
            self._year = self._file_year      # год из имени файла надёжнее догадки модели
        if self.style == "new":
            extra = NEW_STYLE_RULES
            if getattr(self.s, "use_glossary", True):
                self._glossary = Glossary.load()
                total = self._glossary.relevant("\n".join(c.text for c in cues), extra=self._film_entries,
                                                region=self._region, year=self._year, limit=10_000)
                where = ", ".join(x for x in (self._region, str(self._year or "")) if x)
                self.log(f"  Подсказок из словаря для фильма: {len(total)}"
                         + (f" (с учётом: {where})" if where else ""))
        return TRANSLATE_SYSTEM.format(
            profanity=PROFANITY.get(self.s.profanity, PROFANITY["asis"]),
            notes=notes or "(нет — ориентируйся по контексту)",
            extra=extra,
        )

    def _prepare(self, cues: list[Cue]) -> list[Cue]:
        for c in cues:
            m = POS_TAG_RE.match(c.text)
            if m:
                c.prefix = c.prefix or m.group(1)
                c.text = c.text[m.end():].lstrip()
        if self.s.strip_sdh:
            for c in cues:
                c.text = strip_sdh(c.text)
        return [c for c in cues if c.text.strip()]

    def translate_sample(self, cues: list[Cue], title_hint: str) -> tuple[list[Cue], str]:
        """Переводит выборку реплик (для режима сравнения): без кэша и без записи в словарь."""
        cues = [Cue(c.start_ms, c.end_ms, c.text, c.prefix) for c in cues]
        cues = self._prepare(cues)
        notes, entries = ("", [])
        if self.s.analyze_film:
            notes, entries = self.analyze(cues, title_hint)
        system = self.build_system(notes, cues, entries)
        try:
            self._run_parallel(system, cues, list(range(len(cues))), None, notes, len(cues))
        except Stopped:
            pass
        return cues, notes

    def _item(self, cues: list[Cue], i: int) -> dict:
        item = {"id": i + 1, "en": cues[i].text}
        voices = getattr(self, "_voices", None)
        if voices and self.style == "new" and i < len(voices) and voices[i]:
            item["voice"] = voices[i]
        return item

    def _prepare_voices(self, video: str, cues: list[Cue]) -> None:
        """Подгоняет тайминги под звук и определяет пол голоса по каждой реплике."""
        from . import voices as V
        from .sync import NoEnglishTrack, SyncError, find_tools, sync_cues
        tools = find_tools()
        if not tools:
            self.log("  Определение голосов пропущено: нет ffmpeg (скачается при первой синхронизации)")
            return
        self.log(f"  Слушаю голоса в фильме {os.path.basename(video)}…")
        self.progress(0.03, "Проверка синхронизации")
        orig = [(c.start_ms, c.end_ms) for c in cues]
        try:
            rep = sync_cues(video, cues, self.log, stop_flag=self.stop_flag)
        except NoEnglishTrack as e:
            self.log(f"  ⚠ Голоса не определяю: {e}. По озвучке пол героев не определить")
            return
        except SyncError as e:
            if self.stop_flag():
                raise Stopped()
            self.log(f"  ⚠ Синхронизация не удалась ({e}) — голоса не определяю, переведу без них")
            return
        # итог сверки пригодится, чтобы не запускать alass второй раз после перевода
        self._sync = [rep.status, rep.summary]
        if rep.status == "rejected":
            self.log("  ⚠ Сверка со звуком не удалась — голоса не определяю, переведу без них")
            return
        try:
            self._voices = V.detect(video, cues, tools[1], log=self.log, stop_flag=self.stop_flag,
                                    progress=lambda f: self.progress(0.03 + 0.04 * f, "Голоса"),
                                    track=rep.track)
            if getattr(self.s, "save_voice_labels", False):
                lab = self._voice_labels_path
                write_srt(lab, V.labels_srt(cues, self._voices), encoding=self.s.encoding)
                self.log(f"  Пометки голосов для проверки: {os.path.basename(lab)}")
        except V.VoiceError as e:
            if self.stop_flag():
                raise Stopped()
            self.log(f"  ⚠ Не удалось определить голоса: {e}")
        except ImportError:
            self.log("  ⚠ Не найден модуль numpy — голоса не определяю")
        finally:
            moved = any((c.start_ms, c.end_ms) != o for c, o in zip(cues, orig))
            if moved and self.s.sync_after:
                # тайминги, подогнанные под звук, пойдут и в русский файл
                self._timing = [[c.start_ms, c.end_ms] for c in cues]
            elif moved:
                for c, (a, b) in zip(cues, orig):
                    c.start_ms, c.end_ms = a, b
                self.log("  (в русском файле останутся исходные тайминги — подгонка нужна была только для голосов)")

    def _translate_batch(self, system: str, cues: list[Cue], idxs: list[int]) -> None:
        """Переводит реплики с номерами idxs (подряд идущие). При сбоях делит пачку пополам."""
        self._check_stop()
        first, last = idxs[0], idxs[-1]
        before = []
        for j in range(max(0, first - 8), first):
            item = {"en": cues[j].text}
            if cues[j].ru:
                item["ru"] = cues[j].ru
            before.append(item)
        after = [{"en": cues[j].text} for j in range(last + 1, min(len(cues), last + 5))]
        payload = {
            "before": before,
            "translate": [self._item(cues, i) for i in idxs],
            "after": after,
        }
        g = getattr(self, "_glossary", None)
        if g is not None:
            hits = g.relevant("\n".join(cues[i].text for i in idxs), extra=self._film_entries,
                              region=self._region, year=self._year, limit=HINTS_PER_BATCH)
            if hits:
                payload["hints"] = format_hints(hits)
        user = json.dumps(payload, ensure_ascii=False)
        max_tokens = min(8000, 800 + 200 * len(idxs))

        got: dict[int, str] = {}
        for attempt in range(2):
            try:
                content, usage = self.client.chat_retry(
                    system, user, max_tokens=max_tokens, temperature=TEMPERATURE.get(self.style, 0.6),
                    stop_flag=self.stop_flag, log=self.log)
                self._add_usage(usage)
                if usage.truncated:
                    # Ответ не влез в лимит — дальше пачка будет разбита на части
                    self.log("  ⚠ Ответ модели обрезан — разобью пачку на части")
                    try:
                        got = parse_translations(content)
                    except ValueError:
                        got = {}
                    break
                got = parse_translations(content)
            except ValueError:
                self.log("  ⚠ Модель ответила не в том формате — повторяю")
                continue
            except ApiError as e:
                if not e.retryable or self.stop_flag():
                    raise
                self.log(f"  ⚠ {e}")
                continue
            missing = [i for i in idxs if not got.get(i + 1, "").strip()]
            if not missing:
                break
            if attempt == 0 and len(missing) > len(idxs) // 3:
                continue  # слишком много пропусков — пробуем ещё раз целиком
            break

        for i in idxs:
            ru = got.get(i + 1, "").strip()
            if ru:
                cues[i].ru = finalize(ru, cues[i].text)

        missing = [i for i in idxs if not cues[i].ru]
        if missing:
            if len(idxs) == 1:
                self.log(f"  ⚠ Реплику {idxs[0] + 1} перевести не удалось — оставлена английская")
                return
            # делим пропущенный хвост на части и пробуем снова
            groups, cur = [], [missing[0]]
            for m in missing[1:]:
                if m == cur[-1] + 1:
                    cur.append(m)
                else:
                    groups.append(cur)
                    cur = [m]
            groups.append(cur)
            for g in groups:
                if len(g) == len(idxs):
                    half = len(g) // 2
                    self._translate_batch(system, cues, g[:half])
                    self._translate_batch(system, cues, g[half:])
                else:
                    self._translate_batch(system, cues, g)

    def translate(self, path: str, existing: str | None = None, video: str | None = None) -> dict:
        """existing — что делать, если перевод уже есть: overwrite (с копией старого), alt (сохранить
        вторым вариантом Фильм.ru.v2.srt) или skip. По умолчанию — из настроек."""
        self.log(f"▶ {os.path.basename(path)}")
        out = output_path_for(path)
        mode = existing or ("overwrite" if self.s.overwrite else getattr(self.s, "existing_mode", "skip"))
        if mode == "ask":
            mode = "skip"
        target = out
        if os.path.exists(out):
            if mode == "skip":
                self.log(f"  Уже есть {os.path.basename(out)} — пропускаю")
                return {"status": "skipped"}
            if mode == "alt":
                target = alt_output_path(path)

        self._voices = self._timing = self._sync = None
        self._file_year = film_year(path)
        out_ru = output_path_for(path)
        self._voice_labels_path = (out_ru[:-len(".ru.srt")] if out_ru.endswith(".ru.srt")
                                   else os.path.splitext(out_ru)[0]) + ".voices.srt"
        src = load_source(path, self.s, self.log,
                          progress=lambda f: self.progress(f * 0.05, "Чтение MKV"))
        if not src:
            return {"status": "skipped"}
        cues = self._prepare(src.cues)
        if not cues:
            self.log("  Субтитры пустые — пропускаю")
            return {"status": "skipped"}
        self.log(f"  Источник: {src.description}, реплик: {len(cues)}")

        cache = self._cache_path(cues)
        notes = ""
        entries: list[Entry] = []
        if os.path.exists(cache):
            try:
                with open(cache, encoding="utf-8") as f:
                    data = json.load(f)
                notes = data.get("notes", "")
                entries = [Entry(en=a, ru=b, note=c) for a, b, c in data.get("entries", [])]
                self._voices = data.get("voices")
                self._sync = data.get("sync")
                self._timing = data.get("timing")
                if self._timing and len(self._timing) == len(cues):
                    for c, (a, b) in zip(cues, self._timing):
                        c.start_ms, c.end_ms = a, b
                for k, v in data.get("ru", {}).items():
                    if int(k) < len(cues):
                        cues[int(k)].ru = v
                done = sum(1 for c in cues if c.ru)
                self.log(f"  Продолжаю прерванный перевод: готово {done} из {len(cues)}")
            except (OSError, ValueError):
                pass

        try:
            if (video and self.style == "new" and getattr(self.s, "detect_voices", True)
                    and not getattr(self, "_voices", None)):
                self._prepare_voices(video, cues)
                self._save_cache(cache, notes, cues, entries)
            if self.s.analyze_film and not notes:
                self.progress(0.05, "Разбор фильма")
                self.log("  Разбираю фильм: персонажи, обращения, термины…")
                notes, entries = self.analyze(cues, film_title_hint(path))
                self._save_cache(cache, notes, cues, entries)
                first_line = notes.splitlines()[0] if notes else ""
                if first_line:
                    self.log(f"  {first_line}")
                if entries and getattr(self.s, "use_glossary", True):
                    g = Glossary.load()
                    added = g.merge_auto(entries, film_title_hint(path))
                    g.save()
                    self.log(f"  Найдено сленга и идиом: {len(entries)}, новых в словаре: {added}")

            system = self.build_system(notes, cues, entries)

            todo = [i for i, c in enumerate(cues) if not c.ru]
            total = len(cues)
            if todo:
                self._run_parallel(system, cues, todo, cache, notes, total, entries)
        except Stopped:
            self.log("  ■ Остановлено. Прогресс сохранён — при следующем запуске продолжу с того же места")
            return {"status": "stopped"}

        failed = sum(1 for c in cues if not c.ru)
        if target == src.output and os.path.exists(target):
            from .sync import backup
            saved = backup(target)
            self.log(f"  Прежний перевод сохранён в копии: {saved}")
        src.output = target
        write_srt(src.output, cues, encoding=self.s.encoding)
        try:
            os.remove(cache)
        except OSError:
            pass
        self.progress(1.0, "Готово")
        msg = f"  ✔ Сохранено: {os.path.basename(src.output)}"
        if failed:
            msg += f" (не переведено реплик: {failed})"
        self.log(msg)
        return {"status": "done", "output": src.output, "failed": failed,
                "sync": getattr(self, "_sync", None)}

    def _run_parallel(self, system, cues, todo, cache, notes, total, entries=None):
        bs = max(5, int(self.s.batch_size))
        # Делим оставшиеся реплики на подряд идущие пачки
        batches, cur = [], []
        for i in todo:
            if cur and (i != cur[-1] + 1 or len(cur) >= bs):
                batches.append(cur)
                cur = []
            cur.append(i)
        if cur:
            batches.append(cur)

        workers = max(1, min(int(self.s.workers), 8))
        # Каждый поток получает свой непрерывный кусок фильма и идёт по нему по порядку,
        # чтобы у каждой пачки был уже переведённый предыдущий контекст.
        n = min(workers, max(1, len(batches) // 3)) if len(batches) > 3 else 1
        chunks = [batches[k * len(batches) // n:(k + 1) * len(batches) // n] for k in range(n)]
        done_lock = threading.Lock()

        def worker(chunk):
            for b in chunk:
                self._check_stop()
                try:
                    self._translate_batch(system, cues, b)
                except Stopped:
                    raise
                except Exception:
                    self._abort = True  # остановить остальные потоки
                    raise
                if cache:
                    self._save_cache(cache, notes, cues, entries)
                with done_lock:
                    done = sum(1 for c in cues if c.ru)
                self.progress(0.08 + 0.92 * done / total, f"{done} / {total}")

        self.log(f"  Перевожу {len(todo)} реплик ({len(batches)} пачек, потоков: {n})…")
        with ThreadPoolExecutor(max_workers=n) as ex:
            futures = [ex.submit(worker, ch) for ch in chunks]
            errors = []
            for f in futures:
                try:
                    f.result()
                except Stopped:
                    errors.append(Stopped())
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
        for e in errors:
            if not isinstance(e, Stopped):
                raise e
        if errors:
            raise Stopped()


def collect_inputs(paths: list[str]) -> list[str]:
    """Разворачивает папки в список видео/срт; если для видео есть .srt рядом, берём видео один раз."""
    result: list[str] = []
    seen = set()
    for p in paths:
        if os.path.isdir(p):
            for root, _, files in os.walk(p):
                for name in sorted(files):
                    full = os.path.join(root, name)
                    ext = os.path.splitext(name)[1].lower()
                    if ext in VIDEO_EXT:
                        result.append(full)
                    elif ext in SUB_EXT:
                        if name.lower().endswith(".voices.srt"):
                            continue          # пометки голосов, сделанные самой программой
                        stem, tokens = _split_name(full)
                        if any(t in RU_TOKENS for t in tokens) or is_russian_variant(full):
                            continue
                        # .srt рядом с видео будет взят через само видео
                        if any(os.path.exists(stem + e) for e in VIDEO_EXT):
                            continue
                        result.append(full)
        else:
            result.append(p)
    out = []
    for p in result:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out
