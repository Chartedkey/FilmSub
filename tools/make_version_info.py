"""Создаёт version_info.txt для PyInstaller: сведения о версии и авторе внутри FilmSub.exe.

Файл без этих сведений антивирусы чаще считают подозрительным."""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
text = open(os.path.join(ROOT, "filmsub", "__init__.py"), encoding="utf-8").read()
ver = re.search(r'__version__ = "(.+?)"', text).group(1)
nums = [int(x) for x in re.findall(r"\d+", ver)][:4]
nums += [0] * (4 - len(nums))
t = tuple(nums)
dotted = ".".join(map(str, t))

out = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041904B0', [
      StringStruct('CompanyName', 'FilmSub (github.com/Chartedkey/FilmSub)'),
      StringStruct('FileDescription', 'FilmSub — перевод субтитров фильмов на русский'),
      StringStruct('FileVersion', '{dotted}'),
      StringStruct('InternalName', 'FilmSub'),
      StringStruct('LegalCopyright', 'Open source, github.com/Chartedkey/FilmSub'),
      StringStruct('OriginalFilename', 'FilmSub.exe'),
      StringStruct('ProductName', 'FilmSub'),
      StringStruct('ProductVersion', '{ver}')])]),
    VarFileInfo([VarStruct('Translation', [1049, 1200])])
  ]
)
"""
path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "version_info.txt")
with open(path, "w", encoding="utf-8") as f:
    f.write(out)
print(f"version_info: {ver} -> {path}")
