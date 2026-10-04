# VideoGen — архітектура

Статус документа: **PHASE 1 (аудит і архітектура)**. Код ще не написано.
Усі рішення нижче обрано з пріоритетом: стабільність → цілісність даних →
відновлюваність → обробка помилок → пам'ять → процеси → чутливість GUI →
швидкість → зручність.

---

## 0. Аналіз вимог: корінні причини проблем старої програми

Стара реалізація відома симптомами, а не кодом. Кожен симптом зіставлено з
архітектурною причиною та рішенням, яке усуває саме причину.

| Симптом | Імовірна корінна причина | Архітектурне рішення |
|---|---|---|
| Зависання GUI | Важкі операції в main thread; синхронне очікування `subprocess` | GUI-процес лише відображає стан; уся робота — в окремому процесі Engine (§3) |
| Нескінченний рендеринг | Немає timeout/watchdog; `while True` очікування; один великий FFmpeg на весь job | Рендер дробиться на короткі сегменти; кожен subprocess має hard timeout + stall timeout (§7) |
| «Проблемні» зображення вбивають процес | Декодування оригіналу безпосередньо в рендері; виняток або segfault у нативному декодері | Ізольований процес нормалізації з per-image timeout та ланцюгом fallback-декодерів (§6) |
| Переповнення RAM/VRAM | Усі кадри або всі зображення в пам'яті | Потокова обробка «1 зображення → нормалізація → диск → звільнення»; кадри ніколи не зберігаються в Python (§9) |
| Зависання після великої кількості кадрів | Переповнений буфер stderr/stdout у pipe (deadlock), накопичення пам'яті | stdout/stderr читаються окремими потоками в обмежені кільцеві буфери; FFmpeg не отримує кадри через Python (§8) |
| Некоректне завершення batch | Один виняток зупиняє цикл; відсутність стану | Кожен job ізольований `try/finally`; стан у SQLite; помилка job ніколи не виходить за межі job (§5) |
| Залишкові процеси FFmpeg | `process.kill()` без дерева процесів | Windows Job Object з `KILL_ON_JOB_CLOSE` + psutil-перевірка дерева (§8.4) |
| «Успішні» биті відео | Успіх = код повернення 0 | Обов'язкова post-render верифікація через ffprobe + декодування (§10) |

---

## 1. Технологічний стек

| Шар | Рішення | Обґрунтування |
|---|---|---|
| Мова | Python 3.11 x64 | Стабільна, підтримується PyInstaller, PySide6, Pillow |
| GUI | PySide6 (Qt 6, LGPL) | Зрілий цикл подій, `QTimer` для неблокуючого опитування, нативний вигляд Windows |
| Основний декодер | Pillow | Контроль `MAX_IMAGE_PIXELS`, EXIF, ICC, `draft()` для JPEG |
| Резервний декодер | OpenCV (`opencv-python-headless`, `cv2.imdecode`) | Незалежна від Pillow реалізація libpng/libjpeg/libwebp |
| Безпечна конвертація | FFmpeg (`-frames:v 1`) | Третій незалежний декодер, працює в окремому процесі з timeout |
| Медіа | FFmpeg + ffprobe (статичні збірки, поставляються разом з EXE) | Користувачу нічого не потрібно встановлювати |
| Системні метрики | psutil | RAM, CPU, диск, дерево процесів |
| Дерево процесів Windows | Job Objects через `ctypes` (без pywin32) | Гарантоване вбивство нащадків навіть при падінні батьківського процесу |
| Стан | SQLite (WAL, `synchronous=FULL`) + `manifest.json` на кожен job | Атомарні транзакції, переживає збої живлення |
| Тести | pytest, pytest-timeout | Кожен тест має власний timeout — тести теж не можуть зависнути |
| Пакування | PyInstaller **onedir** | `onefile` розпаковується в `%TEMP%` при кожному запуску — повільно і конфліктує з антивірусом |

---

## 2. Структура проєкту

Рекомендована структура з вимог збережена з двома свідомими відхиленнями:

1. Пакет `logging/` перейменовано на `applog/`. Пакет з назвою `logging`
   при запуску `python main.py` або в PyInstaller затіняє стандартний модуль
   `logging`, що ламає всі сторонні бібліотеки.
2. Пакет `ffmpeg/` перейменовано на `ffmpeg_ctl/` з тієї ж причини
   (конфлікт із PyPI-пакетом `ffmpeg`/`ffmpeg-python`, якщо він буде встановлений
   у середовищі розробника).

```
videogen-app/
  ARCHITECTURE.md  IMPLEMENTATION_PLAN.md  OPEN_QUESTIONS.md
  README.md  TROUBLESHOOTING.md  BUILD.md  TESTING.md          (фаза 9)
  pyproject.toml
  videogen/
    __init__.py               # __version__
    main.py                   # точка входу; freeze_support(); запуск GUI
    engine_main.py            # точка входу процесу Engine
    config/
      settings.py             # Settings (dataclass), завантаження/збереження, валідація
      defaults.py
    core/
      models.py               # JobConfig, JobState, JobStatus, Stage, MediaInfo, ImageItem, RenderResult, ProcessResult
      errors.py               # таксономія помилок (§11)
      events.py               # типізовані події Engine → GUI та команди GUI → Engine
      state_manager.py        # SQLite-сховище стану
      job_manager.py          # життєвий цикл одного job, retry-політика
      queue_manager.py        # черга, pause/resume/stop, concurrency
      pipeline.py             # етапи одного job: validate → normalize → render → mux → verify → finalize
      discovery.py            # пошук job у вхідній папці (§4.1)
      engine.py               # головний цикл Engine-процесу (обробка команд, планування)
    media/
      image_validator.py
      image_normalizer.py     # ланцюг декодерів, виконується в ізольованому процесі
      audio_processor.py
      timeline.py             # детермінований розподіл кадрів (§9.3)
      video_renderer.py       # сегментний рендер + фінальний mux
      media_validator.py      # ffprobe + повне декодування
      archiver.py             # потокове архівування (§12)
    ffmpeg_ctl/
      locator.py              # пошук ffmpeg/ffprobe, перевірка версії
      runner.py               # FFmpegRunner: запуск, читання stdout/stderr, progress
      progress.py             # ProgressParser для `-progress pipe:1`
      process_manager.py      # Job Object, kill tree, перевірка завершення
    workers/
      image_worker.py         # дочірній процес нормалізації (протокол запит/відповідь)
      watchdog.py             # Watchdog: hard timeout + stall detection
      resource_monitor.py     # RAM/CPU/диск
    storage/
      workspace.py            # робочі каталоги job, безпечні імена
      manifest.py             # атомарний запис manifest.json
      cleanup.py              # видалення з timeout і повторними спробами
      atomic.py               # atomic write / atomic rename
    applog/
      logger.py               # QueueHandler/QueueListener, ротація, per-job лог
      diagnostics.py          # diagnostic snapshot
    providers/                # тільки для MODE B (див. OPEN_QUESTIONS)
      base.py                 # TTSProvider, ImageGenProvider (інтерфейси з timeout)
    gui/
      main_window.py
      widgets.py
      progress.py
      engine_client.py        # запуск Engine, неблокуючий обмін подіями
      recovery_dialog.py
    utils/
      paths.py                # довгі шляхи, безпечні імена, Unicode
      hashing.py              # потоковий SHA-256
      system.py               # версія ОС, вільне місце, приоритет процесу
  tests/
    conftest.py  fixtures/ (генеруються програмно)
    unit/  integration/  stress/  e2e/
  packaging/
    videogen.spec  build.ps1  third_party/ffmpeg/ (ліцензії)
```

Жоден модуль не повинен перевищувати ~400 рядків; GUI не імпортує `media/`,
`ffmpeg_ctl/`, `workers/` — лише `core/events.py`, `core/models.py`,
`config/` і `gui/engine_client.py`.

---

## 3. Модель процесів

```
┌──────────────────────────────┐
│ GUI process (videogen.exe)   │   Qt main thread: тільки UI.
│  - MainWindow                │   QTimer (100 мс) читає події з events_q
│  - EngineClient              │   неблокуюче (get_nowait, ≤ 200 подій за тік).
└──────────┬───────────────────┘
           │ multiprocessing (spawn), 2 черги: commands_q, events_q
           │ + Windows Job Object "GUI-JOB" (KILL_ON_JOB_CLOSE)
┌──────────▼───────────────────┐
│ Engine process               │   Планувальник, стан, watchdog-и, логування.
│  - QueueManager / JobManager │   Не виконує декодування і кодування сам.
│  - Watchdog threads          │
│  - LogListener               │
└───┬───────────────┬──────────┘
    │               │ subprocess.Popen(argv list, CREATE_NO_WINDOW)
    │               │ кожен процес → Job Object "JOB-<job_id>"
┌───▼─────────┐  ┌──▼─────────────────────┐
│ ImageWorker │  │ ffmpeg / ffprobe       │
│ (процес на  │  │ (короткоживучі, по      │
│  один job)  │  │  одному на сегмент)    │
└─────────────┘  └────────────────────────┘
```

Чому окремий процес Engine, а не потоки в GUI:

* нативний збій (segfault у декодері, OOM) не знищує GUI — GUI показує
  «Engine аварійно завершився», позначає активні job як `INTERRUPTED` і
  пропонує перезапуск;
* GIL: інтенсивна обробка в Engine не впливає на Qt-цикл;
* пам'ять Engine можна повністю звільнити перезапуском, не закриваючи вікно.

Чому ImageWorker — окремий процес, а не потік Engine: зависання всередині C-коду
декодера неможливо перервати в потоці. Процес можна вбити. ImageWorker
перезапускається (а) на кожен новий job, (б) після кожних
`image_worker_recycle_after` зображень (за замовчуванням 200), (в) після
будь-якого timeout. Це обмежує накопичення пам'яті в нативних бібліотеках.

### 3.1. Протокол GUI ↔ Engine

* Усі повідомлення — `@dataclass(frozen=True)` з `core/events.py`; серіалізуються
  pickle через `multiprocessing.Queue`. Ніяких «довільних dict».
* Команди: `StartBatch`, `Pause`, `Resume`, `Stop`, `CancelCurrentJob`,
  `RecoveryDecision(job_id, action)`, `Shutdown`, `Ping`.
* Події: `EngineReady`, `BatchStateChanged`, `JobQueued`, `JobStageChanged`,
  `JobProgress`, `JobFinished`, `ImageSkipped`, `ResourceWarning`,
  `InterruptedJobsFound`, `LogRecord` (тільки INFO+ для журналу GUI),
  `Heartbeat`, `EngineError`.
* Engine надсилає `Heartbeat` кожну 1 с. Якщо GUI не отримав його 15 с і
  `engine.is_alive()` = True — показ попередження «Engine не відповідає» з
  кнопкою «Перезапустити Engine». Якщо `is_alive()` = False — автоматичний
  перехід у стан `ENGINE_DEAD`.
* `JobProgress` агрегується в Engine: не частіше 5 подій/с на job (throttle),
  тому `events_q` не може розростися.
* Черга `events_q` обмежена (`maxsize=1000`); Engine використовує
  `put(timeout=0.5)` і при переповненні відкидає лише `JobProgress`/`LogRecord`
  (вони мають наступників), але ніколи — `JobFinished`/`BatchStateChanged`
  (для них — повтор до 10 разів, після чого запис у лог і стан у SQLite
  залишається джерелом істини; GUI періодично (раз на 5 с) перечитує
  підсумки з SQLite у режимі тільки для читання).

---

## 4. Потік даних

```
INPUT ─► DISCOVERY ─► VALIDATION ─► NORMALIZATION ─► TIMELINE ─► SEGMENT RENDER ─►
  MUX (concat + audio) ─► VERIFICATION ─► FINALIZE (atomic rename) ─► ARCHIVE ─► CLEANUP
```

### 4.1. Discovery (MODE A)

Припущення (до відповіді в OPEN_QUESTIONS Q1):

* якщо `input/` містить підпапки — кожна підпапка з ≥1 аудіо і ≥1 зображенням
  є окремим job; ім'я job = ім'я підпапки;
* якщо `input/` безпосередньо містить аудіо та зображення — один job;
* зображення сортуються «природним» порядком (`img2` < `img10`), без урахування
  регістру, стабільно;
* якщо в папці більше одного аудіо — job отримує статус `FAILED (INPUT)` із
  повідомленням «У папці кілька аудіофайлів; залиште один».

Discovery виконується в Engine і лише читає списки файлів (`os.scandir`), не
відкриває вміст. Для 1000+ файлів — це мілісекунди.

### 4.2. Робочі каталоги

```
<workspace>/
  vg-<batch_id>/
    j0001/
      in/            # нічого не копіюється, тут лише посилання в manifest
      norm/          # i00001.jpg … (безпечні ASCII-імена)
      audio/         # a.wav (нормалізоване)
      seg/           # s00001.mp4 … + concat.txt
      out/           # .tmp.mp4 (до atomic rename)
      manifest.json  # дзеркало стану job
<appdata>/
  state.db
  logs/application.log(.1..5)  logs/errors.log(.1..5)
  diagnostics/<job_id>/{manifest.json, job.log, ffmpeg_stderr_tail.txt, snapshot.json}
  settings.json
```

`<appdata>` = `%LOCALAPPDATA%\VideoGen`. Короткі ASCII-імена у workspace
усувають проблеми з Unicode, лапками, `&#%+` і довжиною шляху в аргументах
FFmpeg та в `concat.txt`. Оригінальні шляхи з Unicode використовуються лише
для читання (через Python, не через shell) і для запису фінального файлу.

### 4.3. Вихід

`<output>/<job_name>.mp4`. Колізія імен → `<job_name> (2).mp4`; існуючий
файл ніколи не перезаписується без явного налаштування `overwrite_existing`.

---

## 5. Машини станів

### 5.1. Стан job (персистентний, у SQLite)

```
                 ┌──────────────── retry (attempts < max для класу помилки) ──────────┐
                 ▼                                                                    │
  (discover) ─► QUEUED ─► RUNNING[stage] ──────────────► SUCCESS   (усі матеріали)    │
                 │          │  │  ├────────────────────────► PARTIAL   (відео є, але     │
                 │          │  │  │                           частину зображень пропущено)│
                 │          │  │  └─ помилка ─► (класифікація) ─► RETRY_PENDING ──────┘
                 │          │  │                               └► FAILED
                 │          │  └─ CancelCurrentJob/Stop ─────────► CANCELLED
                 │          └─ перезапуск програми застав RUNNING ► INTERRUPTED
                 │                                                   │
                 └─ Stop до старту ─► CANCELLED      INTERRUPTED ─(Resume/Retry)─► QUEUED
                                                     INTERRUPTED ─(Ignore)──────► CANCELLED
```

`stage` (лише в межах RUNNING): `VALIDATING → NORMALIZING → AUDIO →
TIMELINE → RENDERING → MUXING → VERIFYING → FINALIZING → ARCHIVING → CLEANUP`.

Дозволені переходи задаються таблицею в `core/models.py`; `StateManager`
відхиляє будь-який інший перехід винятком `IllegalTransition` (це баг, а не
runtime-ситуація; тест перевіряє всю таблицю).

Повна таблиця дозволених переходів (джерело істини — `core/models.py`,
`ALLOWED_TRANSITIONS`):

| З | До |
|---|---|
| QUEUED | RUNNING, CANCELLED, INTERRUPTED |
| RUNNING | SUCCESS, PARTIAL, FAILED, CANCELLED, INTERRUPTED, RETRY_PENDING |
| RETRY_PENDING | RUNNING, QUEUED, CANCELLED, INTERRUPTED |
| INTERRUPTED | QUEUED (Resume/Retry), CANCELLED (Ignore) |
| FAILED, PARTIAL, CANCELLED | QUEUED (лише ручна дія «Retry») |
| SUCCESS | — (абсолютно термінальний) |

**CANCELLED проти INTERRUPTED.** CANCELLED — лише рішення користувача (STOP,
CANCEL CURRENT JOB): тимчасові файли видаляються. Якщо ж робота зупиняється не
з волі користувача (GUI аварійно зник, програму вбито, вимкнулося живлення),
поточні й ще не розпочаті завдання стають INTERRUPTED: workspace із
перевіреними зображеннями та фрагментами зберігається, і після запуску
програма пропонує Resume / Retry / Ignore. Механізм — прапорець `interrupt`
у `CancellationToken`, який передається від пакета до кожного job.

`SUCCESS`, `PARTIAL`, `FAILED`, `CANCELLED` — термінальні для автоматики:
змінити їх може лише явна дія користувача. Ручний Retry для PARTIAL
створює **новий** вихідний файл і ніколи не перезаписує попередній.

**PARTIAL (DEGRADED)** — відео створене й пройшло верифікацію, але не з
повного набору матеріалів. Такий job не рахується як успішний: у GUI
окремий лічильник і жовта позначка, у `manifest.json` — список пропущених
файлів з причинами, у лозі — WARNING на кожен файл і підсумок, до імені
вихідного файлу додається суфікс ` [PARTIAL]` (налаштування
`partial_suffix`), щоб неповне відео не можна було сплутати з повним.

Ключова властивість: **запис у SQLite виконується ДО фактичної дії**
(write-ahead): спочатку `RUNNING/RENDERING`, потім запуск FFmpeg. Тому після
збою стан ніколи не «відстає» від дійсності в небезпечний бік.

### 5.2. Стан batch / Engine (в пам'яті Engine + дзеркало в SQLite)

```
 IDLE ─Start─► RUNNING ─Pause─► PAUSING ─(поточна атомарна операція завершена)─► PAUSED
                 ▲  │                                                              │
                 │  │◄──────────────────────────Resume─────────────────────────────┘
                 │  ├─ресурси нижче порогу─► RESOURCE_WAIT ─(ресурси відновлені)─► RUNNING
                 │  ├─Stop─► STOPPING ─(subprocess-и вбиті, cleanup)─► STOPPED ─► IDLE
                 │  └─черга порожня─► COMPLETED ─► IDLE
```

* `PAUSE` = завершити поточну **атомарну операцію** (один сегмент ≤ кількох
  секунд, або одне зображення), після чого не запускати наступну. FFmpeg ніколи
  не призупиняється посеред кодування (`SuspendThread` не використовується).
  Завдяки сегментному рендеру пауза настає за секунди, а не після всього відео.
* `STOP`: (1) прапорець `cancel_event`; (2) черга не видає нових job;
  (3) на активний subprocess — graceful (`q` у stdin FFmpeg), 3 с;
  (4) `terminate()`, 3 с; (5) `TerminateJobObject` + psutil-перевірка;
  (6) cleanup; (7) `CANCELLED`; (8) подія `BatchStateChanged(STOPPED)`.
  Загальна верхня межа STOP — `stop_deadline_s` (15 с); після неї Engine
  повідомляє GUI про неможливість завершити процес PID=… і продовжує, не
  чекаючи (Job Object усе одно вб'є процес при закритті хендлу).
* `CANCEL CURRENT JOB` — те саме, але тільки для поточного job; batch
  продовжується.

### 5.3. Стан зображення (у manifest)

`PENDING → VALID → NORMALIZED(decoder=pillow|opencv|ffmpeg)` або
`→ INVALID(reason_code, message)`.

Політика (відповідь на Q3): спочатку ланцюг резервних декодерів (§6.2).
Лише якщо всі вони не впоралися, зображення стає `INVALID`: запис у
manifest (`status=INVALID`, `reason_code`, `message`, `decoders_tried`) і
WARNING у лог, лічильник «пропущені файли» +1, його час рівномірно
перерозподіляється (детерміновано, §9.3), а job завершується як **PARTIAL**,
а не SUCCESS. Якщо валідних зображень менше `min_valid_images` (1) — job
`FAILED(INPUT)`. Налаштування `on_invalid_image = skip_as_partial | fail_job`
(за замовчуванням `skip_as_partial`).

**Відновлені зображення.** Якщо основний декодер (Pillow) відхилив файл
через пошкоджені дані (обрізаний, битий, завис чи впав), а резервний декодер
усе ж його прочитав, зображення використовується, але отримує позначку
`recovered=true`: резервні декодери «домальовують» пошкоджені ділянки
(наприклад, сірою заливкою). Такий job також завершується як **PARTIAL**
(degraded) із переліком відновлених файлів. Якщо Pillow не прочитав файл з
причини, не пов'язаної з пошкодженням (наприклад, непідтримувана
особливість формату), результат резервного декодера вважається повноцінним.

---

## 6. Конвеєр зображень

### 6.1. Валідація (у ImageWorker, timeout на файл)

1. Існування, звичайний файл (не каталог, не посилання за межі), доступ на читання
   (`open(..., 'rb')` з обробкою `PermissionError` — файл може бути заблокований
   іншою програмою; 2 повторні спроби з паузою 0,5 с — це transient).
2. Розмір > 0 і ≤ `max_image_file_bytes` (за замовчуванням 200 МБ).
3. Сигнатура вмісту (magic bytes) → фактичний формат; розбіжність з розширенням —
   WARNING у лог, але не помилка (обробляємо за фактичним форматом).
   Невідома сигнатура → `INVALID(UNSUPPORTED_FORMAT)`.
4. `Image.open()` (лише заголовок) → width, height, mode, наявність alpha, EXIF.
5. Ліміт розмірів: `width*height ≤ max_image_pixels` (за замовчуванням 100 Мп),
   кожна сторона ≤ 30000. Перевищення → спроба зменшеного декодування
   (`draft()` для JPEG); інакше `INVALID(TOO_LARGE)`.
   `Image.MAX_IMAGE_PIXELS` встановлюється явно; `DecompressionBombWarning`
   перетворюється на помилку.
6. `img.verify()` на окремому відкритті, потім повне `img.load()` — виявляє
   обрізані файли. `ImageFile.LOAD_TRUNCATED_IMAGES = False` (не приховуємо
   пошкодження).

### 6.2. Нормалізація — ланцюг декодерів

```
TRY  Pillow:  open → exif_transpose (з try; битий EXIF → ігнорувати орієнтацію + WARNING)
              → ICC→sRGB (ImageCms; помилка → WARNING, без перетворення)
              → режим: P/LA/RGBA → композиція на фон (колір з налаштувань) → RGB;
                       I;16/I/F → масштабування до 8 біт; CMYK → RGB
              → компонування полотна (§6.4) з LANCZOS, у 2 етапи для великих
              → JPEG q=95, 4:4:4, без EXIF/ICC/XMP → norm/i00001.jpg
FAIL→ OpenCV: cv2.imdecode(np.fromfile(path), IMREAD_COLOR | IMREAD_IGNORE_ORIENTATION)
              (np.fromfile — бо cv2.imread не працює з Unicode-шляхами на Windows)
              → той самий resize/запис
FAIL→ FFmpeg: ffmpeg -i <копія у workspace з ASCII-іменем> -frames:v 1 -vf scale+pad → .jpg
              (окремий subprocess з timeout 30 с)
FAIL→ INVALID(reason) + запис у лог + подія ImageSkipped
```

Кожна спроба загорнута в `try/except Exception` всередині ImageWorker;
`MemoryError` і нативний збій процесу обробляються Engine: процес помер /
timeout → поточне зображення одразу переходить до наступного декодера **в новому
процесі** (не повторюємо той самий декодер, що вже вбив процес).

Після запису нормалізованого файлу ImageWorker повторно відкриває його
(`verify`) — інакше джерело ще раз вважається проблемним.

### 6.3. Протокол ImageWorker

* Запит: `NormalizeRequest(index, src_path, dst_path, target_w, target_h, fit_mode, bg_color, decoders=[...])`.
* Відповідь: `NormalizeResult(index, ok, decoder_used, width, height, warnings, error_code, error_message)`.
* Обмін — через `multiprocessing.Pipe` (дуплекс); Engine чекає відповідь через
  `conn.poll(timeout)` — без нескінченного очікування.
* Timeout на зображення: `image_timeout_base_s (10) + pixels / 5e6 * 2 с`,
  максимум `image_timeout_max_s (120)`.
* Обробка строго по одному зображенню; у пам'яті ImageWorker одночасно
  максимум один декодований об'єкт; після кожного — `close()`, `del`,
  кожні 50 зображень — `gc.collect()`.

### 6.4. Компонування кадру з розмитим фоном (відповідь на Q4)

Нормалізоване зображення — це готове **полотно** розміру
`W·overscan × H·overscan` (overscan = 1,15 — запас для руху Ken Burns, §8.1.1),
а не просто перемасштабований оригінал. Логіка залежить від співвідношення
сторін джерела `r_src` і кадру `r_dst`:

1. `|r_src / r_dst − 1| ≤ cover_tolerance` (0,10) — зображення майже
   збігається з кадром: масштаб «cover», обрізається ≤ 10 % по одній осі.
2. Інакше — «contain + blur»: передній план повністю видимий (contain);
   фон = те саме зображення, масштабоване «cover», зменшене в 8 разів,
   GaussianBlur(radius≈20), затемнене до 60 %, збільшене назад
   (розмиття на зменшеній копії — у ~60 разів дешевше і передбачуване за
   пам'яттю).

Окремі правила для орієнтацій:

| Режим | Джерело | Поведінка |
|---|---|---|
| 16:9 | горизонтальне | майже завжди варіант 1 |
| 16:9 | вертикальне/квадратне | передній план по висоті, розмиті бічні поля |
| 9:16 | вертикальне | майже завжди варіант 1 |
| 9:16 | горизонтальне | передній план по ширині **з помірним збільшенням**: щоб не лишалась вузька смуга посередині, допускається обрізання країв до `vertical_max_crop` (15 %) по ширині; решта — розмитий фон зверху/знизу |

Для `9:16` з горизонтальним джерелом рух Ken Burns обмежується панорамою
вздовж ширини переднього плану (не виходить за видиму частину).

---

## 7. Watchdog і timeout-и

### 7.1. Два незалежні поняття

* **RUNNING** — процес живий (`poll() is None`).
* **MAKING PROGRESS** — за останні `stall_timeout` змінився хоча б один
  сигнал прогресу.

Сигнали прогресу (будь-який скидає таймер стагнації):

| Сигнал | Джерело |
|---|---|
| `frame` / `out_time_us` зросли | `-progress pipe:1` FFmpeg |
| розмір вихідного файлу зріс | `os.stat` раз на 2 с |
| CPU time процесу зріс більш ніж на 0,5 с | `psutil.Process.cpu_times()` |
| нова відповідь ImageWorker | pipe |
| файлова активність workspace (mtime) | `os.scandir` каталогу виводу |

Примітка: CPU-активність сама по собі **не** вважається достатньою на
довгому інтервалі — процес, що крутиться в циклі на 100 % CPU без зростання
`frame`/розміру файлу протягом `2 × stall_timeout`, також визнається
завислим (livelock).

### 7.2. Формули timeout (без «магічних» констант)

Позначення: `F` — кадрів у сегменті, `fps_min` — мінімальна очікувана швидкість
кодування (калібрується, §7.4), `D` — тривалість аудіо, с.

| Операція | Hard timeout | Stall timeout |
|---|---|---|
| Нормалізація зображення | `10 + Mpx·0,4` с, ≤ 120 | — (немає проміжних сигналів; лише hard) |
| ffprobe | `15 + size_GB·10` с | — |
| Нормалізація аудіо | `20 + D / speed_min_audio(20×) · 3` | 30 с |
| Рендер сегмента | `15 + F / fps_min · 3` | `max(20, 5 · F / fps_min / 10)` |
| Mux (concat copy + AAC) | `30 + D / 10 · 3` | 30 с |
| Верифікація (повне декодування) | `30 + D / 20 · 3` | 30 с |
| Архівування | `30 + bytes / 20 МБ/с · 3` | 30 с (розмір архіву) |
| Cleanup каталогу | `10 + files · 0,05` с | — |

Усі коефіцієнти — у `config/settings.py` (`TimeoutPolicy`), одне місце.

### 7.3. Ескалація

```
stall/hard timeout виявлено
  1. log WARNING  "job=… stage=… pid=… no progress for N s"
  2. diagnostic snapshot (§13.3)
  3. graceful: FFmpeg ← "q\n" у stdin; ImageWorker ← Shutdown        чекати 3 с
  4. terminate() (на Windows = TerminateProcess)                    чекати 3 с
  5. TerminateJobObject(job_handle) + psutil kill усіх нащадків      чекати 5 с
  6. перевірка: psutil.pid_exists / статус zombie для кожного PID дерева
     ще живий → CRITICAL у лог + подія GUI; продовжуємо (хендл Job Object
     закривається → ОС вб'є процес при першій нагоді)
  7. видалення тимчасового виходу операції
  8. операція → TimeoutError_ → класифікація → retry або FAILED
```

Watchdog — це потік у Engine на кожен активний subprocess; він не має
`while True`: цикл `while not done_event.wait(poll_interval)` з додатковою
умовою `elapsed < hard_timeout + escalation_budget`. Після вичерпання бюджету
потік гарантовано виходить.

### 7.4. Калібрування швидкості

На першому job batch перший сегмент вимірює фактичний `fps`. `fps_min` для
решти = `max(config.fps_min_floor, measured_fps / 4)`. Це адаптує timeout до
повільних ПК без ручних налаштувань і не дозволяє їм стати безмежними.

---

## 8. Шар FFmpeg

### 8.1. Чому сегментний рендер

Замість одного довгого FFmpeg на все відео (стара проблема «нескінченного
рендеру») кожне нормалізоване зображення кодується в окремий сегмент з точною
кількістю кадрів:

```
ffmpeg -hide_banner -y          # без -nostdin: stdin потрібен для graceful 'q'
       -loop 1 -framerate <fps> -i norm/i00001.jpg
       -frames:v <F_i> -c:v libx264 -preset <preset> -crf <crf> -pix_fmt yuv420p
       -r <fps> -video_track_timescale <fps*512> -g <2*fps>
       -progress pipe:1 -nostats  seg/s00001.tmp.mp4
```

Після цього — фінальний mux:

```
ffmpeg -f concat -safe 0 -i seg/concat.txt -i audio/a.wav
       -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 192k
       -af apad -t <total_frames/fps> -movflags +faststart
       -progress pipe:1 -nostats  out/.<name>.tmp.mp4
```

Переваги (перевірено спайком на FFmpeg 6.1: 3 сегменти × 74 кадри + аудіо
7,37 с → відео 222 кадри, 7,400 с, аудіо 7,400 с):

* кожен subprocess короткий → точні timeout-и, швидке виявлення зависання;
* пауза і STOP спрацьовують на межі сегмента (секунди);
* відновлення після збою — з першого невідрендереного сегмента (сегменти
  верифікуються ffprobe і записуються в manifest);
* retry повторює один сегмент, а не всю годину рендеру;
* кадри ніколи не проходять через Python і не лежать у RAM;
* мінімум файлів на диску (один сегмент на зображення, а не сотні PNG-кадрів) —
  менше навантаження на Windows Defender.

#### 8.1.1. Рух (Ken Burns) і переходи (відповідь на Q2)

Рух і переходи реалізуються **всередині сегментів**, тому всі властивості
сегментного рендеру (короткі процеси, точні timeout-и, resume) зберігаються.

**Ken Burns.** Для кожного зображення детерміновано (seed = індекс + job_id)
обирається один з помірних рухів: zoom-in 1,00→1,08, zoom-out 1,08→1,00,
pan вліво/вправо/вгору/вниз з масштабом 1,06. Параметри — функція лише від
номера кадру, без випадковості під час рендеру. Реалізація:
`zoompan` по полотну з overscan (§6.4), яке перед фільтром збільшується до
2× цільової роздільності — це прибирає «тремтіння» через цілочислове
округлення координат. Альтернативи (`crop` з виразами + `scale`) будуть
порівняні бенчмарком у PHASE 5; критерій вибору — стабільність і
швидкість ≥ 2× реального часу на референсному ПК. Ефект вимикається в
налаштуваннях (`effects.ken_burns`).

**Переходи.** Crossfade тривалістю `T_x` (0,6 с, але ≤ 25 % кадрів
коротшого з двох сусідніх зображень). Перехід між зображеннями *i* та *i+1*
належить сегменту *i+1*: його FFmpeg отримує два входи (*i* у кінцевій фазі
руху, *i+1* у початковій) і `xfade`. Кількість кадрів сегментів далі
підсумовується рівно в `total_frames` (§9.3: перехід «позичає» кадри в
кінці зображення *i*, тому сегмент *i* коротшає на `T_x·fps`, а сума не
змінюється). Кожен сегмент — як і раніше, один короткий FFmpeg-процес.

**Захист від відомих проблем старої програми.** Ефекти ніколи не
виконуються в Python по кадрах; жодних кадрів на диску; фільтр має
фіксовану тривалість (`-frames:v F_i`), тому «нескінченна генерація»
неможлива навіть при помилці у виразі фільтра; watchdog і hard timeout
працюють так само.

### 8.2. FFmpegRunner

* `subprocess.Popen(argv: list[str], stdin=PIPE, stdout=PIPE, stderr=PIPE,
  creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, shell=False)`.
* Відразу після створення процес додається до Job Object (§8.4). Вікно гонки
  (процес створив дочірній процес до призначення Job Object) закривається
  тим, що Engine сам перебуває в Job Object з `KILL_ON_JOB_CLOSE`, а нащадки
  успадковують його автоматично.
* stdout (progress) і stderr читаються **двома окремими потоками-читачами**,
  кожен до EOF. stderr зберігається в `collections.deque(maxlen=400)` рядків —
  pipe ніколи не переповнюється, пам'ять обмежена.
* `ProcessResult(pid, argv, started_at, ended_at, returncode, stderr_tail,
  progress_last, killed_by_watchdog, kill_reason)`.
* Очікування завершення: `proc.wait(timeout=slice)` у циклі зі скінченною
  умовою (див. §7.3), ніколи `proc.communicate()` без timeout.
* `-loglevel warning` + `-progress pipe:1`: достатньо для діагностики і не
  засмічує pipe.
* Аргументи формуються лише функціями-будівельниками з типізованих параметрів;
  шляхи — `str(Path)`, відносні до `cwd=job_dir`. Ніякої конкатенації рядків
  для shell.

### 8.3. ProgressParser

Парсить блоки `key=value` з `-progress pipe:1`: `frame`, `fps`, `out_time_us`,
`speed`, `total_size`, `progress=continue|end`. Відомо `F` (кадри сегмента) і
загальна кількість кадрів job → відсоток = `(кадри завершених сегментів +
frame поточного) / total_frames`. Для mux — `out_time_us / (D·1e6)`.
`N/A` і пошкоджені рядки ігноруються без винятку.

ETA: ковзне середнє швидкості (кадрів/с) за останні 30 с на рівні batch;
для ще не почато job — оцінка з кількості кадрів. Показується лише після
≥ 10 с роботи (інакше «оцінюється…»).

### 8.4. ProcessManager і дерево процесів (Windows)

* `CreateJobObjectW` + `SetInformationJobObject(JobObjectExtendedLimitInformation,
  JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)`.
* GUI-процес створює Job Object для Engine; Engine — окремий Job Object на
  кожен job для своїх дочірніх процесів (вкладені Job Objects підтримуються з
  Windows 8). Наслідок: якщо GUI вбили з диспетчера задач — ОС вбиває Engine,
  ImageWorker і FFmpeg. **Сирітських FFmpeg не буває за конструкцією.**
* `kill_tree(pid)`: `TerminateJobObject`; потім `psutil.Process(pid).children(recursive=True)`
  → `kill()` → `psutil.wait_procs(timeout=5)`; повертає список вцілілих.
* На Linux/macOS (лише для розробки і CI): `start_new_session=True` + `os.killpg`.
  Інтерфейс однаковий.
* Реєстр живих процесів у Engine: `dict[pid, ProcessRecord]`. При завершенні
  Engine (будь-яким шляхом через `finally`/`atexit`) — kill усіх записів.
* При старті програми: пошук процесів `ffmpeg.exe`, чий батьківський PID
  записаний у `state.db` як колишній Engine (на випадок, якщо Job Object був
  недоступний, наприклад, під обмеженим антивірусом/політикою) — пропонувати
  завершити.

---

## 9. Пам'ять, аудіо та синхронізація

### 9.1. Пам'ять

* Engine зберігає про зображення лише метадані (`ImageItem`: шлях, індекс,
  статус, розміри) — ~200 байт на зображення; 1000 зображень ≈ 200 КБ.
* Жодних списків декодованих зображень або кадрів. Пікселі живуть лише в
  ImageWorker і лише для одного зображення одночасно.
* ResourceMonitor (потік Engine, інтервал 2 с): `available RAM`,
  RSS Engine + дочірніх, CPU, вільне місце на диску workspace і output.
* Пороги (налаштовувані):
  * `ram_available_min_mb` (1024) або RSS дерева > `ram_tree_max_mb` (3072) →
    batch → `RESOURCE_WAIT`: новий job/сегмент не стартує; поточний сегмент
    завершується (він короткий); ImageWorker перезапускається; `gc.collect()`;
    очікування відновлення з верхньою межею `resource_wait_max_s` (600 с),
    після якої поточний job → `FAILED(RESOURCE)` і batch продовжує спробу з
    наступним (якщо ресурсів так і немає — batch зупиняється з поясненням).
  * Disk: див. §9.4.
* Memory-leak тест (§TESTING): RSS Engine до/після кожного з 50 job; критерій —
  лінійна регресія приросту < 1 МБ/job, і RSS після 50 job < RSS після 5 job + 30 МБ.

### 9.2. Аудіо

1. ffprobe: наявність аудіопотоку, codec, sample rate, channels, metadata duration.
2. **Фактична тривалість** — повне декодування:
   `ffmpeg -i in -vn -ac 2 -ar 48000 -c:a pcm_s16le audio/a.wav` з `-progress`.
   Тривалість = розмір PCM-даних WAV / (48000·2·2). Metadata duration
   використовується лише для оцінки timeout і для порівняння (розбіжність > 2 %
   → WARNING у лог).
3. Помилки декодування (`stderr` містить «Invalid data», код ≠ 0) або
   тривалість < `min_audio_s` (0,5 с) → `FAILED(INPUT, INVALID_AUDIO)`, без retry.
4. Гучність: за замовчуванням не змінюється (Q5); опційно — `loudnorm` у
   двопрохідному режимі.

### 9.3. Детермінований таймлайн

```
total_frames = ceil(D · fps)              # відео ніколи не коротше аудіо
N            = кількість валідних зображень
base, rem    = divmod(total_frames, N)
F_i          = base + (1 if i < rem else 0)   # i = 0..N-1, перші rem отримують +1
```

* Σ F_i = total_frames рівно; результат залежить лише від (D, fps, N).
* Якщо `base < min_frames_per_image` (наприклад, 1000 зображень на 10 с аудіо)
  → job `FAILED(INPUT)` з поясненням «Зображень забагато для тривалості
  аудіо», а не відео з мерехтінням (поріг налаштовується; Q6).
* Аудіо добивається тишею (`apad`) до `total_frames / fps`; надлишок < 1 кадру
  (< 34 мс при 30 fps). Ні чорного кадру (останній сегмент має рівно F_N кадрів
  зображення), ні обрізання аудіо.
* Верифікація: `|video_dur − total_frames/fps| ≤ 1/fps`,
  `|audio_dur − video_dur| ≤ 1/fps + 0,05 с` (AAC priming).

### 9.4. Диск

Оцінка перед job:
`need = Σ norm_jpeg (≈ W·H·0,4 байта) + wav (D·192000) + сегменти (D·bitrate_est)
+ вихід (D·(bitrate_est + 192 кбіт/с)) · 2 + reserve (1 ГБ)`.
Перевіряється окремо для томів workspace і output (можуть збігатися — тоді
сума). Недостатньо → job не стартує, `FAILED(DISK)` з повідомленням
**«Недостатньо вільного місця на диску.»** + скільки потрібно / скільки є;
batch переходить у `PAUSED` (не прогорає решту job із тією ж помилкою).
Під час batch ResourceMonitor перевіряє вільне місце; < reserve → `RESOURCE_WAIT`.

---

## 10. Atomic output і верифікація

1. Mux пише в `job/out/.<name>.tmp.mp4` (той самий том, що й workspace).
2. Верифікація тимчасового файлу:
   * `returncode == 0` і watchdog не втручався;
   * файл існує, розмір ≥ `min_output_bytes` (10 КБ) і правдоподібний
     (≥ D · 10 кбіт/с);
   * `ffprobe -show_format -show_streams -count_packets`: контейнер mp4,
     рівно 1 відеопотік h264 потрібної роздільності, 1 аудіопотік (aac,
     48 кГц, 2 канали), `duration > 0`;
   * кількість відеопакетів == `total_frames`;
   * тривалості в межах допусків §9.3;
   * повне декодування `ffmpeg -v error -i tmp -f null -` (з timeout і
     watchdog) — порожній stderr; інакше `VerificationError`.
3. Копіювання/переміщення у вихідну папку:
   * той самий том → `os.replace(tmp, <output>/.<name>.part)`;
   * інший том → потокове копіювання блоками 8 МБ у `.part` з `fsync`,
     потім звірка розміру і SHA-256;
   * `os.replace(.part, final)` — атомарно; при `PermissionError` (файл
     заблоковано антивірусом/провідником) — до 5 спроб з паузами 0,2…2 с.
4. Лише після успішного `os.replace` — `SUCCESS` у SQLite з `output_file`,
   `output_sha256`, `actual_duration`.
5. Будь-яке переривання → `.tmp`/`.part` видаляються; при старті програми
   залишкові `.part` у output, що не згадуються в SQLite як SUCCESS,
   видаляються (тільки файли з нашим префіксом і суфіксом `.part`).

**Готові результати ніколи не змінюються**: `SUCCESS`-job не перерендерюється
при Resume, а файл з таким іменем ніколи не перезаписується.

---

## 11. Помилки, класифікація і retry

| Клас (`core/errors.py`) | Приклади | Retry | Примітка |
|---|---|---|---|
| `InputError` | битий/відсутній аудіо, 0 валідних зображень, забагато зображень | 0 | Повідомлення з номером файлу і причиною |
| `TransientError` | `PermissionError` при читанні, тимчасова недоступність мережевого диска | 2 | Затримка 1 с, 3 с (скінченна) |
| `FFmpegCrashError` | код ≠ 0, аварійне завершення | 1 | Повторюється лише невдала операція (сегмент/mux) |
| `TimeoutError_` | hard/stall timeout | 1 | Із чистим каталогом операції |
| `VerificationError` | вихід не пройшов перевірку | 1 | Повторюється mux і верифікація |
| `ResourceError` | диск, RAM | 0 (очікування, не retry) | Див. §9.1, §9.4 |
| `FFmpegUnavailableError` | ffmpeg не знайдено / не запускається | 0 | Блокує START з поясненням |
| `CancelledError_` | STOP / CANCEL CURRENT JOB | 0 | → CANCELLED |
| `InternalError` | будь-який інший виняток | 0 | CRITICAL + traceback; job FAILED, batch продовжується |

* Retry ітеративний (цикл `for attempt in range(max_attempts)`), **ніколи
  рекурсивний**. Лічильник `attempts` у SQLite — переживає перезапуск;
  після збою програми INTERRUPTED → Resume не обнуляє лічильник.
* Повідомлення для користувача формує `UserMessage` з шаблонів, наприклад:
  «Не вдалося прочитати зображення №17. Файл: image_017.png. Причина:
  пошкоджений PNG (файл обрізано).» Технічний traceback — лише в логах.

---

## 12. Архівування

Призначення (відповідь на Q7): після SUCCESS/PARTIAL створюється
**пакет job** `output/_archive/<job_name>.zip`, достатній для перевірки або
повторного створення відео:

| Вміст | Включено |
|---|---|
| `manifest.json`, `job.log`, `settings_snapshot.json` | так |
| оригінальні вхідні зображення і аудіо | так (`archive.include_inputs`, true) |
| фінальне відео | ні за замовчуванням (`archive.include_video`, false): відео вже лежить в `output/`, а дублювання подвоює розмір архіву — це одна з відомих проблем старої програми; у manifest зберігаються ім'я та SHA-256 відео для перевірки |
| нормалізовані зображення, сегменти, WAV | **ніколи** — це тимчасові render-файли, вони відтворюються з оригіналів |

Перед стартом оцінюється розмір архіву; якщо він більший за
`archive.max_size_mb` (4096) — архів не створюється, job отримує WARNING,
а не мовчазне створення гігантського файлу. Реалізація:

* `zipfile.ZipFile(path + '.part', 'w', ZIP_STORED для jpg/png/mp4/mp3, DEFLATED
  для текстів, allowZip64=True)`; додавання файлів по одному через
  `zf.write()` (потоково, блоками) — архів ніколи не будується в RAM.
* Виконується в окремому процесі (може бути довгим, вбивається watchdog-ом за
  ростом розміру `.part`).
* Перевірка: файл існує; розмір > 0; `ZipFile.testzip()` = None; кількість
  записів і сумарний розмір відповідають очікуваним. Тоді — `os.replace`.
* Невдача архівування → job `FAILED(ARCHIVE)` (згідно з вимогою: не
  вважати job завершеним); відео при цьому вже атомарно записане й лишається
  (у manifest зазначено «відео готове, архів не створено»; Retry повторює
  лише архівування).

---

## 13. Логування і діагностика

### 13.1. Схема

* Кожен процес (Engine, ImageWorker, архіватор) пише через `QueueHandler`
  у спільну `multiprocessing.Queue`; єдиний `QueueListener` в Engine пише у файли
  — немає конкурентного запису в один файл з кількох процесів (на Windows
  `RotatingFileHandler` з кількох процесів ламає ротацію).
* Файли (`RotatingFileHandler`, 10 МБ × 5 копій, UTF-8):
  `application.log` (INFO+, DEBUG за налаштуванням), `errors.log` (WARNING+).
* Per-job: `diagnostics/<job_id>/job.log` (JSON Lines: `ts, level, job_id,
  stage, event, duration_ms, error, traceback`), створюється лише якщо job
  не SUCCESS або увімкнено `keep_job_logs`.
* Формат рядка: `2026-10-03T12:00:00.123+03:00 INFO  job=j0001 stage=RENDERING
  event=segment_done seg=17 dur_ms=812 | повідомлення`.
* GUI отримує тільки INFO+ через події (з throttling) і має кнопку OPEN LOG.

### 13.2. Ротація діагностики

`diagnostics/` обмежується `diagnostics_max_jobs` (200) і `diagnostics_max_mb`
(500): найстаріші каталоги видаляються під час запуску Engine.

### 13.3. Diagnostic snapshot

`snapshot.json`: час, job_id, stage, argv, PID, дерево процесів зі статусом,
CPU-час, RSS, хвіст stderr, останній блок progress, розмір і mtime вихідного
файлу, вільна RAM/диск, налаштування timeout-ів, версія програми і FFmpeg.

---

## 14. Crash recovery

При старті Engine (до прийому команд):

1. `PRAGMA integrity_check` на `state.db`; пошкоджено → копія в
   `state.db.corrupt-<ts>`, відновлення списку job з `manifest.json` у workspace.
2. Усі `RUNNING` і `RETRY_PENDING` → `INTERRUPTED` (одна транзакція), фіксація `interrupted_at`.
3. Для кожного INTERRUPTED: перевірка workspace (наявні верифіковані сегменти
   за manifest), очищення `.tmp`/`.part`.
4. Подія `InterruptedJobsFound([...])` → GUI показує діалог зі списком:
   **Resume** (продовжити з першого невідрендереного сегмента — сегменти
   повторно перевіряються ffprobe), **Retry** (почати job з нуля, чистий
   workspace), **Ignore** (→ CANCELLED, cleanup workspace).
5. SUCCESS-вихідні файли не чіпаються; перевірка лише їх існування
   (зниклий файл → WARNING у лог, статус не змінюється).

---

## 15. Cleanup

* Кожен job: `try: pipeline… finally: cleanup(job_dir)`; cleanup сам
  загорнутий у `try/except` і не кидає винятків назовні.
* `cleanup.remove_tree(path, deadline)`: обхід знизу вгору, `os.chmod` для
  read-only, повтор `PermissionError` до 3 разів з паузою 0,2–1 с (антивірус),
  скінченний дедлайн; нездатні до видалення файли → список у лог і в
  таблицю `pending_cleanup` у SQLite → повторна спроба при наступному запуску.
* Write-ahead: workspace job потрапляє в `pending_cleanup` у тій самій
  транзакції, що й підсумковий статус (SUCCESS / PARTIAL / FAILED /
  CANCELLED), і знімається звідти лише після успішного видалення. Аварія між
  статусом і видаленням не залишає workspace назавжди: його прибирає
  наступний запуск. Workspace INTERRUPTED-завдань (потрібні для Resume) не
  видаляються ніколи.
* Видалення виконується тільки всередині `<workspace>/vg-*` (захисна
  перевірка `path.resolve().is_relative_to(workspace_root)` + маркер-файл
  `.videogen-workspace` у кореневому каталозі batch) — захист від видалення
  користувацьких даних через помилку в шляху.
* Діагностика зберігається окремо в `<appdata>/diagnostics` до cleanup.

---

## 15a. Один екземпляр програми

Engine бере ексклюзивне блокування `<appdata>/engine.lock` (`fcntl`/`msvcrt`).
Другий екземпляр отримує зрозуміле повідомлення «Програма вже запущена».
Блокування прив'язане до відкритого файлу, тому ОС знімає його при будь-якому
завершенні процесу — «завислого» блокування після збою не буває.

Усі точки входу, що створюють процеси (`main.py`, `engine_main.py`, тестові
драйвери), викликають `multiprocessing.freeze_support()` і мають захист
`if __name__ == "__main__"`: у режимі spawn дочірній процес повторно імпортує
головний модуль.

## 16. Concurrency

* За замовчуванням: 1 job одночасно, 1 FFmpeg одночасно, 1 ImageWorker.
* Конвеєризація, що не збільшує ризик: поки FFmpeg кодує сегмент *i*,
  ImageWorker нормалізує зображення *i+1…i+k* (k = `prefetch_images`, 4) —
  обмежений буфер на диску, не в RAM.
* `max_parallel_jobs` (1–4) — налаштування; кожен паралельний слот
  дозволяється ResourceMonitor лише за наявності запасу RAM і CPU < 85 %.
  `-threads` FFmpeg розподіляється між слотами.
* Engine — однопотоковий планувальник + потоки-читачі pipe + потоки
  watchdog; спільний стан захищений одним `threading.Lock` у `JobManager`;
  події між потоками — лише через `queue.Queue` / `threading.Event`.
  Порядок захоплення блокувань документується; вкладених блокувань немає.

---

## 17. Відмови і реакції (failure modes)

| # | Відмова | Виявлення | Реакція |
|---|---|---|---|
| F1 | FFmpeg завис (0 % CPU) | stall timeout | ескалація §7.3, retry 1 |
| F2 | FFmpeg livelock (100 % CPU, без кадрів) | stall: frame/size не ростуть | те саме |
| F3 | FFmpeg аварійно завершився | returncode ≠ 0 | retry 1 сегмента |
| F4 | FFmpeg не знайдено | locator при запуску | START заблоковано, пояснення |
| F5 | Декодер зависає/segfault | timeout/смерть ImageWorker | наступний декодер у новому процесі |
| F6 | Битий/перейменований/0-байт файл | валідація | INVALID, пропуск |
| F7 | Битий аудіо | декодування | job FAILED(INPUT) |
| F8 | Немає місця | оцінка/моніторинг | не стартувати / RESOURCE_WAIT |
| F9 | Мало RAM | ResourceMonitor | RESOURCE_WAIT, перезапуск ImageWorker |
| F10 | Engine аварійно завершився | GUI: `is_alive()`/heartbeat | ОС вбиває нащадків (Job Object); GUI пропонує перезапуск; INTERRUPTED |
| F11 | GUI вбили | — | Job Object вбиває все; recovery при старті |
| F12 | Втрата живлення | — | SQLite WAL + write-ahead стан; recovery |
| F13 | Файл виходу заблоковано | `PermissionError` на replace | обмежені повтори, потім FAILED з поясненням |
| F14 | Повільний антивірус | великі stall-інтервали на I/O | мінімум файлів; рекомендація виключення в TROUBLESHOOTING |
| F15 | Довгі шляхи > 260 | `utils/paths` | `\\?\`-префікс для Python I/O; ASCII-імена в workspace; маніфест PyInstaller `longPathAware` |
| F16 | Переповнення pipe | — (запобігання) | окремі потоки-читачі до EOF |
| F17 | Переповнення черги подій | `queue.Full` | відкидання лише throttled-подій (інші чекають місця до 5 с); якщо GUI вже зник — жодного очікування, подія відкидається одразу; SQLite — джерело істини |
| F18 | Пошкоджений state.db | integrity_check | відновлення з manifest |
| F19 | Виняток у cleanup | try/except | pending_cleanup, повтор при старті |
| F20 | Помилка архівування | перевірка архіву | FAILED(ARCHIVE), відео збережене |

---

## 18. Аудит циклів (інваріант для code review)

Правило: у коді **немає** `while True`. Дозволені форми:

* `for … in range(max_n)` / ітерація скінченної колекції;
* `while not stop_event.wait(interval):` **разом** з перевіркою дедлайну;
* `while proc.poll() is None and time.monotonic() < deadline:`.

Перевірка автоматизована: тест `tests/unit/test_code_invariants.py` проходить
AST усього пакета і падає на `while True`/`while 1`, `shell=True`,
`communicate()` без `timeout`, `.wait()`/`.join()`/`.get()` без `timeout`
у модулях Engine, `except: pass`, рекурсивні виклики в `retry`-функціях.
Винятки — лише з явним коментарем-позначкою `# invariant-ok: <причина>`.

---

## 19. Конфігурація

`config/settings.py` — ієрархія frozen-dataclass:

```
Settings
  video:      orientation(H|V), resolution(1920x1080 | 1080x1920), fps(30), crf(20), preset(medium), fit_mode, bg_color
  audio:      sample_rate(48000), channels(2), aac_bitrate(192k), loudnorm(False)
  images:     max_pixels, max_file_bytes, on_invalid(skip|fail_job), min_valid_images(1), min_frames_per_image
  timeouts:   TimeoutPolicy (усі коефіцієнти §7.2), stall_poll_s, escalation waits
  retry:      transient=2, ffmpeg_crash=1, timeout=1, verification=1
  resources:  ram_available_min_mb, ram_tree_max_mb, disk_reserve_mb, cpu_max_pct, resource_wait_max_s
  concurrency: max_parallel_jobs(1), prefetch_images(4), image_worker_recycle_after(200)
  paths:      input, output, workspace, appdata
  cleanup:    keep_failed_workspace(False), diagnostics_max_jobs, diagnostics_max_mb
  logging:    level, max_bytes, backup_count, keep_job_logs
  archive:    enabled, target(Q7)
```

Збереження — `settings.json` атомарним записом; невідомі/невалідні ключі →
значення за замовчуванням + WARNING (програма не падає через зіпсований
файл налаштувань). Знімок Settings фіксується в `manifest.json` кожного job.

---

## 20. Manifest

`manifest.json` (атомарний запис: `tmp` → `flush` → `fsync` → `os.replace`):

```
{ "schema": 1, "job_id", "batch_id", "job_name", "created_at", "software_version",
  "ffmpeg_version", "mode", "orientation", "resolution", "fps",
  "input_dir", "input_files": [{"index","path","size","sha256","status","decoder","reason"}],
  "audio_file", "audio_sha256", "audio": {codec, sample_rate, channels, metadata_duration, decoded_duration},
  "timeline": {"total_frames", "frames_per_image": [...]} ,
  "segments": [{"index","frames","status","size"}],
  "expected_duration", "actual_duration", "status", "stage", "attempts",
  "start_time", "end_time", "error": {"class","code","message"}, "output_file", "output_sha256",
  "settings_snapshot": {...} }
```

Хеші рахуються потоково (блоки 1 МБ) в ImageWorker під час валідації, а не
окремим проходом. Для дуже великих файлів (> 500 МБ) — `sha256` перших і
останніх 16 МБ + розмір (позначено `"hash_mode": "partial"`).

---

## 21. MODE B (Script + Image Prompts → Voice + Images → Video)

Архітектурно MODE B — це **генерувальний префікс** до MODE A:

```
script.txt + prompts.txt ─► TTS provider ─► audio/voice.wav
                         ─► Image provider ─► gen/i00001.png …
                         ─► (далі ідентично MODE A, починаючи з VALIDATION)
```

* `providers/base.py`: `TTSProvider.synthesize(text, out_path, timeout) -> AudioResult`,
  `ImageGenProvider.generate(prompt, out_path, size, timeout) -> ImageResult`.
  Кожен виклик — з timeout, класифікацією помилок (мережа = Transient, 2 retry;
  відмова контент-фільтра = Input, 0 retry), записом у manifest.
* Стара програма використовувала **ElevenLabs** (озвучка) та **OpenAI**
  (зображення). Вони реалізуються як окремі адаптери
  `providers/elevenlabs_tts.py` і `providers/openai_images.py`, які
  реєструються в `providers/registry.py`; Core і pipeline залежать лише від
  інтерфейсів `base.py` і нічого не знають про конкретні сервіси.
* Мережеві виклики — з timeout на з'єднання і читання, обмеженими retry
  (2, з backoff 2/6 с), потоковим записом відповіді на диск, перевіркою
  результату (аудіо → §9.2, зображення → §6) і кешуванням за хешем
  (текст/промпт + параметри), щоб повтор job не генерував і не оплачував те
  саме вдруге. Ключі API — у Windows Credential Manager (не у `settings.json`
  і не в логах).
* До окремої фази MODE B GUI показує режим, але блокує START з поясненням
  «Провайдер не налаштований».

---

## 22. Безпека шляхів і Unicode

* Усі шляхи — `pathlib.Path`; у subprocess передаються як окремі елементи
  argv (`list[str]`), `shell=False` скрізь.
* Python на Windows використовує Unicode API — українські імена, пробіли,
  `'&#%+()` безпечні. FFmpeg отримує лише ASCII-шляхи з workspace
  (виняток: аудіо-джерело читається FFmpeg напряму — `subprocess` на Windows
  передає argv як UTF-16 через `CreateProcessW`, FFmpeg приймає UTF-8/широкі
  аргументи; додатковий захист — при помилці відкриття джерело копіюється в
  workspace під ASCII-іменем і повторюється).
* `concat.txt` містить лише відносні ASCII-імена `s00001.mp4`, тому
  екранування лапок не потрібне.
* Довгі шляхи: `utils/paths.long_path()` додає `\\?\` для операцій Python із
  шляхами > 240 символів; EXE має маніфест `longPathAware`.
* Безпечне ім'я вихідного файлу: заборонені символи Windows `<>:"/\|?*`,
  керівні символи, зарезервовані імена (`CON`, `NUL`, `COM1`…), кінцеві
  крапки/пробіли замінюються; довжина обмежується 150 символами.

---

## 23. Відповідність вимогам (трасування)

| Вимога (номер у ТЗ) | Розділ |
|---|---|
| 1, 41 GUI не блокується | §3, §3.1 |
| 4, 5, 33 Watchdog, stall | §7 |
| 6–8, 34 Зображення | §6 |
| 9, 10 Пам'ять, потокові кадри | §8.1, §9.1 |
| 11–13 FFmpeg, progress, kill tree | §8 |
| 14, 15 Atomic output, верифікація | §10 |
| 16, 17 Аудіо, синхронізація | §9.2, §9.3 |
| 18, 19 Batch, ізоляція помилок | §4, §5, §11 |
| 20 Retry | §11 |
| 21 Cleanup | §15 |
| 22, 23 Recovery, manifest | §14, §20 |
| 24, 25 Логування | §13 |
| 26 Шляхи | §22 |
| 27 Concurrency | §16 |
| 28–31 Шари, типи, конфігурація | §2, §19 |
| 32 Timeout-и | §7.2 |
| 35 Архіви | §12 |
| 36, 37 Диск, RAM | §9 |
| 38 Цикли | §18 |
| 39, 40 STOP, PAUSE | §5.2 |
| 47 Повідомлення | §11 |
