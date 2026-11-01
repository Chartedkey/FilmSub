"""Автообновление: проверка релизов на GitHub и замена FilmSub.exe новой версией."""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
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
    """Подменяет exe без вспомогательных скриптов и запускает новую версию.

    Windows не даёт удалить запущенный exe, но разрешает его переименовать: текущий файл
    становится FilmSub.old.exe, на его место встаёт скачанный, и новая версия запускается.
    Старый файл удаляет уже новая версия при запуске (cleanup_old)."""
    exe = sys.executable
    folder = os.path.dirname(exe)
    old = os.path.join(folder, "FilmSub.old.exe")
    try:
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        old = os.path.join(folder, f"FilmSub.old.{os.getpid()}.exe")
    try:
        os.replace(exe, old)
    except OSError as e:
        raise UpdateError(f"Не удалось заменить программу: {e}")
    try:
        os.replace(new_exe, exe)
    except OSError as e:
        try:
            os.replace(old, exe)       # откат
        except OSError:
            pass
        raise UpdateError(f"Не удалось установить обновление: {e}")
    env = dict(os.environ)
    # новый процесс должен распаковаться заново, а не использовать папку текущего
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for k in [k for k in env if k.startswith("_PYI") or k == "_MEIPASS2"]:
        env.pop(k, None)
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([exe], cwd=folder, env=env, creationflags=flags, close_fds=True)


def cleanup_old() -> None:
    """Удаляет остатки прошлого обновления. Старая версия может ещё закрываться — пробуем несколько раз."""
    if not can_self_update():
        return
    folder = os.path.dirname(sys.executable)

    def job():
        for _ in range(20):
            left = 0
            try:
                names = os.listdir(folder)
            except OSError:
                return
            for n in names:
                low = n.lower()
                if (low.startswith("filmsub.old") and low.endswith(".exe")) or low == "filmsub.new.exe":
                    try:
                        os.remove(os.path.join(folder, n))
                    except OSError:
                        left += 1
            if not left:
                return
            time.sleep(1.5)

    threading.Thread(target=job, daemon=True).start()
