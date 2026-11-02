"""Вкладка «Проверка»: выбор файлов, запуск проверки, замечания с галочками, прошлые проверки."""

from __future__ import annotations

import os
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .review import (ReviewResult, apply_fixes, find_english_for, is_russian_file, list_results,
                     load_result, update_saved, variant_choices, write_report)
from .srt import ms_to_ts

ON, OFF = "☑", "☐"
VIDEO_TYPES = "*.mkv *.mp4 *.avi *.m4v *.mov *.ts *.m2ts *.webm"


class ReviewPanel(ttk.Frame):
    """on_check(en_path, ru_path) запускает проверку; on_stop() — останавливает."""

    def __init__(self, master, on_check, on_stop, get_encoding, log=None, model_label=None):
        super().__init__(master, padding=10)
        self.on_check, self.on_stop = on_check, on_stop
        self.get_encoding = get_encoding
        self.log = log or (lambda t: None)
        self.model_label = model_label or (lambda: "")
        self.res: ReviewResult | None = None
        self.cur: int | None = None
        self._build()
        self.refresh_history()

    # ------------------------------------------------------------------ интерфейс
    def _build(self):
        ff = ttk.Frame(self)
        ff.pack(fill="x")
        ff.columnconfigure(1, weight=1)
        self.v_en = tk.StringVar()
        self.v_ru = tk.StringVar()
        ttk.Label(ff, text="Английские субтитры или фильм:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(ff, textvariable=self.v_en).grid(row=0, column=1, sticky="we")
        ttk.Button(ff, text="Выбрать…", command=self._pick_en).grid(row=0, column=2, padx=(6, 0))
        ttk.Label(ff, text="Русский перевод:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        ttk.Entry(ff, textvariable=self.v_ru).grid(row=1, column=1, sticky="we", pady=(6, 0))
        ttk.Button(ff, text="Выбрать…", command=self._pick_ru).grid(row=1, column=2, padx=(6, 0), pady=(6, 0))

        rf = ttk.Frame(self)
        rf.pack(fill="x", pady=(8, 0))
        self.check_btn = ttk.Button(rf, text="▶  Проверить", style="Big.TButton", command=self._check)
        self.check_btn.pack(side="left")
        self.stop_btn = ttk.Button(rf, text="■  Стоп", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.l_model = ttk.Label(rf, text="", style="Hint.TLabel")
        self.l_model.pack(side="left", padx=10)
        pf = ttk.Frame(self)
        pf.pack(fill="x", pady=(6, 0))
        self.l_status = ttk.Label(pf, text="")
        self.l_status.pack(side="left")
        self.pb = ttk.Progressbar(pf, maximum=1000)
        self.pb.pack(side="left", fill="x", expand=True, padx=(10, 0))

        hf = ttk.Frame(self)
        hf.pack(fill="x", pady=(8, 0))
        ttk.Label(hf, text="Прошлые проверки:").pack(side="left")
        self.v_hist = tk.StringVar()
        self.hist_cb = ttk.Combobox(hf, textvariable=self.v_hist, state="readonly")
        self.hist_cb.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(hf, text="Открыть", command=self._open_hist).pack(side="left")
        self._hist: list[tuple[str, str]] = []

        ttk.Separator(self).pack(fill="x", pady=8)
        self.l_summary = ttk.Label(self, text="Выберите файлы и нажмите «Проверить» — или откройте прошлую "
                                              "проверку.", font=("Segoe UI", 10, "bold"))
        self.l_summary.pack(anchor="w")
        ttk.Label(self, text="Галочками отмечены исправления, которые попадут в новый вариант: важные — сразу, "
                             "мелочи — на ваше усмотрение. Щёлкните по ☐/☑, чтобы принять или отклонить; "
                             "исправление можно отредактировать внизу.", style="Hint.TLabel",
                  wraplength=1000, justify="left").pack(anchor="w", pady=(2, 4))

        mid = ttk.PanedWindow(self, orient="vertical")
        mid.pack(fill="both", expand=True)
        tf = ttk.Frame(mid)
        cols = ("ok", "n", "time", "type", "ru", "fix")
        self.tv = ttk.Treeview(tf, columns=cols, show="headings", selectmode="browse", height=10)
        for c, t, w, st in (("ok", "", 34, False), ("n", "№", 48, False), ("time", "Время", 70, False),
                            ("type", "Тип", 120, False), ("ru", "Сейчас", 360, True),
                            ("fix", "Исправление", 360, True)):
            self.tv.heading(c, text=t)
            self.tv.column(c, width=w, stretch=st, anchor="center" if c in ("ok", "n") else "w")
        self.tv.tag_configure("imp", foreground="#b3261e")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=sb.set)
        self.tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        mid.add(tf, weight=3)

        df = ttk.Frame(mid, padding=(0, 6, 0, 0))
        df.columnconfigure(1, weight=1)
        self.l_problem = ttk.Label(df, text="", wraplength=1000, justify="left", foreground="#b3261e")
        self.l_problem.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(df, text="Оригинал:").grid(row=1, column=0, sticky="nw", padx=(0, 8), pady=2)
        self.l_en = ttk.Label(df, text="", wraplength=950, justify="left")
        self.l_en.grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(df, text="Сейчас:").grid(row=2, column=0, sticky="nw", padx=(0, 8), pady=2)
        self.l_ru = ttk.Label(df, text="", wraplength=950, justify="left")
        self.l_ru.grid(row=2, column=1, sticky="w", pady=2)
        ttk.Label(df, text="Исправление:").grid(row=3, column=0, sticky="nw", padx=(0, 8), pady=2)
        self.t_fix = tk.Text(df, height=3, wrap="word", font=("Segoe UI", 10))
        self.t_fix.grid(row=3, column=1, sticky="we", pady=2)
        self.t_fix.bind("<KeyRelease>", self._fix_edited)
        mid.add(df, weight=1)

        bf = ttk.Frame(self, padding=(0, 6, 0, 0))
        bf.pack(fill="x")
        ttk.Button(bf, text="Отметить все", command=lambda: self._mark(lambda i: True)).pack(side="left")
        ttk.Button(bf, text="Только важные", command=lambda: self._mark(lambda i: i.severity == "важно")).pack(
            side="left", padx=6)
        ttk.Button(bf, text="Снять все", command=lambda: self._mark(lambda i: False)).pack(side="left")
        ttk.Button(bf, text="Отчёт в браузере", command=self._open_report).pack(side="left", padx=(18, 0))
        self.l_count = ttk.Label(bf, text="")
        self.l_count.pack(side="left", padx=18)
        self.save_btn = ttk.Button(bf, text="Сохранить исправленный вариант", style="Accent.TButton",
                                   command=self._save, state="disabled")
        self.save_btn.pack(side="right")

        self.tv.bind("<Button-1>", self._click)
        self.tv.bind("<<TreeviewSelect>>", lambda e: self._show())
        self.tv.bind("<space>", lambda e: self._toggle(self.cur))
        self.update_model()

    # ------------------------------------------------------------------ файлы
    def update_model(self):
        self.l_model.configure(text=self.model_label())

    def set_files(self, en: str, ru: str):
        self.v_en.set(en or "")
        self.v_ru.set(ru or "")

    def _pick_en(self):
        start = os.path.dirname(self.v_en.get() or self.v_ru.get() or "") or None
        f = filedialog.askopenfilename(parent=self, title="Английские субтитры или фильм", initialdir=start,
                                       filetypes=[("Субтитры и видео", f"*.srt {VIDEO_TYPES}"),
                                                  ("Все файлы", "*.*")])
        if not f:
            return
        if is_russian_file(f):
            messagebox.showinfo("FilmSub", "Это русские субтитры — их выберите во втором поле.", parent=self)
            return
        self.v_en.set(f)
        if not self.v_ru.get():
            variants = variant_choices(f)
            if variants:
                self.v_ru.set(max(variants, key=os.path.getmtime))

    def _pick_ru(self):
        start = os.path.dirname(self.v_ru.get() or self.v_en.get() or "") or None
        f = filedialog.askopenfilename(parent=self, title="Русский перевод", initialdir=start,
                                       filetypes=[("Субтитры", "*.srt"), ("Все файлы", "*.*")])
        if not f:
            return
        self.v_ru.set(f)
        if not self.v_en.get():
            en = find_english_for(f)
            if en:
                self.v_en.set(en)

    def _check(self):
        en, ru = self.v_en.get().strip(), self.v_ru.get().strip()
        if not en or not os.path.exists(en):
            messagebox.showinfo("FilmSub", "Укажите английские субтитры или сам фильм.", parent=self)
            return
        if not ru or not os.path.exists(ru):
            messagebox.showinfo("FilmSub", "Укажите файл русского перевода.", parent=self)
            return
        self.on_check(en, ru)

    def set_running(self, running: bool):
        self.check_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        if running:
            self.pb["value"] = 0
            self.l_status.configure(text="Проверка…")

    def set_progress(self, frac: float, text: str = ""):
        self.pb["value"] = int(max(0.0, min(1.0, frac)) * 1000)
        if text:
            self.l_status.configure(text=text)

    def finished(self, text: str = ""):
        self.set_running(False)
        self.l_status.configure(text=text)

    # ------------------------------------------------------------------ прошлые проверки
    def refresh_history(self):
        try:
            self._hist = list_results()
        except OSError:
            self._hist = []
        self.hist_cb.configure(values=[label for _, label in self._hist])
        if self._hist:
            self.hist_cb.current(0)

    def _open_hist(self):
        k = self.hist_cb.current() if self._hist else -1
        if k is None or k < 0 or k >= len(self._hist):
            return
        try:
            res = load_result(self._hist[k][0])
        except (OSError, ValueError, TypeError) as e:
            messagebox.showwarning("FilmSub", f"Не удалось открыть проверку: {e}", parent=self)
            return
        self.set_files(res.source, res.ru_path)
        self.show(res)

    # ------------------------------------------------------------------ результат
    def show(self, res: ReviewResult):
        self.res = res
        self.cur = None
        for iid in self.tv.get_children():
            self.tv.delete(iid)
        score = f"{res.score} из 10" if res.score is not None else "—"
        imp = sum(1 for i in res.issues if i.severity == "важно")
        cost = f" · стоимость ${res.cost:.2f}" if res.cost is not None else ""
        self.l_summary.configure(
            text=f"{os.path.basename(res.ru_path)}: оценка {score} · замечаний {len(res.issues)} "
                 f"(важных {imp}) · проверено реплик {res.checked}{cost} · {res.model}")
        for k, it in enumerate(res.issues):
            self.tv.insert("", "end", iid=str(k), values=self._row(it),
                           tags=("imp",) if it.severity == "важно" else ())
        self.save_btn.configure(state="normal" if res.issues else "disabled")
        self.l_problem.configure(text="" if res.issues else "Модель не нашла ошибок в переводе.")
        self.l_en.configure(text="")
        self.l_ru.configure(text="")
        self.t_fix.delete("1.0", "end")
        if res.issues:
            self.tv.selection_set("0")
        self._update_count()

    @staticmethod
    def _one_line(t: str) -> str:
        return " / ".join(x.strip() for x in (t or "").splitlines() if x.strip())

    def _row(self, it):
        return (ON if it.accepted else OFF, it.idx + 1, ms_to_ts(it.start_ms)[:8],
                it.type + ("" if it.severity == "важно" else " · мелочь"),
                self._one_line(it.ru), self._one_line(it.fix))

    def _click(self, event):
        if self.tv.identify_region(event.x, event.y) != "cell":
            return
        if self.tv.identify_column(event.x) == "#1":
            row = self.tv.identify_row(event.y)
            if row:
                self._toggle(int(row))

    def _toggle(self, k):
        if k is None or not self.res:
            return
        it = self.res.issues[k]
        it.accepted = not it.accepted
        self.tv.item(str(k), values=self._row(it))
        self._update_count()

    def _mark(self, pred):
        if not self.res:
            return
        for k, it in enumerate(self.res.issues):
            it.accepted = bool(pred(it))
            self.tv.item(str(k), values=self._row(it))
        self._update_count()

    def _show(self):
        sel = self.tv.selection()
        if not sel or not self.res:
            return
        self.cur = int(sel[0])
        it = self.res.issues[self.cur]
        self.l_problem.configure(text=f"{it.type}, {it.severity}: {it.problem}")
        self.l_en.configure(text=it.en or "—")
        self.l_ru.configure(text=it.ru)
        self.t_fix.delete("1.0", "end")
        self.t_fix.insert("1.0", it.fix)

    def _fix_edited(self, _e=None):
        if self.cur is None or not self.res:
            return
        it = self.res.issues[self.cur]
        text = self.t_fix.get("1.0", "end").strip()
        if text and text != it.fix:
            it.fix = text
            self.tv.item(str(self.cur), values=self._row(it))

    def _update_count(self):
        if not self.res:
            self.l_count.configure(text="")
            return
        n = sum(1 for i in self.res.issues if i.accepted)
        self.l_count.configure(text=f"Принято исправлений: {n} из {len(self.res.issues)}")

    def _open_report(self):
        if not self.res:
            return
        try:
            if not self.res.report:
                self.res.report = os.path.splitext(self.res.data_file)[0] + ".html"
            write_report(self.res)       # с учётом правок
            update_saved(self.res)
            webbrowser.open(Path(self.res.report).resolve().as_uri())
        except OSError as e:
            messagebox.showwarning("FilmSub", f"Не удалось открыть отчёт: {e}", parent=self)

    def _save(self):
        if not self.res:
            return
        n = sum(1 for i in self.res.issues if i.accepted)
        if not n:
            messagebox.showinfo("FilmSub", "Не отмечено ни одного исправления.", parent=self)
            return
        if not os.path.exists(self.res.ru_path):
            messagebox.showwarning("FilmSub", f"Не найден файл перевода:\n{self.res.ru_path}", parent=self)
            return
        try:
            out, applied = apply_fixes(self.res, self.get_encoding())
        except OSError as e:
            messagebox.showerror("FilmSub", f"Не удалось сохранить: {e}", parent=self)
            return
        update_saved(self.res)
        self.log(f"✔ Исправленный вариант сохранён: {out} (исправлений: {applied})")
        messagebox.showinfo("FilmSub", f"Сохранено: {os.path.basename(out)}\n\nИсправлений: {applied}. "
                                       f"Исходный перевод {os.path.basename(self.res.ru_path)} не изменён; "
                                       "в Dune можно переключаться между вариантами.", parent=self)
