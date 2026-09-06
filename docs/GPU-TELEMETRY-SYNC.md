# GPU telemetry synchronization — PROCESSOR ↔ GPU-блок Dashboard (2026-09-06)

## Исходная проблема

На реальном сервере 192.168.80.22 два GPU-представления Dashboard
одновременно показывали **разные** состояния одной и той же карты
Tesla V100-SXM2-16GB:

| Показатель | PROCESSOR (новый блок) | GPU-блок (нижний, существующий) | `ollama ps` (реальность) |
|---|---|---|---|
| Utilization | 9 % | 0 % | 35 % (GPU) |
| VRAM | 15.4 / 16.0 GB | 550.5 MB / 16 GB | devstral-24b — 13.5 GB |
| Temperature | 45 °C | 44 °C | — |

Модель `devstral-24b:latest` реально держит ~13.5 GB VRAM → показание
PROCESSOR (15.4 GB с моделью) соответствует реальности, а показание
нижнего GPU-блока (550 MB, 0 %) — нет.

## Источники данных обоих блоков (до исправления)

Оба блока на бэкенде используют **один и тот же** `GPUCollector`
(NVML → nvidia-smi → remote; singleton `get_gpu_collector()`,
single-flight + cache 0.9 s). Второго механизма сбора GPU в проекте нет.
Расхождение возникало **не в сборщике, а во времени и пути доставки**:

- **GPU-блок (нижний)**: `RealtimeService.build_snapshot()` →
  `GPUCollector.sample()` → `last_snapshot` → WebSocket `/ws` + `/api/status`
  → `updateFromSnapshot()` → секция GPU.
- **PROCESSOR**: собственный цикл фронта (3 s) → `GET /api/system/processor`
  → `ProcessorCollector.sample()` → **самостоятельный** вызов
  `GPUCollector.sample()` (плюс SystemCollector и OllamaClient).

## Причина рассинхронизации

1. **Два независимых времени съёма.** PROCESSOR вызывал GPUCollector в
   момент HTTP-запроса, GPU-блок — в момент сборки realtime-снапшота.
   Между ними окно в секунды, в течение которого utilization/VRAM
   успевают измениться (загрузка/выгрузка модели меняет VRAM скачком).
2. **Разные окна консистентности.** `GPUCollector` кэширует на 0.9 s;
   realtime — на `REFRESH_INTERVAL` (2 s); PROCESSOR — на свой кэш 2 s.
   Соседние, но не совпадающие окна дают соседние, но не совпадающие
   цифры.
3. **`ollama ps` (35 %/65 % CPU/GPU) — не GPU utilization.** Это доля
   слоя модели на GPU, отдельная метрика; она не должна подменять
   ни NVML utilization, ни общую VRAM (см. требование 9 — соблюдено:
   model VRAM хранится в `ollama.running_models[].size_vram`, а
   `gpus[].memory_used` — всегда показание NVML/nvidia-smi).
4. Отдельный наблюдённый механизм устаревания (уже закрыт workstream'ом
   stale-snapshot, до исправления был в untracked-правках): зависший
   Ollama-запрос стопорил realtime-цикл, и `/api/status` отдавал
   устаревший снапшот.

## Исправление

Единый источник телеметрии — **current snapshot realtime-сервиса**.
`ProcessorCollector.sample()` теперь работает по приоритетам:

1. **`realtime-snapshot`**: если у `RealtimeService` есть свежий
   `last_snapshot` (возраст ≤ `REFRESH_INTERVAL * 2`, т.е. актуальный или
   одно окно «старее»), PROCESSOR строит payload **из него** — тот же
   объект, который рендерит GPU-блок. Расхождение исключено по
   построению; плюс исчезают дублирующие опросы GPU (HTTP-запрос
   PROCESSOR больше не дёргает NVML отдельно).
2. **`own-collection`**: если снапшота нет или он устарел (стоял старт,
   завис realtime-цикл) — PROCESSOR собирает свежие данные сам, как и
   раньше. Устаревшие числа GPU-блока **не** протекают в PROCESSOR —
   это продолжает гарантию workstream'а stale-snapshot.

Не менялось и сохранено:
- архитектура PROCESSOR v2 (локальный сбор, без host-agent/SSH);
- NVML → nvidia-smi fallback, multi-GPU, single-flight и кэш
  GPUCollector;
- stale/last-good поведение (workstream не тронут: файлы
  `gpu_collector.py`, `realtime.py`, `routers/status.py`, `app.js`,
  `test_gpu_stale_regression.py` остались с изменениями автора
  workstream'а, без наших правок);
- `ollama ps` VRAM — отдельная метрика от общей VRAM GPU;
- без benchmark/scoring/ranking; без второго GPU-механизма.

Payload PROCESSOR помечен полем `telemetry_source`
(`realtime-snapshot` | `own-collection`) + `snapshot_ts` для наблюдаемости.

## Изменённые файлы

| Файл | Изменение |
|---|---|
| `webui/remote_sys.py` | `_realtime_snapshot()` — доступ к current snapshot realtime-сервиса с проверкой свежести; `_from_shared_snapshot()` — сборка payload из общего снапшота (GPU/CPU/ollama); `sample()` — приоритет shared-снапшота, fallback на собственную сборку; `telemetry_source`/`snapshot_ts`; docstring модуля дополнен |
| `tests/test_processor.py` | +7 regression-тестов синхронизации (см. ниже); стаб `make_collector` управляет shared-снапшотом |
| `docs/GPU-TELEMETRY-SYNC.md` | этот документ |

## Regression-тесты (path-to-Dashboard, не только collector)

Все тесты — на mock-коллекторах, без сети и без реального GPU:

1. `test_processor_matches_dashboard_gpu_block` — PROCESSOR показывает
   ровно те же utilization/VRAM/temperature, что в снапшоте GPU-блока.
2. `test_vram_not_replaced_by_ollama_model_vram` — `gpus[].memory_used`
   (15.4 GB) и `ollama.vram_used` (13.5 GB) не смешиваются.
3. `test_stale_realtime_snapshot_not_used` — устаревший снапшот
   (75 %/55 °C, час назад) отвергается **реальной** проверкой свежести;
   PROCESSOR собирает свежие данные (`telemetry_source: own-collection`).
4. `test_no_snapshot_falls_back_to_own_collection` — realtime отсутствует
   (ранний старт) → свежая собственная сборка без падений.
5. `test_multi_gpu_consistent_between_blocks` — multi-GPU проходит через
   общий снапшот с сохранением per-index значений.
6. `test_realtime_snapshot_and_api_status_share_gpu_object` — сквозной
   путь: `/api/status` (данные GPU-блока) и `/api/system/processor`
   возвращают идентичные GPU-числа из одного снапшота.
7. `test_dashboard_gpu_tile_uses_fresh_shared_source` — то же при
   актуальном снапшоте, с проверкой `telemetry_source`.

## Результаты тестов

- Полный suite: **132 passed / 0 failed** (~24.6 s).
  До задачи — 125; +7 новых тестов синхронизации.
- `test_gpu_stale_regression.py` (untracked workstream) — проходит,
  stale/last-good поведение не нарушено.

## Commit

- см. hash в финальном отчёте задачи (коммиты этой задачи: фикс синхронизации + документация).

## Замечание по деплою

Проверка на 192.168.80.22 выполняется пользователем вручную после
`git pull` (Hermes на .22 не заходит). Ожидаемый результат: оба блока
показывают одинаковые utilization/VRAM/temperature; VRAM отражает
загруженные модели; поле `telemetry_source` в `/api/system/processor`
— `realtime-snapshot`.
