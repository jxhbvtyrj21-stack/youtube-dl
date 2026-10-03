# Збірка

## Автоматична збірка (рекомендовано)

GitHub Actions, файл `.github/workflows/videogen-windows.yml`, запускається
при кожній зміні в `videogen-app/` (і вручну через **Run workflow**).
Два завдання на `windows-latest`:

1. **Tests on Windows** — повний набір автоматичних тестів на справжній
   Windows, включно з перевіркою Job Objects (`tests/ffmpeg/test_job_object_windows.py`).
2. **Build EXE and self-test** — PyInstaller-збірка, самоперевірка зібраної
   програми лише з вбудованим FFmpeg (системний `PATH` очищено), запуск GUI з
   автоматичним закриттям і перевіркою, що не лишилося процесів, пакування в
   ZIP. Готовий архів — в артефакті **VideoGen-win64** сторінки запуску.

## Ручна збірка на Windows

Потрібно: Windows 10/11 x64, Python 3.11 (`py -3.11`), інтернет (для FFmpeg).

```powershell
cd videogen-app
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

Скрипт:

1. створює `.venv-build` і встановлює залежності;
2. завантажує FFmpeg (`packaging\fetch_ffmpeg.ps1`, збірка gyan.dev
   «release essentials») і **перевіряє SHA-256**;
3. запускає тести (`-SkipTests` — пропустити);
4. збирає `dist\VideoGen\` (PyInstaller, режим onedir);
5. виконує `VideoGen-cli.exe --selftest`;
6. пакує `dist\VideoGen-<версія>-win64.zip`.

## Склад збірки

```
VideoGen\
  VideoGen.exe          GUI (без консолі)
  VideoGen-cli.exe      та сама програма з консоллю: --selftest, --version
  _internal\            Python, Qt, Pillow, OpenCV …
    ffmpeg\ffmpeg.exe, ffprobe.exe, LICENSE.txt
    docs\README.md, TROUBLESHOOTING.md
```

Свідомі рішення:

* **onedir, а не onefile**: onefile розпаковується в `%TEMP%` при кожному
  запуску — повільно і щоразу перевіряється антивірусом;
* **без UPX**: стиснуті UPX файли часто дають хибні спрацювання антивірусів;
* **маніфест** (`packaging\videogen.exe.manifest`): `longPathAware`
  (шляхи > 260 символів), Per-Monitor DPI, UTF-8 як кодова сторінка;
* `packaging\launcher.py` викликає `multiprocessing.freeze_support()` —
  обов'язково для дочірніх процесів у зібраному EXE.

## Ліцензії

FFmpeg-збірка gyan.dev «essentials» містить libx264 і поширюється за GPL;
її ліцензія кладеться поруч (`_internal\ffmpeg\LICENSE.txt`). PySide6 (Qt) —
LGPL, підключається динамічно. Перед публічним поширенням програми
перевірте сумісність ліцензій для свого випадку.

## Розробка без збірки

```bash
pip install -e ".[media,gui,dev]"     # ffmpeg/ffprobe мають бути в PATH
python -m videogen.main               # GUI
python -m videogen.main --selftest    # самоперевірка
python -m pytest                      # тести
```
