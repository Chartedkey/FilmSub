"""Список и извлечение встроенных текстовых субтитров."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from .mkv import SubTrack, read_mkv_subtitles
from .srt import read_srt
from .sync import SyncError, _candidates, _wait

# Графические PGS, DVD/VobSub, DVB и XSUB сюда намеренно не входят.
TEXT_CODECS = {'subrip', 'srt', 'ass', 'ssa', 'mov_text', 'text', 'webvtt'}


class SubtitleError(Exception):
    pass


class SubtitleCancelled(SubtitleError):
    pass


class MediaTrack(SubTrack):
    @property
    def display_number(self):
        return self.number + 1

    @property
    def is_text(self):
        return self.codec in TEXT_CODECS


def media_tools():
    if os.name != 'nt':
        ff, fp = shutil.which('ffmpeg'), shutil.which('ffprobe')
        if ff and fp:
            return ff, fp
    for root in _candidates():
        folder = os.path.join(root, 'ffmpeg', 'bin')
        ff, fp = os.path.join(folder, 'ffmpeg.exe'), os.path.join(folder, 'ffprobe.exe')
        if os.path.isfile(ff) and os.path.isfile(fp):
            return ff, fp
    ff, fp = shutil.which('ffmpeg'), shutil.which('ffprobe')
    return (ff, fp) if ff and fp else None


def list_tracks(path: str, tools=None) -> list[SubTrack]:
    if path.lower().endswith('.mkv'):
        return read_mkv_subtitles(path)
    tools = tools or media_tools()
    if not tools:
        raise SubtitleError('Для чтения встроенных субтитров нужны ffmpeg и ffprobe')
    cmd = [tools[1], '-v', 'error', '-select_streams', 's', '-show_entries',
           'stream=index,codec_name:stream_tags=language,title:stream_disposition=default,forced',
           '-of', 'json', path]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise SubtitleError('Не удалось прочитать дорожки субтитров: ' +
                                result.stderr.decode('utf-8', 'replace').strip()[-2000:])
        streams = json.loads(result.stdout).get('streams', [])
        tracks = []
        for stream in streams:
            tags = {k.lower(): str(v) for k, v in (stream.get('tags') or {}).items()}
            flags = stream.get('disposition') or {}
            tracks.append(MediaTrack(number=int(stream['index']), codec=stream.get('codec_name', ''),
                                     language=tags.get('language', 'und').lower(), name=tags.get('title', ''),
                                     default=bool(flags.get('default')), forced=bool(flags.get('forced'))))
        return tracks
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
        raise SubtitleError(f'Не удалось прочитать дорожки субтитров: {e}') from e


def extract(path: str, track: SubTrack, tools=None, stop_flag=None, progress=None):
    if stop_flag and stop_flag():
        raise SubtitleCancelled('Остановлено')
    if not track.is_text:
        raise SubtitleError('Графическим субтитрам нужно распознавание текста (OCR)')
    if path.lower().endswith('.mkv'):
        tracks = read_mkv_subtitles(path, wanted=lambda t: t.number == track.number,
                                    progress=progress, stop_flag=stop_flag)
        return next((t.cues for t in tracks if t.number == track.number), [])
    tools = tools or media_tools()
    if not tools:
        raise SubtitleError('Для извлечения субтитров нужен ffmpeg')
    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, 'subtitles.srt')
        cmd = [tools[0], '-v', 'error', '-nostdin', '-y', '-i', path, '-map', f'0:{track.number}',
               '-c:s', 'srt', dest]
        with tempfile.TemporaryFile() as errors:
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errors,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                _wait(proc, stop_flag, 1800, 'ffmpeg')
            except (OSError, SyncError) as e:
                if stop_flag and stop_flag():
                    raise SubtitleCancelled('Остановлено') from e
                raise SubtitleError(f'Не удалось извлечь субтитры: {e}') from e
            if proc.returncode or not os.path.isfile(dest):
                errors.seek(0, os.SEEK_END)
                errors.seek(max(0, errors.tell() - 4096))
                raise SubtitleError('ffmpeg не смог извлечь текстовую дорожку: ' +
                                    errors.read().decode('utf-8', 'replace').strip())
        return read_srt(dest)
