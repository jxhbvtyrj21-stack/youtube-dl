# План реалізації

Кожна фаза закінчується **воротами**: перелік тестів, які мають пройти, перш ніж
почнеться наступна фаза. GUI (фаза 6) не починається, доки фази 2–5 не пройшли
свої ворота. Посилання `§N` ведуть до ARCHITECTURE.md.

Позначення статусу: ✅ виконано · ⏳ у роботі · ⬜ не почато.

---

## PHASE 1 — Аудит і архітектура ✅

* [x] Аналіз вимог і корінних причин (§0)
* [x] Компоненти, модель процесів, потік даних (§2–§4)
* [x] Машини станів job, batch, зображення (§5)
* [x] Відмови та реакції (§17), життєвий цикл процесів (§7, §8)
* [x] Керування ресурсами (§9), формули timeout (§7.2)
* [x] OPEN_QUESTIONS.md
* [x] Технічний спайк: сегментний рендер + concat + `apad` дає точну кількість
      кадрів і збіг тривалості аудіо/відео (FFmpeg 6.1: 222 кадри = 7,400 с,
      аудіо 7,400 с)

Ворота: архітектура покриває всі 55 пунктів ТЗ (таблиця §23).

---

## PHASE 2 — Core ✅

| Файл | Зміст |
|---|---|
| `core/models.py` | `JobStatus` (з **PARTIAL**), `Stage`, `BatchState`, `ImageStatus`, `Mode`, `Orientation`; `JobConfig`, `JobState`, `ImageItem`, `MediaInfo`, `ProcessResult`, `RenderResult`, `ErrorInfo`; таблиці переходів job і batch |
| `core/errors.py` | ієрархія помилок §11, `retry_budget()`, `classify()` |
| `core/events.py` | типізовані команди GUI→Engine і події Engine→GUI |
| `core/state_manager.py` | SQLite (WAL, `synchronous=FULL`), `transition()` з перевіркою в транзакції, recovery `RUNNING/RETRY_PENDING → INTERRUPTED`, карантин пошкодженої БД, `pending_cleanup` |
| `core/cancellation.py` | `CancellationToken` (з callback-ами для вбивства процесів), `PauseGate` (пауза між атомарними операціями) |
| `core/job_manager.py` | ітеративний retry з бюджетом на клас помилки, абсолютна межа спроб між перезапусками (`HARD_ATTEMPT_CAP`), `finalize` завжди в `finally` |
| `core/queue_manager.py` | слоти паралельності, PAUSE/RESUME/STOP/CANCEL CURRENT, RESOURCE_WAIT з верхньою межею, автопауза при нестачі диска |
| `core/timeouts.py` | формули timeout §7.2 |
| `storage/atomic.py`, `manifest.py`, `workspace.py`, `cleanup.py` | §10, §15, §20 |
| `applog/logger.py`, `diagnostics.py` | §13: один слухач на всі процеси, ротація, JSONL-журнал job |
| `config/settings.py` | §19: значення за замовчуванням, межі кожного поля, безпечне завантаження |
| `utils/paths.py`, `hashing.py`, `system.py` | §22 |

`config/defaults.py` не створювався: значення за замовчуванням і допустимі
межі визначені поруч із кожним полем у `settings.py` (одне джерело істини).

Ворота пройдено: **257 тестів** (`python -m pytest tests`), три прогони поспіль
без жодного нестабільного результату:
* таблиці переходів job і batch перевірено повністю (усі пари станів);
* `RUNNING → INTERRUPTED` після «перезапуску»; **реальне вбивство процесу**
  (`SIGKILL`) посеред тисяч записів у БД — база цілісна, стан відновлюється;
* атомарний запис: симуляція збою перед `rename` — старий файл цілий;
* cleanup: заблокований файл, read-only файл, «завислий» диск (дедлайн),
  відмова видаляти шлях поза workspace;
* ротація логів, запис з кількох процесів через один слухач, JSONL-журнал job;
* безпечні імена: Unicode, `&#%+'()`, `CON`, довгі імена;
* retry: для кожного класу помилки кількість спроб = бюджет ТЗ §20;
* batch: 100 jobs із 3 помилками → 97 + 3; STOP, CANCEL CURRENT, PAUSE/RESUME,
  нестача ресурсів, нестача диска, паралельні слоти;
* 1000 jobs без витоку потоків; без витоку файлових дескрипторів;
* AST-аудит інваріантів (§18) по всьому пакету.

Знайдені й виправлені під час фази вади:
1. ім'я тимчасового файлу при атомарному записі могло перевищити ліміт
   довжини імені, навіть коли цільове ім'я вміщалося;
2. файл налаштувань у невалідному UTF-8 спричиняв виняток;
3. гонка в журналі job: файл міг закритися до запису останніх повідомлень
   (тепер відкриття/закриття проходять через ту саму чергу, що й записи);
4. PAUSE, натиснутий під час очікування ресурсів, не зупиняв запуск
   наступного job.

---

## PHASE 3 — Media ⬜

| Файл | Зміст |
|---|---|
| `media/image_validator.py` | §6.1 |
| `media/image_normalizer.py` | ланцюг Pillow → OpenCV → FFmpeg §6.2 |
| `workers/image_worker.py` | дочірній процес, протокол §6.3, recycle |
| `media/audio_processor.py` | §9.2 |
| `media/timeline.py` | §9.3 |
| `media/media_validator.py` | §10 п.2 |
| `media/archiver.py` | §12 |

Ворота — фікстури генеруються програмно в `tests/fixtures/factory.py`
(ніяких бінарних файлів у репозиторії):
valid JPG/PNG/WEBP/BMP/TIFF; corrupted JPG і PNG (обрізані, змінені байти);
перейменоване розширення (PNG як `.jpg`); 0 байт; 120 Мп (декомпресійна бомба);
прозорий PNG і палітровий PNG з прозорістю; EXIF orientation 1–8 і битий EXIF;
CMYK JPEG; 16-бітний PNG; Unicode-ім'я (`зображення №1 (копія) & #%+'.png`);
ім'я довжиною 250 символів; відсутній файл; файл, що спричиняє зависання
декодера (тестовий хук ImageWorker `sleep`) → timeout і перехід до наступного
декодера; файл, що спричиняє аварію процесу (`os._exit`) → новий процес.
Аудіо: валідне MP3/WAV/M4A, битий, 0 байт, лише відеопотік, неправильна
metadata duration. Timeline: властивість Σ F_i = total_frames для 10 000
випадкових (D, fps, N).

---

## PHASE 4 — FFmpeg ⬜

| Файл | Зміст |
|---|---|
| `ffmpeg_ctl/locator.py` | пошук поруч з EXE → налаштування → PATH; перевірка `-version` з timeout |
| `ffmpeg_ctl/process_manager.py` | Job Objects (ctypes), kill_tree, реєстр PID, POSIX-гілка |
| `ffmpeg_ctl/runner.py` | §8.2 |
| `ffmpeg_ctl/progress.py` | §8.3 |
| `workers/watchdog.py` | §7 |
| `workers/resource_monitor.py` | §9.1 |

Ворота — використовується `tests/fake_ffmpeg.py` (скрипт, що імітує FFmpeg:
нормальну роботу з progress, зависання з 0 % CPU, livelock 100 % CPU,
аварію з кодом 1, породження дочірнього процесу, ігнорування `q` і SIGTERM,
переповнення stderr 50 МБ):
* hard timeout і stall виявляються в межах `timeout + 1 с`;
* ескалація graceful → terminate → kill tree; після неї жодного живого PID;
* дочірній процес fake-FFmpeg також вбитий;
* stderr 50 МБ не спричиняє deadlock, пам'ять обмежена хвостом;
* `FFmpegUnavailableError` при відсутньому бінарнику;
* парсер прогресу: `N/A`, обрізані блоки, `progress=end`.

---

## PHASE 5 — Pipeline і Engine ⬜

| Файл | Зміст |
|---|---|
| `core/discovery.py` | §4.1 |
| `media/video_renderer.py` | сегменти + mux §8.1 |
| `core/pipeline.py` | етапи §4, write-ahead стан, resume з сегмента |
| `core/engine.py`, `engine_main.py` | цикл команд, heartbeat, recovery §14 |
| `providers/base.py` | інтерфейси MODE B + `FakeProvider` |

Ворота (integration, реальний FFmpeg):
* job 10 зображень + аудіо → валідне MP4, кадри = `ceil(D·fps)`, |A−V| ≤ 1 кадр;
* batch з одним і з кількома битими зображеннями — завершується, лічильники вірні;
* batch 4 job: битий аудіо / timeout (fake) / успіх / 0 валідних зображень → 1 SUCCESS, 3 FAILED;
* недостатньо диска (підмінений `disk_usage`) → job не стартує, повідомлення;
* STOP посеред рендеру → CANCELLED за ≤ `stop_deadline_s`, без процесів і `.tmp`;
* PAUSE → зупинка на межі сегмента; RESUME → продовження з того самого сегмента;
* вбивство Engine (`kill -9` / `TerminateProcess`) посеред рендеру → перезапуск →
  INTERRUPTED → Resume → валідний результат; готові виходи інших job не змінені;
* помилка архівування → FAILED(ARCHIVE), відео ціле;
* підмінена верифікація (обрізаний вихід) → FAILED, файл у output не з'явився;
* MODE B з FakeProvider → валідне відео.

---

## PHASE 6 — GUI ⬜

`gui/main_window.py`, `widgets.py`, `progress.py`, `engine_client.py`,
`recovery_dialog.py`; усі 20 елементів ТЗ §3.

Ворота (pytest-qt, offscreen):
* таймер подій не обробляє > 200 подій за тік; час тіку < 16 мс при потоці 5000 подій/с;
* під час рендеру з fake-FFmpeg, що завис, головний потік відповідає (вимір
  затримки `QTimer.singleShot(0)` < 100 мс протягом усього тесту);
* кнопки доступні відповідно до стану batch (матриця станів);
* смерть Engine → повідомлення і кнопка перезапуску; діалог recovery.

---

## PHASE 7 — Automated tests ⬜

Повний прогін unit + integration + GUI; відповідність 25 сценаріям ТЗ §42
фіксується таблицею в TESTING.md (сценарій → тест-функція → результат).

## PHASE 8 — Stress ⬜

* 100 / 500 / 1000 зображень в одному job і batch 50 job;
* запис RSS Engine/ImageWorker до і після кожного job → CSV + графік;
  критерій витоку §9.1;
* після кожного job: кількість файлів у workspace = 0, PID FFmpeg = 0;
* 500 зображень із 5 % битих → batch завершується;
* скрипт `tests/stress/run_stress.py` з параметрами, щоб повторити на Windows.

## PHASE 9 — Packaging і документація ⬜

* PyInstaller onedir, `videogen.spec`, маніфест (`longPathAware`, DPI aware),
  вбудовані ffmpeg/ffprobe (LGPL/GPL-збірка — ліцензії в `third_party/`);
* збірка в GitHub Actions на `windows-latest` + smoke-тест EXE
  (запуск Engine у headless-режимі `--selftest`: рендер 3 зображень, верифікація,
  перевірка відсутності процесів-сиріт);
* README.md, TROUBLESHOOTING.md, BUILD.md, TESTING.md.

**Обмеження середовища розробки:** поточне середовище — Linux. Код Job Objects
та EXE можна повноцінно перевірити лише на Windows; для цього передбачено
CI-збірку на `windows-latest`. Поки вона не пройде, критерій «Standalone EXE
запускається на Windows» не вважається виконаним.

---

## PHASE 10 — Фінальний review стабільності ⬜

Окремий прохід за чек-листом ТЗ §52 з фіксацією знахідок і виправлень у
`REVIEW.md`.
