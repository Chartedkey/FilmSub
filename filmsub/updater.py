"""Автообновление: проверка релизов на GitHub и замена FilmSub.exe новой версией."""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass

from . import __version__

REPO = "Chartedkey/FilmSub"
API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    exe_url: str
    notes: str
    page: str


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) or (0,)


def is_newer(remote: str, local: str = __version__) -> bool:
    return parse_version(remote) > parse_version(local)


def check_latest(timeout: int = 15) -> Release | None:
    req = urllib.request.Request(API_LATEST, headers={"User-Agent": "FilmSub",
                                                      "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise UpdateError(f"Не удалось проверить обновления: {e}")
    tag = data.get("tag_name") or ""
    exe = next((a.get("browser_download_url") for a in data.get("assets", [])
                if str(a.get("name", "")).lower() == "filmsub.exe"), None)
    if not tag or not exe:
        return None
    return Release(version=tag.lstrip("vV"), exe_url=exe, notes=(data.get("body") or "").strip(),
                   page=data.get("html_url") or RELEASES_PAGE)


def can_self_update() -> bool:
    """Заменить себя может только собранный FilmSub.exe на Windows."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def download(rel: Release, progress=None) -> str:
    exe = sys.executable
    target = os.path.join(os.path.dirname(exe), "FilmSub.new.exe")
    req = urllib.request.Request(rel.exe_url, headers={"User-Agent": "FilmSub"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as r, \
                open(target, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress and total:
                    progress(got / total, f"Загрузка обновления: {got // 1_000_000} из {total // 1_000_000} МБ")
    except PermissionError:
        raise UpdateError("Нет прав на запись в папку с программой. Перенесите FilmSub.exe, например, "
                          "на Рабочий стол или в «Документы» и повторите.")
    except OSError as e:
        try:
            os.remove(target)
        except OSError:
            pass
        raise UpdateError(f"Не удалось скачать обновление: {e}")
    if os.path.getsize(target) < 1_000_000:
        os.remove(target)
        raise UpdateError("Скачанный файл повреждён — попробуйте позже")
    return target


def apply_and_restart(new_exe: str) -> None:
    """Запускает маленький bat-файл: он ждёт закрытия программы, подменяет exe и запускает новую версию."""
    exe = sys.executable
    pid = os.getpid()
    bat = os.path.join(tempfile.gettempdir(), "filmsub_update.bat")
    with open(bat, "w", encoding="cp866", errors="replace") as f:
        f.write(f"""@echo off
:wait
timeout /t 1 /nobreak >nul
tasklist /fi "PID eq {pid}" 2>nul | find "{pid}" >nul && goto wait
move /y "{new_exe}" "{exe}" >nul
start "" "{exe}"
del "%~f0"
""")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(["cmd", "/c", bat], creationflags=flags, close_fds=True)
