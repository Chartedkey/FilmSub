@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Сборка FilmSub.exe
echo.
echo  ==============================================
echo    Сборка FilmSub.exe  (нужен интернет, 2-5 минут)
echo  ==============================================
echo.

set "PY="
where py >nul 2>nul && (py -3 -c "import sys" >nul 2>nul && set "PY=py -3")
if not defined PY (
    python -c "import sys; assert sys.version_info >= (3, 9)" >nul 2>nul && set "PY=python"
)
if not defined PY (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
)
if not defined PY (
    echo  Python не найден. Устанавливаю Python 3.12 через winget...
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
    ) else (
        echo.
        echo  Не получилось установить Python автоматически.
        echo  Скачайте его с https://www.python.org/downloads/ ^(при установке отметьте
        echo  "Add python.exe to PATH"^) и запустите этот файл ещё раз.
        pause
        exit /b 1
    )
)

echo  [1/3] Готовлю окружение для сборки...
%PY% -m venv .build_venv || goto :fail
call .build_venv\Scripts\activate.bat || goto :fail
python -m pip install --upgrade pip >nul
echo  [2/3] Устанавливаю PyInstaller...
python -m pip install pyinstaller || goto :fail

echo  [3/3] Собираю FilmSub.exe...
pyinstaller --noconfirm --clean --onefile --windowed --name FilmSub ^
    --icon filmsub.ico --add-data "filmsub.ico;." --add-data "filmsub/data;filmsub/data" FilmSub.pyw || goto :fail

copy /y dist\FilmSub.exe FilmSub.exe >nul || goto :fail
call deactivate
rmdir /s /q build dist .build_venv >nul 2>nul
del /q FilmSub.spec >nul 2>nul

echo.
echo  Готово! Рядом появился FilmSub.exe — его можно перенести куда угодно
echo  и закрепить на панели задач. Эту папку со сборкой можно удалить.
echo.
explorer /select,"%~dp0FilmSub.exe"
pause
exit /b 0

:fail
echo.
echo  Сборка не удалась — пришлите текст ошибки выше.
pause
exit /b 1
