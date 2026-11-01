"""Окно с результатами проверки перевода: замечания с галочками и сохранение исправленного варианта."""

from __future__ import annotations

import os
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

from .review import ReviewResult, apply_fixes, write_report
from .srt import ms_to_ts

ON, OFF = "☑", "☐"


class ReviewWindow(tk.Toplevel):
    def __init__(self, master, res: ReviewResult, encoding: str, log=None):
        super().__init__(master)
        self.res = res
        self.encoding = encoding
        self.log = log or (lambda t: None)
        self.title(f"Проверка перевода — {os.path.basename(res.ru_path)}")
        self.geometry("1100x700")
        self.minsize(760, 480)
        self.cur: int | None = None

        top = ttk.Frame(self, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        score = f"{res.score} из 10" if res.score is not None else "—"
        imp = sum(1 for i in res.issues if i.severity == "важно")
        cost = f" · стоимость ${res.cost:.3f}" if res.cost is not None else ""
        ttk.Label(top, text=f"Оценка перевода: {score}   ·   замечаний: {len(res.issues)} (важных {imp})"
                            f"   ·   проверено реплик: {res.checked}{cost}",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        ttk.Label(top, text=f"Модель-редактор: {res.model}. Галочками отмечены исправления, которые попадут в "
                            "новый вариант. Важные отмечены сразу, мелочи — на ваше усмотрение. Щёлкните по ☐/☑, "
                            "чтобы принять или отклонить; исправление можно отредактировать внизу.",
                  style="Hint.TLabel", wraplength=1040, justify="left").pack(anchor="w", pady=(2, 0))

        mid = ttk.PanedWindow(self, orient="vertical")
        mid.pack(fill="both", expand=True, padx=12, pady=6)

        tf = ttk.Frame(mid)
        cols = ("ok", "n", "time", "type", "ru", "fix")
        self.tv = ttk.Treeview(tf, columns=cols, show="headings", selectmode="browse")
        for c, t, w, st in (("ok", "", 34, False), ("n", "№", 48, False), ("time", "Время", 70, False),
                            ("type", "Тип", 110, False), ("ru", "Сейчас", 380, True), ("fix", "Исправление", 380, True)):
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

        bf = ttk.Frame(self, padding=(12, 4, 12, 12))
        bf.pack(fill="x")
        ttk.Button(bf, text="Отметить все", command=lambda: self._mark(lambda i: True)).pack(side="left")
        ttk.Button(bf, text="Только важные", command=lambda: self._mark(lambda i: i.severity == "важно")).pack(
            side="left", padx=6)
        ttk.Button(bf, text="Снять все", command=lambda: self._mark(lambda i: False)).pack(side="left")
        ttk.Button(bf, text="Отчёт в браузере", command=self._open_report).pack(side="left", padx=(18, 0))
        self.l_count = ttk.Label(bf, text="")
        self.l_count.pack(side="left", padx=18)
        ttk.Button(bf, text="Закрыть", command=self.destroy).pack(side="right")
        self.save_btn = ttk.Button(bf, text="Сохранить исправленный вариант", style="Accent.TButton",
                                   command=self._save)
        self.save_btn.pack(side="right", padx=6)

        for k, it in enumerate(res.issues):
            self.tv.insert("", "end", iid=str(k), values=self._row(it),
                           tags=("imp",) if it.severity == "важно" else ())
        self.tv.bind("<Button-1>", self._click)
        self.tv.bind("<<TreeviewSelect>>", lambda e: self._show())
        self.tv.bind("<space>", lambda e: self._toggle(self.cur))
        if res.issues:
            self.tv.selection_set("0")
        else:
            self.l_problem.configure(text="Модель не нашла ошибок в переводе.", foreground="#1a7f37")
            self.save_btn.configure(state="disabled")
        self._update_count()

    @staticmethod
    def _one_line(t: str) -> str:
        return " / ".join(x.strip() for x in t.splitlines() if x.strip())

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
        if k is None:
            return
        it = self.res.issues[k]
        it.accepted = not it.accepted
        self.tv.item(str(k), values=self._row(it))
        self._update_count()

    def _mark(self, pred):
        for k, it in enumerate(self.res.issues):
            it.accepted = bool(pred(it))
            self.tv.item(str(k), values=self._row(it))
        self._update_count()

    def _show(self):
        sel = self.tv.selection()
        if not sel:
            return
        self.cur = int(sel[0])
        it = self.res.issues[self.cur]
        self.l_problem.configure(text=f"{it.type}, {it.severity}: {it.problem}")
        self.l_en.configure(text=it.en or "—")
        self.l_ru.configure(text=it.ru)
        self.t_fix.delete("1.0", "end")
        self.t_fix.insert("1.0", it.fix)

    def _fix_edited(self, _e=None):
        if self.cur is None:
            return
        it = self.res.issues[self.cur]
        text = self.t_fix.get("1.0", "end").strip()
        if text and text != it.fix:
            it.fix = text
            self.tv.item(str(self.cur), values=self._row(it))

    def _update_count(self):
        n = sum(1 for i in self.res.issues if i.accepted)
        self.l_count.configure(text=f"Принято исправлений: {n} из {len(self.res.issues)}")

    def _open_report(self):
        try:
            write_report(self.res)       # с учётом правок
            webbrowser.open(Path(self.res.report).resolve().as_uri())
        except OSError as e:
            messagebox.showwarning("FilmSub", f"Не удалось открыть отчёт: {e}", parent=self)

    def _save(self):
        n = sum(1 for i in self.res.issues if i.accepted)
        if not n:
            messagebox.showinfo("FilmSub", "Не отмечено ни одного исправления.", parent=self)
            return
        try:
            out, applied = apply_fixes(self.res, self.encoding)
        except OSError as e:
            messagebox.showerror("FilmSub", f"Не удалось сохранить: {e}", parent=self)
            return
        self.log(f"✔ Исправленный вариант сохранён: {out} (исправлений: {applied})")
        messagebox.showinfo("FilmSub", f"Сохранено: {os.path.basename(out)}\n\nИсправлений: {applied}. "
                                       f"Исходный перевод {os.path.basename(self.res.ru_path)} не изменён; "
                                       "в Dune можно переключаться между вариантами.", parent=self)
