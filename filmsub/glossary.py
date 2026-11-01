"""Словарь сленга, идиом и реалий.

Два слоя:
- встроенный (filmsub/data/glossary/*.txt) — идёт вместе с программой, только для чтения;
- личный (%APPDATA%\\FilmSub\\glossary.json) — найденное в фильмах автоматически и правки
  пользователя. Личная запись с тем же выражением важнее встроенной.

Выражения ищутся с учётом форм: «kick the bucket» найдёт и «kicked the bucket»,
«lose one's marbles» — «losing his marbles», «give sb the cold shoulder» — «give Mary the cold shoulder».
"""

from __future__ import annotations

import glob
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field

from .config import app_dir

_lock = threading.Lock()


@dataclass
class Entry:
    en: str                 # выражение по-английски (может содержать sb, sth, one's, oneself)
    ru: str                 # как переводить
    note: str = ""          # пояснение
    status: str = "new"     # new — найдено автоматически, ok — проверено, base — встроенное
    films: list = field(default_factory=list)
    added: str = ""
    region: str = ""        # UK, US, AU, IE, SC … пусто — везде
    era: str = ""           # old (до ~1975), modern (после ~1995), пусто — любое время

    @property
    def key(self) -> str:
        return normalize(self.en)


def normalize(text: str) -> str:
    t = text.lower().replace("’", "'").replace("‘", "'").replace("`", "'")
    t = re.sub(r"[^\w' -]+", " ", t)
    t = t.replace("-", " ")
    return re.sub(r"\s+", " ", t).strip()


# --------------------------------------------------------------------------
# Формы слов
# --------------------------------------------------------------------------

_IRREGULAR = """
be is am are was were been being 's 're 'm
have has had having 've 'd
do does did done doing
go goes went gone going
get gets got gotten getting
make makes made making
take takes took taken taking
give gives gave given giving
come comes came coming
keep keeps kept keeping
know knows knew known knowing
tell tells told telling
say says said saying
see sees saw seen seeing
put puts putting
run runs ran running
throw throws threw thrown throwing
catch catches caught catching
bring brings brought bringing
hold holds held holding
lose loses lost losing
break breaks broke broken breaking
bite bites bit bitten biting
blow blows blew blown blowing
sit sits sat sitting
stand stands stood standing
cut cuts cutting
let lets letting
hit hits hitting
fall falls fell fallen falling
feel feels felt feeling
find finds found finding
lay lays laid laying
lie lies lay lain lying
leave leaves left leaving
pay pays paid paying
sell sells sold selling
send sends sent sending
shoot shoots shot shooting
speak speaks spoke spoken speaking
steal steals stole stolen stealing
stick sticks stuck sticking
swing swings swung swinging
teach teaches taught teaching
think thinks thought thinking
wear wears wore worn wearing
win wins won winning
ring rings rang rung ringing
sink sinks sank sunk sinking
drink drinks drank drunk drinking
eat eats ate eaten eating
spill spills spilled spilt spilling
burn burns burned burnt burning
draw draws drew drawn drawing
drive drives drove driven driving
ride rides rode ridden riding
rise rises rose risen rising
shake shakes shook shaken shaking
sing sings sang sung singing
spin spins spun spinning
split splits splitting
spit spits spat spitting
strike strikes struck striking
sweep sweeps swept sweeping
tear tears tore torn tearing
wake wakes woke woken waking
beat beats beaten beating
bend bends bent bending
bet bets betting
bleed bleeds bled bleeding
buy buys bought buying
deal deals dealt dealing
dig digs dug digging
feed feeds fed feeding
fight fights fought fighting
fly flies flew flown flying
forget forgets forgot forgotten forgetting
freeze freezes froze frozen freezing
grow grows grew grown growing
hang hangs hung hanging
hide hides hid hidden hiding
hurt hurts hurting
lead leads led leading
lend lends lent lending
light lights lit lighting
mean means meant meaning
meet meets met meeting
quit quits quitting
read reads reading
seek seeks sought seeking
set sets setting
shut shuts shutting
sleep sleeps slept sleeping
slide slides slid sliding
spend spends spent spending
spread spreads spreading
understand understands understood understanding
weep weeps wept weeping
"""
_FORMS: dict[str, set[str]] = {}
for _line in _IRREGULAR.strip().splitlines():
    _ws = _line.split()
    _set = set(_ws)
    for _w in _ws:
        _FORMS.setdefault(_w, set()).update(_set)

_POSSESSIVE = r"(?:my|your|his|her|its|our|their|one's|ya|yer|me)"
_OBJECT = r"(?:me|you|him|her|it|us|them|ya|yer|'em|em|[\w']+)"
_REFLEXIVE = r"(?:myself|yourself|himself|herself|itself|ourselves|yourselves|themselves|oneself|meself)"
# Слова, которые не склоняем (служебные)
_FIXED = {"a", "an", "the", "of", "to", "in", "on", "at", "by", "for", "with", "and", "or", "up", "out",
          "off", "down", "over", "as", "like", "than", "from", "into", "it", "no", "not", "all", "s"}


def _word_forms(w: str) -> str:
    if w in _FIXED or "'" in w or not w.isalpha():
        return re.escape(w)
    forms = set(_FORMS.get(w, ()))
    forms.add(w)
    if w not in _FORMS:
        # правильные формы: s/es, ed/d, ing, удвоение согласной, y→ies/ied
        forms.update({w + "s", w + "es", w + "ed", w + "d", w + "ing"})
        if w.endswith("e"):
            forms.add(w[:-1] + "ing")
        if w.endswith("y") and len(w) > 2 and w[-2] not in "aeiou":
            forms.update({w[:-1] + "ies", w[:-1] + "ied"})
        if len(w) >= 3 and w[-1] not in "aeiouwxy" and w[-2] in "aeiou" and w[-3] not in "aeiou":
            forms.update({w + w[-1] + "ed", w + w[-1] + "ing"})
    return "(?:" + "|".join(sorted((re.escape(f) for f in forms), key=len, reverse=True)) + ")"


def is_abbreviation(en: str) -> bool:
    """«OR», «DA», «CID», «A&E» — сокращения ищем с учётом регистра, иначе «OR» найдёт союз «or»."""
    letters = re.sub(r"[^A-Za-z]", "", en)
    return bool(letters) and len(letters) <= 5 and letters.isupper() and " " not in en.strip()


def build_pattern(en: str) -> re.Pattern | None:
    if is_abbreviation(en):
        return re.compile(r"(?<![\w'&.-])" + re.escape(en.strip()) + r"(?![\w'&-])")
    words = normalize(en).split()
    if not words:
        return None
    parts = []
    for w in words:
        if w in ("one's", "someone's", "sb's", "your's"):
            parts.append(_POSSESSIVE)
        elif w in ("sb", "someone", "somebody"):
            parts.append(_OBJECT + r"(?:\s[\w']+)?")
        elif w in ("sth", "something"):
            parts.append(r"[\w']+(?:\s[\w']+){0,3}")
        elif w in ("oneself",):
            parts.append(_REFLEXIVE)
        else:
            parts.append(_word_forms(w))
    return re.compile(r"(?<![\w'])" + r"\s".join(parts) + r"(?![\w'])")


_pattern_cache: dict[str, re.Pattern | None] = {}
_anchor_cache: dict[str, str] = {}
_SLOTS = {"sb", "sth", "one's", "oneself", "someone", "something", "somebody", "someone's", "sb's"}


def _anchor(e: Entry) -> str:
    """Кусок самого длинного «правильного» слова — для быстрого отсева перед регулярным выражением."""
    k = e.key
    if k not in _anchor_cache:
        words = [w for w in k.split() if w not in _SLOTS and w not in _FORMS and w not in _FIXED]
        if words:
            w = max(words, key=len)
            _anchor_cache[k] = w[:max(3, len(w) - 2)]
        else:
            _anchor_cache[k] = ""
    return _anchor_cache[k]


def _pattern(e: Entry) -> re.Pattern | None:
    k = e.key
    if k not in _pattern_cache:
        _pattern_cache[k] = build_pattern(e.en)
    return _pattern_cache[k]


# --------------------------------------------------------------------------
# Встроенный словарь
# --------------------------------------------------------------------------

_base_cache: list[Entry] | None = None
_REGIONS = {"UK", "US", "AU", "IE", "SC"}


def base_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "glossary")


def load_base() -> list[Entry]:
    """Файлы формата:  #@ region=UK era=old  — умолчания для следующих строк;
    строки:  выражение | перевод | пояснение [| регион | эпоха]"""
    global _base_cache
    if _base_cache is not None:
        return _base_cache
    out: dict[tuple, Entry] = {}
    for path in sorted(glob.glob(os.path.join(base_dir(), "*.txt"))):
        region, era = "", ""
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#@"):
                    opts = dict(p.split("=", 1) for p in line[2:].split() if "=" in p)
                    region = opts.get("region", "").replace("*", "")
                    era = opts.get("era", "").replace("*", "")
                    continue
                if line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 2 or not parts[0] or not parts[1]:
                    continue
                note = parts[2] if len(parts) > 2 else ""
                if note.upper() in _REGIONS and not (len(parts) > 3 and parts[3]):
                    # страну по ошибке указали в колонке пояснения — переносим в регион
                    parts = parts[:2] + ["", note.upper()] + parts[4:]
                    note = ""
                e = Entry(en=parts[0], ru=parts[1], note=note,
                          status="base",
                          region=(parts[3] if len(parts) > 3 and parts[3] else region).replace("*", ""),
                          era=(parts[4] if len(parts) > 4 and parts[4] else era).replace("*", ""))
                if not e.key:
                    continue
                # Одно выражение может иметь разные значения в разных странах/эпохах —
                # такие варианты храним раздельно; в одной стране и эпохе — объединяем
                vkey = (e.key, e.region, e.era)
                old = out.get(vkey)
                if old is None:
                    out[vkey] = e
                    continue
                if e.ru not in old.ru:
                    old.ru = f"{old.ru}; {e.ru}"
                if e.note and e.note not in old.note:
                    old.note = f"{old.note}; {e.note}" if old.note else e.note
    _base_cache = list(out.values())
    return _base_cache


# --------------------------------------------------------------------------
# Личный словарь
# --------------------------------------------------------------------------

def glossary_path() -> str:
    return os.path.join(app_dir(), "glossary.json")


class Glossary:
    def __init__(self, entries: list[Entry] | None = None):
        self.entries: dict[str, Entry] = {}
        for e in entries or []:
            if e.key:
                self.entries[e.key] = e

    @classmethod
    def load(cls) -> "Glossary":
        try:
            with open(glossary_path(), encoding="utf-8") as f:
                data = json.load(f)
            items = []
            for d in data.get("entries", []):
                try:
                    items.append(Entry(**{k: v for k, v in d.items() if k in Entry.__annotations__}))
                except TypeError:
                    continue
            return cls(items)
        except (OSError, ValueError):
            return cls()

    def save(self) -> None:
        with _lock:
            data = {"version": 2, "entries": [asdict(e) for e in
                                              sorted(self.entries.values(), key=lambda e: e.key)]}
            tmp = glossary_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, glossary_path())

    def merge_auto(self, found: list[Entry], film: str) -> int:
        """Добавляет найденные при разборе выражения. Не трогает проверенные и не дублирует
        то, что уже есть во встроенном словаре."""
        base_keys = {e.key for e in load_base()}
        added = 0
        for e in found:
            k = e.key
            if not k or not e.ru.strip() or k in base_keys:
                continue
            cur = self.entries.get(k)
            if cur:
                if film and film not in cur.films:
                    cur.films = (cur.films + [film])[-20:]
                continue
            e.status = "new"
            e.films = [film] if film else []
            e.added = time.strftime("%Y-%m-%d")
            self.entries[k] = e
            added += 1
        return added

    def upsert(self, en: str, ru: str, note: str = "", status: str = "ok",
               region: str = "", era: str = "") -> None:
        e = Entry(en=en.strip(), ru=ru.strip(), note=note.strip(), status=status,
                  added=time.strftime("%Y-%m-%d"), region=region, era=era)
        old = self.entries.get(e.key)
        if old:
            e.films = old.films
            e.added = old.added
        self.entries[e.key] = e

    def delete(self, key: str) -> None:
        self.entries.pop(key, None)

    # --- отбор ---
    def combined(self) -> list[Entry]:
        """Встроенный словарь + личный. Личная запись заменяет все встроенные варианты выражения."""
        mine = set(self.entries)
        return [e for e in load_base() if e.key not in mine] + list(self.entries.values())

    def relevant(self, text: str, extra: list[Entry] | None = None, limit: int = 60,
                 region: str = "", year: int | None = None, use_base: bool = True) -> list[Entry]:
        pool = self.combined() if use_base else list(self.entries.values())
        known = {e.key for e in pool}
        pool += [e for e in (extra or []) if e.key not in known]
        norm = " " + normalize(text) + " "
        raw = " " + text + " "
        hits = []
        for e in pool:
            if not suits(e, region, year):
                continue
            if is_abbreviation(e.en):
                p = _pattern(e)
                if p and p.search(raw):
                    hits.append(e)
                continue
            # быстрый отсев: самое длинное «обычное» слово выражения должно встречаться в тексте
            anchor = _anchor(e)
            if anchor and anchor not in norm:
                continue
            p = _pattern(e)
            if p and p.search(norm):
                hits.append(e)
        rank = {"ok": 0, "base": 1, "new": 2}
        # проверенные → встроенные → новые; среди равных — более длинные (конкретные) выражения
        hits.sort(key=lambda e: (rank.get(e.status, 3), -len(e.key)))
        # варианты одного выражения (подходящие по стране/эпохе) — склеиваем в одну подсказку
        merged: dict[str, Entry] = {}
        for e in hits:
            m = merged.get(e.key)
            if m is None:
                merged[e.key] = Entry(en=e.en, ru=e.ru, note=e.note, status=e.status,
                                      region=e.region, era=e.era)
            elif e.ru not in m.ru:
                m.ru = f"{m.ru}; {e.ru}"
        return list(merged.values())[:limit]


def suits(e: Entry, region: str, year: int | None) -> bool:
    if e.region and region:
        regions = {r.strip().upper() for r in e.region.split(",")}
        reg = region.upper()
        if reg not in regions and not (reg in ("UK", "GB") and regions & {"UK", "GB", "SC", "IE"}):
            return False
    if e.era and year:
        if e.era == "old" and year >= 1990:
            return False
        if e.era == "modern" and year < 1985:
            return False
    return True


def format_hints(entries: list[Entry]) -> list[str]:
    out = []
    for e in entries:
        s = f"{e.en} — {e.ru}"
        if e.note:
            s += f" ({e.note})"
        out.append(s)
    return out


GLOSSARY_MARK = "СЛОВАРЬ:"
META_RE = re.compile(r"^МЕТКИ:\s*(.*)$", re.MULTILINE)


def parse_glossary_block(text: str) -> tuple[str, list[Entry]]:
    """Отделяет от справки блок «СЛОВАРЬ:» со строками «en | ru | пояснение»."""
    idx = text.find(GLOSSARY_MARK)
    if idx < 0:
        return text.strip(), []
    notes = text[:idx].strip()
    entries = []
    for line in text[idx + len(GLOSSARY_MARK):].splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if "|" not in line:
            continue
        parts = [p.strip().strip("«»\"'") for p in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        if len(parts[0]) > 60 or len(parts[1]) > 120:
            continue
        entries.append(Entry(en=parts[0], ru=parts[1], note=parts[2] if len(parts) > 2 else ""))
    return notes, entries


def parse_meta(notes: str) -> tuple[str, int | None]:
    """Из строки «МЕТКИ: страна=UK; год=1962» достаёт страну и год действия."""
    m = META_RE.search(notes or "")
    if not m:
        return "", None
    opts = {}
    for part in re.split(r"[;,]", m.group(1)):
        if "=" in part:
            k, v = part.split("=", 1)
            opts[k.strip().lower()] = v.strip()
    region = opts.get("страна", "").upper()
    region = {"GB": "UK", "ENGLAND": "UK", "BRITAIN": "UK", "USA": "US", "АНГЛИЯ": "UK",
              "ВЕЛИКОБРИТАНИЯ": "UK", "США": "US"}.get(region, region)
    year = None
    ym = re.search(r"(1[89]\d\d|20\d\d)", opts.get("год", ""))
    if ym:
        year = int(ym.group(1))
    return region, year
