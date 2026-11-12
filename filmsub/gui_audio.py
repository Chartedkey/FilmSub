"""Выбор языка оригинала для синхронизации с готовыми субтитрами."""
from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

from .sync import AudioTrack

LANG_NAMES = {
    "ru": "русский", "rus": "русский", "en": "английский", "eng": "английский",
    "fr": "французский", "fra": "французский", "fre": "французский",
    "de": "немецкий", "deu": "немецкий", "ger": "немецкий",
    "it": "итальянский", "ita": "итальянский", "es": "испанский", "spa": "испанский",
    "pt": "португальский", "por": "португальский", "ja": "японский", "jpn": "японский",
    "ko": "корейский", "kor": "корейский", "zh": "китайский", "zho": "китайский",
    "chi": "китайский", "ar": "арабский", "ara": "арабский", "hi": "хинди", "hin": "хинди",
    "sv": "шведский", "swe": "шведский", "pl": "польский", "pol": "польский",
    "tr": "турецкий", "tur": "турецкий", "uk": "украинский", "ukr": "украинский",
}


def track_label(track: AudioTrack) -> str:
    language = LANG_NAMES.get(track.lang)
    label = f"{language} ({track.lang})" if language else track.label()
    if language and track.title:
        label += f" «{track.title}»"
    return f"№{track.n + 1} — {label}"


def choose_audio(parent, source: str, tracks: list[AudioTrack], stop_flag=None) -> int | None:
    """Вызывается в GUI-потоке; закрытие окна и отмена возвращают None."""
    if stop_flag and stop_flag():
        return None
    result = None
    dlg = tk.Toplevel(parent)
    dlg.title("Звуковая дорожка для синхронизации")
    dlg.transient(parent)
    dlg.resizable(True, False)
    frame = ttk.Frame(dlg, padding=16)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=os.path.basename(source), wraplength=600).pack(anchor="w")
    ttk.Label(frame, text="Выберите оригинальную звуковую дорожку фильма — на любом языке.\n"
                         "Например, для французского фильма выберите французскую речь.\n"
                         "Дубляж и комментарии могут давать неточное совпадение с субтитрами.",
              justify="left", wraplength=600).pack(anchor="w", pady=(8, 12))
    choices = tk.Listbox(frame, height=min(8, len(tracks)), width=76, exportselection=False)
    for track in tracks:
        choices.insert("end", track_label(track))
    choices.pack(fill="x")

    def close(number=None):
        nonlocal result
        result = number
        dlg.destroy()

    def confirm(event=None):
        selection = choices.curselection()
        if selection:
            close(tracks[selection[0]].n)

    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=(14, 0))
    ok = ttk.Button(buttons, text="Синхронизировать", command=confirm, state="disabled")
    ok.pack(side="right")
    ttk.Button(buttons, text="Отмена", command=close).pack(side="right", padx=8)
    choices.bind("<<ListboxSelect>>", lambda _: ok.configure(
        state="normal" if choices.curselection() else "disabled"))
    choices.bind("<Double-Button-1>", confirm)
    dlg.bind("<Return>", confirm)
    dlg.bind("<Escape>", lambda _: close())
    dlg.protocol("WM_DELETE_WINDOW", close)
    if len(tracks) == 1:
        choices.selection_set(0)
        ok.configure(state="normal")
    timer = None

    def check_stop():
        nonlocal timer
        if stop_flag and stop_flag():
            close()
        else:
            timer = dlg.after(200, check_stop)

    dlg.grab_set()
    choices.focus_set()
    timer = dlg.after(200, check_stop)
    try:
        parent.wait_window(dlg)
    finally:
        if timer is not None:
            parent.after_cancel(timer)
    return result
