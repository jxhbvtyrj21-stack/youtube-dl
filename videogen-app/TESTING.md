# Тестування

## Як запускати

```bash
pip install -e ".[media,gui,dev]"          # Python 3.11+, ffmpeg/ffprobe у PATH

python -m pytest                            # усі автоматичні тести (без стрес-тестів)
python -m pytest -m stress tests/stress     # стрес: 100/500/1000 зображень, 50 jobs, GUI під навантаженням
VIDEOGEN_STRESS_SIZES=100 python -m pytest -m stress tests/stress   # швидкий варіант
```

* GUI-тести працюють без дисплея (`QT_QPA_PLATFORM=offscreen` встановлюється автоматично).
* Кожен тест має власний timeout (`pytest-timeout`): тест не може зависнути назавжди.
* Наприкінці кожного прогону автоматично перевіряється, що **не лишилося
  жодного дочірнього процесу** (`tests/conftest.py`), а після кожного тесту
  FFmpeg-нагляду — що реєстр живих процесів порожній.
* Тестові медіафайли генеруються програмно (`tests/fixtures/factory.py`);
  бінарних файлів у репозиторії немає.
* Стрес-тести пишуть звіти в `tests/stress/reports/` (JSON/CSV).

## Структура

| Каталог | Що перевіряє |
|---|---|
| `tests/unit/` | стани, налаштування, помилки й повтори, SQLite-стан, черга, логування, сховище, інваріанти коду (AST-аудит) |
| `tests/media/` | валідація й нормалізація зображень, ImageWorker, аудіо, таймлайн, верифікація відео, архів, запуск інструментів |
| `tests/ffmpeg/` | розбір прогресу, watchdog, нагляд за FFmpeg (імітатор `tests/fake_ffmpeg.py`), ресурси |
| `tests/integration/` | повний конвеєр зі справжнім FFmpeg, вбивство процесу й відновлення, стійкість бази до SIGKILL |
| `tests/gui/` | модель вікна, головне вікно, наскрізні сценарії GUI + Engine + FFmpeg |
| `tests/stress/` | великі пакети, витоки пам'яті, швидкість реакції GUI під навантаженням |

## Відповідність сценаріям ТЗ §42

| № | Сценарій ТЗ | Тест(и) |
|---|---|---|
| 1 | valid JPG | `media/test_images.py::test_valid_formats[jpg]`, `test_every_decoder_handles_every_format` |
| 2 | valid PNG | `test_valid_formats[png]`, `test_every_decoder_handles_every_format` |
| 3 | valid WEBP | `test_valid_formats[webp]`, `test_every_decoder_handles_every_format` |
| 4 | corrupted JPG | `test_corrupted_images_fail_cleanly_with_pillow`, `test_truncated_jpeg_is_not_silently_accepted`, `media/test_image_worker.py::test_batch_with_several_bad_images_continues` |
| 5 | corrupted PNG | ті самі + `integration/test_pipeline.py::test_bad_images_give_partial_not_success` |
| 6 | renamed extension | `test_extension_is_not_trusted`, `test_every_decoder_handles_every_format[png_named_jpg]` |
| 7 | zero-byte file | `test_rejected[EMPTY]`, `test_batch_with_several_bad_images_continues` |
| 8 | huge image | `test_decompression_bomb_png_rejected`, `test_huge_jpeg_decoded_via_draft`, `test_huge_non_jpeg_rejected_by_pillow` |
| 9 | transparent PNG | `test_unusual_images[transparent_png/palette_png]`, `test_transparent_png_has_no_alpha_artifacts` |
| 10 | unusual EXIF | `test_exif_orientation_applied`, `test_unusual_images[broken_exif_jpg]` |
| 11 | Unicode filename | `test_unicode_and_long_names`, `test_valid_audio_formats` (Unicode-імена аудіо), `integration::test_single_job_success` (Unicode-назва папки й результату) |
| 12 | long filename | `test_unicode_and_long_names`, `unit/test_storage.py::test_unicode_and_long_paths_roundtrip`, `test_safe_filename_truncates_long_names` |
| 13 | missing input | `integration::test_missing_input_folder`, `test_empty_input_folder`, `test_input_file_vanishes_after_discovery` |
| 14 | missing audio | `integration::test_one_bad_job_does_not_stop_batch` (d_no_audio), `media::test_missing_audio` |
| 15 | corrupted audio | `media::test_corrupted_audio`, `test_video_without_audio_track`, `test_truncated_audio_uses_real_duration`, `integration::test_one_bad_job_does_not_stop_batch` (b_bad_audio) |
| 16 | FFmpeg unavailable | `media/test_runner.py::test_locator_reports_unavailable`, `test_locator_rejects_broken_binary`, `integration::test_ffmpeg_unavailable` |
| 17 | FFmpeg timeout | `ffmpeg/test_supervision.py::test_hung_process_is_detected_and_killed[hang/livelock]`, `test_hard_timeout`, `integration::test_ffmpeg_hang_and_crash_are_bounded[hang]` |
| 18 | FFmpeg crash | `test_crash_is_reported_as_crash`, `integration::test_ffmpeg_hang_and_crash_are_bounded[crash]` |
| 19 | insufficient disk | `integration::test_insufficient_disk_preflight`, `test_low_resources_wait_is_bounded`, `ffmpeg::test_disk_check`, `unit::test_disk_full_pauses_batch` |
| 20 | cancellation | `integration::test_stop_mid_render`, `unit/test_queue_manager.py::test_stop_*`, `test_cancel_current_job_continues_batch`, `ffmpeg::test_cancel_stops_run`, `test_real_ffmpeg_cancel_mid_encode`, `gui/test_gui_e2e.py::test_gui_responsive_while_ffmpeg_hangs_and_stop_works` |
| 21 | batch with one bad image | `integration::test_bad_images_give_partial_not_success`, `test_input_file_vanishes_after_discovery` |
| 22 | batch with several bad images | `media::test_batch_with_several_bad_images_continues`, `stress::test_large_job` (5 % пошкоджених) |
| 23 | application restart during rendering | `integration/test_crash_recovery.py::test_kill_during_render_then_resume`, `gui::test_engine_crash_then_restart_offers_recovery`, `integration/test_crash_durability.py` |
| 24 | archive failure | `integration::test_archive_failure_marks_job_failed_but_keeps_video`, `media::test_archive_*` |
| 25 | output validation failure | `integration::test_output_validation_failure_is_never_success`, `media::test_broken_output_fails`, `test_wrong_frame_count_fails`, `test_missing_audio_stream_fails`, `test_audio_shorter_than_video_fails` |

## Додаткові критичні перевірки

| Вимога ТЗ | Тест |
|---|---|
| §5, §33 зависання ≠ «процес живий» | `ffmpeg::test_watchdog_*`, `test_hung_process_is_detected_and_killed[livelock]` |
| §13, §45 дерево процесів, процеси-сироти | `test_grandchild_is_killed_with_tree`, `test_stubborn_process_ignoring_q_and_sigterm_is_killed`, `media::test_timeout_kills_whole_tree`, перевірка наприкінці кожного прогону |
| §14 атомарний вихід | `unit::test_crash_between_write_and_replace_keeps_old_file`, `integration::test_existing_output_is_never_overwritten` |
| §20 обмежені повтори | `unit/test_job_manager.py::test_retry_budget_per_error_class`, `test_hard_attempt_cap_across_restarts` |
| §22 стан переживає SIGKILL | `integration/test_crash_durability.py::test_hard_kill_during_writes_keeps_db_consistent` |
| §25 ротація логів | `unit/test_logging.py::test_rotation_bounds_disk_usage` |
| §26 безпечні шляхи / argv | `test_safe_filename`, `media::test_argv_must_be_list_of_str` |
| §38 немає нескінченних циклів | `unit/test_code_invariants.py` (AST-аудит: `while True`, `shell=True`, очікування без timeout, `except: pass`, рекурсія) |
| §41 GUI не блокується | `gui::test_event_flood_keeps_ticks_short`, `gui::test_gui_responsive_while_ffmpeg_hangs_and_stop_works`, `stress::test_gui_latency_during_large_job` |
| один екземпляр | `integration::test_second_engine_on_same_data_is_refused` |

## Результати стрес-тестів (ТЗ §43–44)

Середовище: Linux-контейнер, 4 ядра, FFmpeg 6.1, тестова роздільність
426×240, preset `ultrafast`, 0,4 с на зображення, кожне 20-те зображення
пошкоджене.

### Великий job (`test_large_job`)

| Зображень | Пошкоджених | Результат | Час | RSS Engine, МБ (мін–макс) | RSS дочірніх, макс., МБ | Дочірніх процесів, макс. | Файлів у workspace, макс. → після |
|---|---|---|---|---|---|---|---|
| 100 | 5 | PARTIAL, 5 пропущено | 20,5 с | 81,8 – 82,5 | 72 | 2 | 195 → 0 |
| 500 | 25 | PARTIAL, 25 пропущено | 111,6 с | 85,6 – 88,6 | 77 | 3 | 955 → 0 |
| 1000 | 50 | PARTIAL, 50 пропущено | 246,1 с | 83,8 – 92,1 | 76 | 2 | 1905 → 0 |

* пам'ять Engine не залежить від кількості зображень (≈ 82–92 МБ для 100…1000);
* одночасно працює не більше 2–3 дочірніх процесів (ImageWorker + один FFmpeg);
* кількість кадрів у кожному відео точно дорівнює `ceil(D·fps)`;
* пошкоджені файли не зупиняють job, а job чесно позначено PARTIAL;
* після завершення: 0 файлів у workspace, 0 живих дочірніх процесів.

### Витік пам'яті: 50 jobs поспіль (`test_memory_leak_50_jobs`)

| Job | RSS до, МБ | RSS після, МБ | Δ, МБ |
|---|---|---|---|
| 1 | 89,82 | 89,87 | +0,05 |
| 10 | 89,11 | 89,12 | +0,01 |
| 25 | 89,21 | 89,21 | 0,00 |
| 50 | 89,32 | 89,32 | 0,00 |

Лінійний тренд jobs 10–50: **+0,005 МБ/job** (поріг 0,3), різниця job 50 −
job 10: **+0,2 МБ** (поріг 20). Накопичення пам'яті не виявлено. Повна
таблиця — `tests/stress/reports/memory_50_jobs.csv`.

### Реакція GUI під навантаженням (`test_gui_latency_during_large_job`)

300 зображень, справжній Engine-процес, 1275 вимірювань затримки таймера
GUI протягом усієї обробки: медіана **0,09 мс**, 99-й перцентиль **0,34 мс**,
максимум **0,50 мс**; медіанна тривалість такту вікна 0,11 мс.

### Обмеження вимірювань

Цифри отримано в Linux-контейнері. Набір `tests/stress` у Windows CI не
запускається (`-m stress` виключено за замовчуванням); його можна повторити
на Windows тією самою командою (`python -m pytest -m stress tests/stress`).
Результати навантаження на справжній Windows отримано окремою
production-серією (`tests/production`, `windows-latest`) — див.
`STRESS_TEST_REPORT.md`.

## MODE B (PHASE 11)

Справжніх ключів ElevenLabs / OpenAI у середовищі розробки й CI немає, тому
сервіси замінює локальний імітатор `tests/provider_mock.py` (HTTP-сервер на
127.0.0.1, контракт — за публічною документацією сервісів). Мережа не
використовується.

| Файл | Що перевіряє |
|---|---|
| `unit/test_provider_http.py` | timeout з'єднання і читання, межа розміру, обрізана відповідь, 5xx/429 з повторами і `Retry-After`, 401 без повторів, STOP під час очікування сервісу і під час паузи між повторами, очищення ключів із повідомлень |
| `unit/test_providers.py` | контракт запитів ElevenLabs і OpenAI, поділ довгого сценарію і склеювання MP3, повтор без повторної оплати готових частин, квота / відмова модерації / недійсний ключ, некоректні відповіді, розміри за орієнтацією, реєстр провайдерів, тестовий хук лише на loopback |
| `unit/test_credentials.py`, `unit/test_credentials_windows.py` | перевірка ключа; справжній Windows Credential Manager (лише Windows): запис, перезапис, читання, видалення |
| `integration/test_mode_b_services.py` | Engine + справжні адаптери: SUCCESS, кеш (повтор без запитів), тимчасові збої, недійсний ключ, відмова за промптом, зависання сервісу, STOP під час генерації, ключів немає в жодному файлі |
| `gui/test_mode_b_gui.py` | START у режимі B без ключів заблоковано з поясненням; діалог «Ключі API…» |
| `production/test_p05_mode_b.py` | production-тест 15 (лише Windows): реальні налаштування, Engine в окремому процесі читає ключі зі справжнього Credential Manager |

## Production stress / failure testing на Windows

Окремий набір `tests/production` (16 тестів: тривалий рендер 500 і 1000
зображень, 100 послідовних jobs, проблемні та пошкоджені зображення,
зависання FFmpeg, аварії GUI і Engine, переповнення каналу подій,
перезапуск і відновлення, збої бази стану, справжній малий том, перевірка
виходу, великий архів, заповнення диска під час архівування (G), фінальний
аудит ресурсів).

```
python -m pytest -m production tests/production --timeout=0      # quick-масштаб
VIDEOGEN_PROD_SCALE=full python -m pytest -m production tests/production
```

На `windows-latest` набір запускає `videogen-production-stress.yml`
(масштаб `full`, том VHD 400 МБ для тесту дискового простору, журнали
Engine/GUI в артефакті). Результати з реальними цифрами Windows (фінальний
прогін — 16/16) — у `STRESS_TEST_REPORT.md`; закриття прогалин B, E, F, G,
H, I, K, L — у `GAP_CLOSING_REPORT.md` (тести `tests/gui/test_entry_point.py`,
`tests/integration/test_publish_crash.py`, `test_worker_containment.py`,
`test_visual_sanity.py`, `tests/ffmpeg/test_job_object_windows.py`; у Windows CI —
окремий крок «Gap-closing tests with measured values»).
