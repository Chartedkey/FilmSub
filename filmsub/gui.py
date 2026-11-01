"""Окно программы FilmSub (tkinter)."""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__
from .api import PROVIDERS, ApiError, ChatClient, list_openrouter_models
from .config import Settings, load_settings, save_settings
from .compare import run_compare
from .glossary import Glossary, load_base, normalize
from .sync import SyncError, download_tools, find_tools, find_video_for, sync_subtitles
from .translator import FileTranslator, collect_inputs, output_path_for
from .updater import (RELEASES_PAGE, UpdateError, apply_and_restart, can_self_update, check_latest,
                      download, is_newer)

KEY_URLS = {
    "DeepSeek": "https://platform.deepseek.com/api_keys",
    "OpenRouter": "https://openrouter.ai/settings/keys",
}
ENCODINGS = {"UTF-8 (рекомендуется)": "utf-8-sig", "Windows-1251": "cp1251"}
EXISTING = {"спрашивать": "ask", "перезаписывать (старый — в копию)": "overwrite",
            "сохранять вторым вариантом (.ru.v2.srt)": "alt", "пропускать": "skip"}


class App(tk.Tk):
    def __init__(self, initial_paths: list[str]):
        super().__init__()
        self.title(f"FilmSub {__version__} — художественный перевод субтитров")
        self.geometry("960x780")
        self.minsize(780, 600)
        self.s: Settings = load_settings()
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.items: dict[str, str] = {}   # iid -> путь

        self._style()
        try:
            base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            self.iconbitmap(os.path.join(base, "filmsub.ico"))
        except tk.TclError:
            pass
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        self.tab_main = ttk.Frame(nb, padding=10)
        self.tab_glo = ttk.Frame(nb, padding=10)
        self.tab_set = ttk.Frame(nb, padding=14)
        nb.add(self.tab_main, text="  Перевод  ")
        nb.add(self.tab_glo, text="  Словарь  ")
        nb.add(self.tab_set, text="  Настройки  ")
        self.nb = nb
        self._build_main()
        self._build_glossary()
        self._build_settings()
        nb.bind("<<NotebookTabChanged>>", lambda e: self._on_tab())
        self._load_into_widgets()
        self._update_engine_label()

        self.add_paths(initial_paths)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._poll)
        if self.s.check_updates:
            self.after(1500, lambda: self.check_updates(manual=False))
        if not self.s.api_key:
            self.after(300, lambda: (self.nb.select(self.tab_set),
                                     self.log("Укажите API-ключ на вкладке «Настройки».")))

    # ------------------------------------------------------------------ вид
    def _style(self):
        st = ttk.Style(self)
        if "vista" in st.theme_names():
            st.theme_use("vista")
        default = ("Segoe UI", 10) if sys.platform == "win32" else None
        if default:
            self.option_add("*Font", default)
            st.configure(".", font=default)
        st.configure("Big.TButton", font=(default[0] if default else "TkDefaultFont", 11, "bold"),
                     padding=(16, 6))
        st.configure("Hint.TLabel", foreground="#666")
        st.configure("Cost.TLabel", font=(default[0] if default else "TkDefaultFont", 10, "bold"))

    def _build_main(self):
        f = self.tab_main
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Button(bar, text="Добавить файлы…", command=self.pick_files).pack(side="left")
        ttk.Button(bar, text="Добавить папку…", command=self.pick_folder).pack(side="left", padx=6)
        ttk.Button(bar, text="Убрать выбранные", command=self.remove_selected).pack(side="left")
        ttk.Button(bar, text="Очистить", command=self.clear_list).pack(side="left", padx=6)
        self.engine_lbl = ttk.Label(f, style="Hint.TLabel", cursor="hand2")
        self.engine_lbl.pack(anchor="w", pady=(6, 0))
        self.engine_lbl.bind("<Button-1>", lambda e: self.nb.select(self.tab_set))

        cols = ("file", "folder", "status")
        tv = ttk.Treeview(f, columns=cols, show="headings", height=8, selectmode="extended")
        tv.heading("file", text="Фильм / субтитры")
        tv.heading("folder", text="Папка")
        tv.heading("status", text="Статус")
        tv.column("file", width=330)
        tv.column("folder", width=330)
        tv.column("status", width=170)
        tv.pack(fill="both", expand=True, pady=(8, 8))
        tv.tag_configure("done", foreground="#1a7f37")
        tv.tag_configure("error", foreground="#c62828")
        tv.tag_configure("skip", foreground="#888")
        self.tv = tv

        sel = ttk.Frame(f)
        sel.pack(fill="x", pady=(0, 8))
        ttk.Label(sel, text="Для выбранных:", style="Hint.TLabel").pack(side="left")
        self.sync_btn = ttk.Button(sel, text="Синхронизировать со звуком", command=self.start_sync)
        self.sync_btn.pack(side="left", padx=6)
        self.cmp_btn = ttk.Button(sel, text="Сравнить стили перевода…", command=self.start_compare)
        self.cmp_btn.pack(side="left")

        run = ttk.Frame(f)
        run.pack(fill="x")
        self.start_btn = ttk.Button(run, text="▶  Перевести", style="Big.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(run, text="■  Стоп", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        right = ttk.Frame(run)
        right.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.status_lbl = ttk.Label(right, text="Добавьте фильмы (.mkv) или субтитры (.srt)")
        self.status_lbl.pack(anchor="w")
        self.pb = ttk.Progressbar(right, mode="determinate", maximum=1000)
        self.pb.pack(fill="x", pady=(2, 0))
        self.cost_lbl = ttk.Label(run, text="", style="Cost.TLabel")
        self.cost_lbl.pack(side="right")

        self.logbox = ScrolledText(f, height=10, wrap="word", state="disabled",
                                   font=("Consolas", 9) if sys.platform == "win32" else None)
        self.logbox.pack(fill="both", expand=True, pady=(10, 0))

    # ------------------------------------------------------------- словарь
    def _build_glossary(self):
        f = self.tab_glo
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="Поиск:").pack(side="left")
        self.v_gfind = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.v_gfind, width=28)
        e.pack(side="left", padx=6)
        e.bind("<KeyRelease>", lambda ev: self.refresh_glossary())
        self.v_gnew = tk.BooleanVar()
        ttk.Checkbutton(top, text="только непроверенные", variable=self.v_gnew,
                        command=self.refresh_glossary).pack(side="left", padx=8)
        self.v_gbase = tk.BooleanVar()
        ttk.Checkbutton(top, text="показывать встроенный словарь", variable=self.v_gbase,
                        command=self.refresh_glossary).pack(side="left", padx=8)
        self.g_count = ttk.Label(top, style="Hint.TLabel")
        self.g_count.pack(side="right")

        cols = ("en", "ru", "note", "status", "films")
        tv = ttk.Treeview(f, columns=cols, show="headings", selectmode="extended")
        for c, t, w in (("en", "Выражение", 200), ("ru", "Как переводить", 230),
                        ("note", "Пояснение", 260), ("status", "Статус", 100), ("films", "Фильмов", 70)):
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="center" if c == "films" else "w")
        tv.tag_configure("new", foreground="#b26a00")
        tv.tag_configure("base", foreground="#555")
        tv.pack(fill="both", expand=True, pady=8)
        tv.bind("<Double-1>", lambda e: self.edit_entry())
        self.gtv = tv

        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Button(bar, text="Добавить…", command=lambda: self.edit_entry(new=True)).pack(side="left")
        ttk.Button(bar, text="Изменить…", command=self.edit_entry).pack(side="left", padx=6)
        ttk.Button(bar, text="Отметить проверенными", command=self.approve_entries).pack(side="left")
        ttk.Button(bar, text="Удалить", command=self.delete_entries).pack(side="left", padx=6)
        ttk.Label(f, style="Hint.TLabel", wraplength=880, justify="left",
                  text="Словарь пополняется сам, когда фильм переводится новым промптом: найденные сленг и "
                       "идиомы помечаются как «новое». Исправьте или удалите неудачные — изменения "
                       "учтутся во всех следующих фильмах. В запрос попадают только выражения, которые "
                       "есть в субтитрах переводимого фильма.").pack(anchor="w", pady=(8, 0))

    def _on_tab(self):
        if self.nb.select() == str(self.tab_glo):
            self.refresh_glossary()

    def refresh_glossary(self):
        g = Glossary.load()
        q = normalize(self.v_gfind.get()) if self.v_gfind.get().strip() else ""
        self.gtv.delete(*self.gtv.get_children())
        self.g_keys = {}
        shown = 0
        rows = sorted(g.entries.values(), key=lambda e: (e.status == "ok", e.key))
        base = load_base()
        if self.v_gbase.get() and not self.v_gnew.get():
            rows += sorted((e for e in base if e.key not in g.entries), key=lambda e: e.key)
        status_name = {"ok": "проверено", "new": "новое", "base": "встроенное"}
        for e in rows:
            if self.v_gnew.get() and e.status != "new":
                continue
            if q and q not in e.key and q not in e.ru.lower():
                continue
            where = " ".join(x for x in (e.region, {"old": "до 70-х", "modern": "современное"}.get(e.era, ""))
                             if x)
            note = f"{e.note} [{where}]" if where else e.note
            iid = self.gtv.insert("", "end",
                                  values=(e.en, e.ru, note, status_name.get(e.status, e.status),
                                          len(e.films) if e.status != "base" else ""),
                                  tags=("new",) if e.status == "new" else (("base",) if e.status == "base" else ()))
            self.g_keys[iid] = (e.key, e.status == "base")
            shown += 1
            if shown >= 3000:
                break
        new = sum(1 for e in g.entries.values() if e.status == "new")
        self.g_count.configure(text=f"Личный: {len(g.entries)} · непроверенных: {new} · встроенный: {len(base)}"
                                    f" · показано: {shown}")

    def edit_entry(self, new=False):
        g = Glossary.load()
        cur = None
        if not new:
            sel = self.gtv.selection()
            if not sel:
                return
            key, is_base = self.g_keys.get(sel[0], ("", False))
            cur = g.entries.get(key)
            if cur is None and is_base:
                # правка встроенной записи создаёт личную, которая её заменяет
                cur = next((e for e in load_base() if e.key == key), None)
            if not cur:
                return
        dlg = tk.Toplevel(self)
        dlg.title("Новое выражение" if new else "Изменить выражение")
        dlg.transient(self)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill="both")
        vars_ = []
        for r, (lbl, val) in enumerate((("Выражение (англ.):", cur.en if cur else ""),
                                        ("Как переводить:", cur.ru if cur else ""),
                                        ("Пояснение:", cur.note if cur else ""))):
            ttk.Label(frm, text=lbl).grid(row=r, column=0, sticky="w", pady=4)
            v = tk.StringVar(value=val)
            ttk.Entry(frm, textvariable=v, width=52).grid(row=r, column=1, pady=4, padx=(8, 0))
            vars_.append(v)

        def save():
            en, ru, note = (v.get().strip() for v in vars_)
            if not en or not ru:
                messagebox.showwarning("FilmSub", "Заполните выражение и перевод.", parent=dlg)
                return
            g2 = Glossary.load()
            if cur and normalize(en) != cur.key:
                g2.delete(cur.key)
            g2.upsert(en, ru, note, status="ok")
            g2.save()
            dlg.destroy()
            self.refresh_glossary()

        b = ttk.Frame(frm)
        b.grid(row=3, column=1, sticky="e", pady=(10, 0))
        ttk.Button(b, text="Сохранить", command=save).pack(side="left")
        ttk.Button(b, text="Отмена", command=dlg.destroy).pack(side="left", padx=(6, 0))
        dlg.grab_set()

    def approve_entries(self):
        sel = self.gtv.selection()
        if not sel:
            return
        g = Glossary.load()
        for iid in sel:
            k, is_base = self.g_keys.get(iid, ("", False))
            if k in g.entries:
                g.entries[k].status = "ok"
        g.save()
        self.refresh_glossary()

    def delete_entries(self):
        sel = self.gtv.selection()
        if any(self.g_keys.get(i, ("", False))[1] for i in sel):
            messagebox.showinfo("FilmSub", "Встроенные выражения удалить нельзя, но можно исправить "
                                           "(двойной щелчок) — ваш вариант заменит встроенный.")
            sel = [i for i in sel if not self.g_keys.get(i, ("", False))[1]]
        if not sel or not messagebox.askyesno("FilmSub", f"Удалить выражений: {len(sel)}?"):
            return
        g = Glossary.load()
        for iid in sel:
            g.delete(self.g_keys.get(iid, ("", False))[0])
        g.save()
        self.refresh_glossary()

    def _row(self, parent, r, label, widget, hint=None):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 10))
        widget.grid(row=r, column=1, sticky="we", pady=4)
        if hint:
            ttk.Label(parent, text=hint, style="Hint.TLabel").grid(row=r, column=2, sticky="w", padx=8)

    def _build_settings(self):
        f = self.tab_set
        f.columnconfigure(1, weight=1)

        # --- Сервис ---
        g = ttk.LabelFrame(f, text="Сервис перевода", padding=10)
        g.grid(row=0, column=0, sticky="we")
        g.columnconfigure(1, weight=1)
        self.v_provider = tk.StringVar()
        pf = ttk.Frame(g)
        for name in PROVIDERS:
            ttk.Radiobutton(pf, text=name, value=name, variable=self.v_provider,
                            command=self._provider_changed).pack(side="left", padx=(0, 16))
        self._row(g, 0, "Сервис:", pf)

        kf = ttk.Frame(g)
        self.v_key = tk.StringVar()
        self.key_entry = ttk.Entry(kf, textvariable=self.v_key, show="•", width=50)
        self.key_entry.pack(side="left", fill="x", expand=True)
        self.v_show = tk.BooleanVar()
        ttk.Checkbutton(kf, text="показать", variable=self.v_show,
                        command=lambda: self.key_entry.configure(show="" if self.v_show.get() else "•")
                        ).pack(side="left", padx=6)
        self._row(g, 1, "API-ключ:", kf)
        self.key_link = ttk.Label(g, style="Hint.TLabel", cursor="hand2")
        self.key_link.grid(row=2, column=1, sticky="w")
        self.key_link.bind("<Button-1>", lambda e: webbrowser.open(KEY_URLS[self.v_provider.get()]))

        mf = ttk.Frame(g)
        self.v_model = tk.StringVar()
        self.model_cb = ttk.Combobox(mf, textvariable=self.v_model, width=48)
        self.model_cb.pack(side="left", fill="x", expand=True)
        self.all_models: list[str] = []
        self.model_cb.bind("<KeyRelease>", self._filter_models)
        self.models_btn = ttk.Button(mf, text="Список моделей", command=self.load_models)
        self.models_btn.pack(side="left", padx=6)
        self._row(g, 3, "Модель:", mf)

        tf = ttk.Frame(g)
        ttk.Button(tf, text="Проверить подключение", command=self.test_connection).pack(side="left")
        self.test_lbl = ttk.Label(tf, text="")
        self.test_lbl.pack(side="left", padx=10)
        tf.grid(row=4, column=1, sticky="w", pady=(6, 0))

        # --- Цены ---
        self.price_frame = ttk.LabelFrame(f, text="Цены DeepSeek для подсчёта стоимости ($ за 1 млн токенов)",
                                          padding=10)
        self.price_frame.grid(row=1, column=0, sticky="we", pady=(10, 0))
        self.v_pin, self.v_pcache, self.v_pout = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for i, (lbl, var) in enumerate([("вход:", self.v_pin), ("вход из кэша:", self.v_pcache),
                                        ("выход:", self.v_pout)]):
            ttk.Label(self.price_frame, text=lbl).grid(row=0, column=i * 2, padx=(0 if i == 0 else 16, 4))
            ttk.Entry(self.price_frame, textvariable=var, width=9).grid(row=0, column=i * 2 + 1)
        ttk.Label(self.price_frame, style="Hint.TLabel",
                  text="Для OpenRouter точную сумму присылает сам сервис.").grid(
            row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))

        # --- Стиль ---
        g2 = ttk.LabelFrame(f, text="Стиль перевода", padding=10)
        g2.grid(row=2, column=0, sticky="we", pady=(10, 0))
        self.v_prof = tk.StringVar()
        pr = ttk.Frame(g2)
        ttk.Radiobutton(pr, text="передавать как в оригинале", value="asis",
                        variable=self.v_prof).pack(side="left")
        ttk.Radiobutton(pr, text="смягчать (без нецензурных слов)", value="soft",
                        variable=self.v_prof).pack(side="left", padx=16)
        ttk.Label(g2, text="Мат и грубости:").grid(row=0, column=0, sticky="w", padx=(0, 10))
        pr.grid(row=0, column=1, sticky="w")
        self.v_analyze = tk.BooleanVar()
        ttk.Checkbutton(g2, text="Сначала разобрать фильм: персонажи, их пол, кто с кем на «ты»/«вы», "
                                 "термины (точнее, +1–2 цента)",
                        variable=self.v_analyze).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.v_sdh = tk.BooleanVar()
        ttk.Checkbutton(g2, text="Убирать пометки для слабослышащих: [звуки], (смех), «ДЖОН:»",
                        variable=self.v_sdh).grid(row=2, column=0, columnspan=2, sticky="w")
        self.v_pstyle = tk.StringVar()
        ps = ttk.Frame(g2)
        ttk.Radiobutton(ps, text="проверенный", value="classic",
                        variable=self.v_pstyle).pack(side="left")
        ttk.Radiobutton(ps, text="новый: идиомы, сленг + словарь", value="new",
                        variable=self.v_pstyle).pack(side="left", padx=16)
        ttk.Label(g2, text="Промпт:").grid(row=3, column=0, sticky="w", padx=(0, 10), pady=(8, 0))
        ps.grid(row=3, column=1, sticky="w", pady=(8, 0))
        self.v_glossary = tk.BooleanVar()
        ttk.Checkbutton(g2, text="Пополнять и использовать словарь (только для нового промпта)",
                        variable=self.v_glossary).grid(row=4, column=0, columnspan=2, sticky="w")
        cs = ttk.Frame(g2)
        ttk.Label(cs, text="Реплик для сравнения стилей:").pack(side="left")
        self.v_cmpsize = tk.StringVar()
        ttk.Spinbox(cs, from_=30, to=600, increment=30, textvariable=self.v_cmpsize, width=6).pack(
            side="left", padx=6)
        cs.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # --- Файлы и скорость ---
        g3 = ttk.LabelFrame(f, text="Файлы и скорость", padding=10)
        g3.grid(row=3, column=0, sticky="we", pady=(10, 0))
        ef = ttk.Frame(g3)
        ttk.Label(ef, text="Если перевод уже есть:").pack(side="left")
        self.v_existing = tk.StringVar()
        ttk.Combobox(ef, textvariable=self.v_existing, values=list(EXISTING), state="readonly",
                     width=38).pack(side="left", padx=6)
        ef.grid(row=0, column=0, columnspan=4, sticky="w")
        self.v_skipru = tk.BooleanVar()
        ttk.Checkbutton(g3, text="Пропускать фильмы, в которых уже есть русские субтитры",
                        variable=self.v_skipru).grid(row=1, column=0, columnspan=4, sticky="w")
        ttk.Label(g3, text="Кодировка .srt:").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.v_enc = tk.StringVar()
        ttk.Combobox(g3, textvariable=self.v_enc, values=list(ENCODINGS), state="readonly",
                     width=22).grid(row=2, column=1, sticky="w", pady=(8, 0), padx=6)
        ttk.Label(g3, text="Потоков:").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.v_workers = tk.StringVar()
        ttk.Spinbox(g3, from_=1, to=8, textvariable=self.v_workers, width=5).grid(
            row=3, column=1, sticky="w", pady=(8, 0), padx=6)
        ttk.Label(g3, text="Реплик в запросе:").grid(row=3, column=2, sticky="w", pady=(8, 0), padx=(16, 0))
        self.v_batch = tk.StringVar()
        ttk.Spinbox(g3, from_=10, to=100, increment=5, textvariable=self.v_batch, width=5).grid(
            row=3, column=3, sticky="w", pady=(8, 0), padx=6)
        self.v_syncafter = tk.BooleanVar()
        ttk.Checkbutton(g3, text="Синхронизировать со звуком после перевода (нужен сам фильм рядом)",
                        variable=self.v_syncafter).grid(row=4, column=0, columnspan=4, sticky="w",
                                                         pady=(8, 0))

        # --- Обновления ---
        g4 = ttk.LabelFrame(f, text="Обновления", padding=10)
        g4.grid(row=5, column=0, sticky="we", pady=(10, 0))
        self.v_checkupd = tk.BooleanVar()
        ttk.Checkbutton(g4, text="Проверять обновления при запуске",
                        variable=self.v_checkupd).pack(side="left")
        ttk.Button(g4, text="Проверить сейчас", command=lambda: self.check_updates(manual=True)).pack(
            side="left", padx=12)
        ttk.Label(g4, text=f"Версия {__version__}", style="Hint.TLabel").pack(side="left")

        ttk.Label(f, style="Hint.TLabel",
                  text="Настройки сохраняются автоматически. Ключи хранятся только на этом компьютере.").grid(
            row=6, column=0, sticky="w", pady=(12, 0))

    # ------------------------------------------------------------ настройки
    def _load_into_widgets(self):
        s = self.s
        self.v_provider.set(s.provider if s.provider in PROVIDERS else "DeepSeek")
        self._provider_changed(load=True)
        self.v_pin.set(str(s.price_in))
        self.v_pcache.set(str(s.price_cached))
        self.v_pout.set(str(s.price_out))
        self.v_prof.set(s.profanity)
        self.v_analyze.set(s.analyze_film)
        self.v_sdh.set(s.strip_sdh)
        mode = "overwrite" if s.overwrite else s.existing_mode
        self.v_existing.set(next((k for k, v in EXISTING.items() if v == mode), list(EXISTING)[0]))
        self.v_checkupd.set(s.check_updates)
        self.v_skipru.set(s.skip_if_russian)
        self.v_enc.set(next((k for k, v in ENCODINGS.items() if v == s.encoding), list(ENCODINGS)[0]))
        self.v_workers.set(str(s.workers))
        self.v_batch.set(str(s.batch_size))
        self.v_pstyle.set(s.prompt_style if s.prompt_style in ("classic", "new") else "classic")
        self.v_glossary.set(s.use_glossary)
        self.v_cmpsize.set(str(s.compare_size))
        self.v_syncafter.set(s.sync_after)

    def _provider_changed(self, load=False):
        if not load:
            self._store_provider_fields()
        p = self.v_provider.get()
        self.s.provider = p
        self.v_key.set(self.s.keys.get(p, ""))
        self.v_model.set(self.s.models.get(p, PROVIDERS[p]["models"][0]))
        self.model_cb.configure(values=PROVIDERS[p]["models"])
        self.key_link.configure(text=f"Где взять ключ: {KEY_URLS[p]}")
        if p == "OpenRouter":
            self.models_btn.pack(side="left", padx=6)
        else:
            self.models_btn.pack_forget()
        self._update_engine_label()

    def _store_provider_fields(self):
        p = self.s.provider
        self.s.keys[p] = self.v_key.get().strip()
        self.s.models[p] = self.v_model.get().strip()

    def _num(self, var, default, cast=float):
        try:
            return cast(str(var.get()).replace(",", "."))
        except ValueError:
            return default

    def collect_settings(self) -> Settings:
        self._store_provider_fields()
        s = self.s
        s.provider = self.v_provider.get()
        s.price_in = self._num(self.v_pin, s.price_in)
        s.price_cached = self._num(self.v_pcache, s.price_cached)
        s.price_out = self._num(self.v_pout, s.price_out)
        s.profanity = self.v_prof.get()
        s.analyze_film = self.v_analyze.get()
        s.strip_sdh = self.v_sdh.get()
        s.existing_mode = EXISTING.get(self.v_existing.get(), "ask")
        s.overwrite = False
        s.check_updates = self.v_checkupd.get()
        s.skip_if_russian = self.v_skipru.get()
        s.encoding = ENCODINGS.get(self.v_enc.get(), "utf-8-sig")
        s.workers = max(1, min(8, self._num(self.v_workers, 4, int)))
        s.batch_size = max(10, min(100, self._num(self.v_batch, 40, int)))
        s.prompt_style = self.v_pstyle.get() or "classic"
        s.use_glossary = self.v_glossary.get()
        s.compare_size = max(30, min(600, self._num(self.v_cmpsize, 150, int)))
        s.sync_after = self.v_syncafter.get()
        try:
            save_settings(s)
        except OSError as e:
            self.log(f"Не удалось сохранить настройки: {e}")
        self._update_engine_label()
        return s

    def _update_engine_label(self):
        if hasattr(self, "engine_lbl"):
            p = self.v_provider.get() if hasattr(self, "v_provider") else self.s.provider
            m = self.v_model.get() if hasattr(self, "v_model") else self.s.model
            st = self.v_pstyle.get() if hasattr(self, "v_pstyle") else self.s.prompt_style
            st_name = "новый промпт" if st == "new" else "проверенный промпт"
            self.engine_lbl.configure(text=f"Движок: {p} · {m} · {st_name}  ⚙")

    def _filter_models(self, _event=None):
        if self.v_provider.get() != "OpenRouter" or not self.all_models:
            return
        text = self.v_model.get().strip().lower()
        vals = [m for m in self.all_models if text in m.lower()] if text else self.all_models
        self.model_cb.configure(values=vals[:300])

    def load_models(self):
        self.models_btn.configure(state="disabled")

        def job():
            try:
                models = list_openrouter_models()
                self.q.put(("models", models))
            except Exception as e:  # noqa: BLE001
                self.q.put(("models_err", str(e)))
        threading.Thread(target=job, daemon=True).start()

    def test_connection(self):
        s = self.collect_settings()
        if not s.api_key:
            self.test_lbl.configure(text="Сначала введите ключ", foreground="#c62828")
            return
        self.test_lbl.configure(text="Проверяю…", foreground="#666")

        def job():
            try:
                c = ChatClient(s.provider, s.api_key, s.model, timeout=60)
                text, _ = c.chat("Ответь одним словом по-русски.", "Переведи: Hello", max_tokens=20,
                                 json_mode=False)
                self.q.put(("test", True, f"Работает ✔  (ответ: {text.strip()[:30]})"))
            except ApiError as e:
                self.q.put(("test", False, str(e)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("test", False, f"Ошибка: {e}"))
        threading.Thread(target=job, daemon=True).start()

    # ------------------------------------------------------------- список
    def add_paths(self, paths):
        files = collect_inputs([p for p in paths if p])
        existing = {os.path.normcase(p) for p in self.items.values()}
        added = 0
        for p in files:
            if os.path.normcase(p) in existing:
                continue
            status = "готово ранее" if os.path.exists(output_path_for(p)) else "ожидает"
            iid = self.tv.insert("", "end", values=(os.path.basename(p), os.path.dirname(p), status),
                                 tags=("skip",) if status == "готово ранее" else ())
            self.items[iid] = p
            added += 1
        if paths and not added:
            self.log("Новых фильмов или .srt не найдено.")
        if self.items:
            self.status_lbl.configure(text=f"В очереди: {len(self.items)}")

    def pick_files(self):
        paths = filedialog.askopenfilenames(
            title="Выберите фильмы или субтитры",
            initialdir=self.s.last_dir or None,
            filetypes=[("Фильмы и субтитры", "*.mkv *.srt *.mp4 *.avi"), ("Все файлы", "*.*")])
        if paths:
            self.s.last_dir = os.path.dirname(paths[0])
            self.add_paths(list(paths))

    def pick_folder(self):
        d = filedialog.askdirectory(title="Папка с фильмами", initialdir=self.s.last_dir or None)
        if d:
            self.s.last_dir = d
            self.add_paths([d])

    def remove_selected(self):
        if self.worker:
            return
        for iid in self.tv.selection():
            self.tv.delete(iid)
            self.items.pop(iid, None)

    def clear_list(self):
        if self.worker:
            return
        self.tv.delete(*self.tv.get_children())
        self.items.clear()

    def set_status(self, iid, text, tag=None):
        if self.tv.exists(iid):
            vals = list(self.tv.item(iid, "values"))
            vals[2] = text
            self.tv.item(iid, values=vals, tags=(tag,) if tag else ())

    # ---------------------------------------------------------------- лог
    def log(self, text):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    # ------------------------------------------------------------- запуск
    def _launch(self, target, *args):
        self.stop_event.clear()
        for b in (self.start_btn, self.sync_btn, self.cmp_btn):
            b.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.pb["value"] = 0
        self.worker = threading.Thread(target=target, args=args, daemon=True)
        self.worker.start()

    def _need_key(self, s: Settings) -> bool:
        if not s.api_key or not s.model:
            messagebox.showwarning("FilmSub", "Укажите API-ключ и модель на вкладке «Настройки».")
            self.nb.select(self.tab_set)
            return True
        return False

    def _selected_jobs(self):
        sel = [i for i in self.tv.selection() if i in self.items]
        if not sel and len(self.items) == 1:
            sel = list(self.items)
        return [(iid, self.items[iid]) for iid in sel]

    def start_sync(self):
        if self.worker:
            return
        jobs = self._selected_jobs()
        if not jobs:
            messagebox.showinfo("FilmSub", "Выберите в списке фильмы (или их .srt), которые нужно синхронизировать.")
            return
        s = self.collect_settings()
        self._launch(self._run_sync, jobs, s)

    def start_compare(self):
        if self.worker:
            return
        jobs = self._selected_jobs()
        if len(jobs) != 1:
            messagebox.showinfo("FilmSub", "Выберите в списке один фильм или .srt для сравнения стилей.")
            return
        s = self.collect_settings()
        if self._need_key(s):
            return
        n = s.compare_size
        if not messagebox.askyesno(
                "FilmSub", f"Будет переведено около {n} реплик двумя стилями — проверенным и новым "
                           f"(обычно это 1–3 цента). Результат откроется в браузере.\n\nПродолжить?"):
            return
        self._launch(self._run_compare, jobs[0], s)

    # ---------------------------------------------------------- обновления
    def check_updates(self, manual: bool):
        def job():
            try:
                rel = check_latest()
                self.q.put(("update", rel, manual, None))
            except UpdateError as e:
                self.q.put(("update", None, manual, str(e)))
        threading.Thread(target=job, daemon=True).start()

    def _on_update_info(self, rel, manual, err):
        if err:
            if manual:
                messagebox.showwarning("FilmSub", err)
            return
        if not rel or not is_newer(rel.version):
            if manual:
                messagebox.showinfo("FilmSub", f"У вас последняя версия ({__version__}).")
            return
        notes = rel.notes[:600] + ("…" if len(rel.notes) > 600 else "")
        text = f"Доступна новая версия FilmSub {rel.version} (у вас {__version__}).\n\n"
        if notes:
            text += notes + "\n\n"
        if not can_self_update():
            if messagebox.askyesno("FilmSub — обновление", text + "Открыть страницу загрузки?"):
                webbrowser.open(rel.page)
            return
        if self.worker:
            if manual:
                messagebox.showinfo("FilmSub", "Обновление можно установить после окончания перевода.")
            return
        if not messagebox.askyesno("FilmSub — обновление",
                                   text + "Обновить сейчас? Программа перезапустится, настройки и словарь "
                                          "сохранятся."):
            return
        self._launch(self._run_update, rel)

    def _run_update(self, rel):
        try:
            new = download(rel, progress=lambda f, t: self.q.put(("progress", f, t)))
            self.q.put(("log", f"Обновление {rel.version} скачано, перезапускаю программу…"))
            self.q.put(("restart", new))
        except UpdateError as e:
            self.q.put(("log", f"✖ {e}"))
            self.q.put(("finished", None))

    def _ensure_tools(self) -> bool:
        if find_tools():
            return True
        try:
            download_tools(lambda t: self.q.put(("log", t)),
                           progress=lambda f, t: self.q.put(("progress", f, t)))
            return True
        except SyncError as e:
            self.q.put(("log", f"  ✖ {e}"))
            return False

    def _sync_one(self, iid, path, s: Settings, out: str | None = None) -> str:
        """Синхронизирует .ru.srt этого фильма. Возвращает подпись для статуса."""
        out = out or output_path_for(path)
        if not os.path.exists(out):
            self.q.put(("log", f"  Нет файла {os.path.basename(out)} — сначала переведите фильм"))
            return "нет .ru.srt"
        video = find_video_for(path)
        if not video:
            self.q.put(("log", f"  Рядом с {os.path.basename(out)} нет самого фильма — синхронизировать "
                               "не с чем"))
            return "нет фильма"
        if not self._ensure_tools():
            return "нет alass"
        try:
            rep = sync_subtitles(video, out, lambda t: self.q.put(("log", t)), encoding=s.encoding,
                                 stop_flag=self.stop_event.is_set)
        except SyncError as e:
            self.q.put(("log", f"  ✖ {e}"))
            return "ошибка синхр."
        return {"changed": "синхронизировано", "unchanged": "уже синхронно",
                "rejected": "синхр. отклонена"}[rep.status]

    def _run_sync(self, jobs, s: Settings):
        for k, (iid, path) in enumerate(jobs, 1):
            if self.stop_event.is_set():
                break
            self.q.put(("log", f"▶ Синхронизация: {os.path.basename(path)}"))
            self.q.put(("status", iid, "синхронизация…", None))
            self.q.put(("progress", (k - 1) / len(jobs), f"Синхронизация {k} из {len(jobs)}"))
            label = self._sync_one(iid, path, s)
            tag = "done" if label in ("синхронизировано", "уже синхронно") else "error"
            self.q.put(("status", iid, label, tag))
        self.q.put(("finished", None))

    def _run_compare(self, job, s: Settings):
        iid, path = job
        self.q.put(("status", iid, "сравнение…", None))
        try:
            out = run_compare(path, s, lambda t: self.q.put(("log", t)),
                              lambda f, t="": self.q.put(("progress", f, t)), self.stop_event.is_set)
        except ApiError as e:
            self.q.put(("log", f"  ✖ {e}"))
            out = None
        except Exception as e:  # noqa: BLE001
            self.q.put(("log", f"  ✖ Непредвиденная ошибка: {e!r}"))
            out = None
        if out:
            self.q.put(("status", iid, "сравнение готово", "done"))
            self.q.put(("open", out))
        else:
            self.q.put(("status", iid, "сравнение не сделано", "error"))
        self.q.put(("finished", None))

    def start(self):
        if self.worker:
            return
        s = self.collect_settings()
        if not s.api_key:
            messagebox.showwarning("FilmSub", "Укажите API-ключ на вкладке «Настройки».")
            self.nb.select(self.tab_set)
            return
        if not s.model:
            messagebox.showwarning("FilmSub", "Укажите модель на вкладке «Настройки».")
            self.nb.select(self.tab_set)
            return
        jobs = [(iid, p) for iid, p in self.items.items() if self.tv.exists(iid)]
        if not jobs:
            messagebox.showinfo("FilmSub", "Добавьте фильмы или субтитры для перевода.")
            return
        mode = s.existing_mode
        existing = [p for _, p in jobs if os.path.exists(output_path_for(p))]
        if existing and mode == "ask":
            mode = self.ask_existing(existing)
            if mode is None:
                return
        self._launch(self._run, jobs, s, mode)

    def ask_existing(self, paths: list[str]) -> str | None:
        """Спрашивает, что делать с фильмами, у которых перевод уже есть."""
        dlg = tk.Toplevel(self)
        dlg.title("Перевод уже есть")
        dlg.transient(self)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=16)
        frm.pack(fill="both")
        names = "\n".join("• " + os.path.basename(output_path_for(p)) for p in paths[:6])
        if len(paths) > 6:
            names += f"\n… и ещё {len(paths) - 6}"
        ttk.Label(frm, text=f"Для {len(paths)} из выбранных фильмов русские субтитры уже есть:\n\n{names}"
                            "\n\nЧто сделать?", justify="left").pack(anchor="w")
        result = {"mode": None}

        def choose(m):
            result["mode"] = m
            dlg.destroy()

        b = ttk.Frame(frm)
        b.pack(fill="x", pady=(14, 0))
        ttk.Button(b, text="Сохранить вторым вариантом", command=lambda: choose("alt")).pack(fill="x")
        ttk.Label(b, text="Рядом появится Фильм.ru.v2.srt — в Dune можно переключаться между вариантами",
                  style="Hint.TLabel").pack(anchor="w", pady=(2, 8))
        ttk.Button(b, text="Перезаписать", command=lambda: choose("overwrite")).pack(fill="x")
        ttk.Label(b, text="Старый перевод сохранится в резервной копии", style="Hint.TLabel").pack(
            anchor="w", pady=(2, 8))
        ttk.Button(b, text="Пропустить эти фильмы", command=lambda: choose("skip")).pack(fill="x")
        ttk.Button(b, text="Отмена", command=dlg.destroy).pack(fill="x", pady=(8, 0))
        dlg.grab_set()
        self.wait_window(dlg)
        return result["mode"]

    def stop(self):
        self.stop_event.set()
        self.stop_btn.configure(state="disabled")
        self.status_lbl.configure(text="Останавливаю после текущих запросов…")

    def _run(self, jobs, s: Settings, mode: str = "skip"):
        total_cost = 0.0
        exact = True
        n = len(jobs)
        for k, (iid, path) in enumerate(jobs, 1):
            if self.stop_event.is_set():
                break
            self.q.put(("status", iid, "в работе…", None))
            prefix = f"Файл {k} из {n}"

            def progress(frac, text="", prefix=prefix):
                self.q.put(("progress", frac, f"{prefix} · {text}" if text else prefix))

            def on_usage(_u, base=total_cost):
                self.q.put(("cost", base + tr.cost(), tr.usage.cost_usd is not None and exact))

            tr = FileTranslator(s, log=lambda t: self.q.put(("log", t)), progress=progress,
                                stop_flag=self.stop_event.is_set, usage_cb=on_usage)
            try:
                res = tr.translate(path, existing=mode)
            except ApiError as e:
                self.q.put(("log", f"  ✖ {e}"))
                self.q.put(("status", iid, "ошибка", "error"))
                total_cost += tr.cost()
                if not e.retryable:
                    self.q.put(("log", "Перевод остановлен — исправьте проблему и запустите снова."))
                    break
                continue
            except Exception as e:  # noqa: BLE001
                self.q.put(("log", f"  ✖ Непредвиденная ошибка: {e!r}"))
                self.q.put(("status", iid, "ошибка", "error"))
                total_cost += tr.cost()
                continue
            exact = exact and (tr.usage.cost_usd is not None or tr.usage.prompt_tokens == 0)
            total_cost += tr.cost()
            st = res.get("status")
            if st == "done":
                cost = tr.cost()
                label = "готово" + (f" · ${cost:.2f}" if cost >= 0.005 else "")
                if res.get("failed"):
                    label += f" · пропусков {res['failed']}"
                if s.sync_after and not self.stop_event.is_set():
                    self.q.put(("status", iid, "синхронизация…", None))
                    sync_label = self._sync_one(iid, path, s, res.get("output"))
                    label += " · " + sync_label
                self.q.put(("status", iid, label, "done"))
            elif st == "stopped":
                self.q.put(("status", iid, "остановлено", None))
            else:
                self.q.put(("status", iid, "пропущено", "skip"))
        self.q.put(("finished", total_cost))

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.log(msg[1])
                elif kind == "status":
                    self.set_status(msg[1], msg[2], msg[3])
                elif kind == "progress":
                    self.pb["value"] = int(msg[1] * 1000)
                    self.status_lbl.configure(text=msg[2])
                elif kind == "cost":
                    approx = "" if msg[2] else "≈ "
                    self.cost_lbl.configure(text=f"Потрачено: {approx}${msg[1]:.3f}")
                elif kind == "finished":
                    self.worker = None
                    for b in (self.start_btn, self.sync_btn, self.cmp_btn):
                        b.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    stopped = self.stop_event.is_set()
                    self.status_lbl.configure(text="Остановлено" if stopped else "Готово")
                    if not stopped:
                        self.pb["value"] = 1000
                    if msg[1] is not None:
                        self.log(f"Итого потрачено ≈ ${msg[1]:.3f}")
                    self.bell()
                    if self.nb.select() == str(self.tab_glo):
                        self.refresh_glossary()
                elif kind == "update":
                    self._on_update_info(msg[1], msg[2], msg[3])
                elif kind == "restart":
                    try:
                        self.collect_settings()
                    except Exception:  # noqa: BLE001
                        pass
                    apply_and_restart(msg[1])
                    self.destroy()
                    return
                elif kind == "open":
                    webbrowser.open(Path(msg[1]).resolve().as_uri())
                elif kind == "test":
                    self.test_lbl.configure(text=msg[2], foreground="#1a7f37" if msg[1] else "#c62828")
                elif kind == "models":
                    self.models_btn.configure(state="normal")
                    self.all_models = msg[1]
                    self.model_cb.configure(values=msg[1])
                    self.log(f"Загружено моделей OpenRouter: {len(msg[1])}. Введите часть названия "
                             "(например «deepseek» или «claude») и раскройте список.")
                elif kind == "models_err":
                    self.models_btn.configure(state="normal")
                    self.log(f"Не удалось загрузить список моделей: {msg[1]}")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def on_close(self):
        if self.worker and not messagebox.askyesno(
                "FilmSub", "Идёт перевод. Выйти? Прогресс сохранится, и при следующем запуске "
                           "перевод продолжится с того же места."):
            return
        self.stop_event.set()
        try:
            self.collect_settings()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    app = App(sys.argv[1:])
    app.mainloop()


if __name__ == "__main__":
    main()
