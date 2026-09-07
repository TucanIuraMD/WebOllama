# Dashboard v2 — единая рабочая панель (2026-09-07)

## Задача

После v2-переработок Models / Running / PROCESSOR / GPU-telemetry / Chat
привести Dashboard (`webui/static/js/pages/dashboard.js`) к единому
понятному экрану ежедневного использования — без смены архитектуры.

## Аудит до изменений

### Структура (было)

1. **metric-tiles** (Ollama / Models / GPU / история-заглушки) — из
   `snap.ollama`, `snap.gpu` (WS-снапшот или `GET /api/status`);
2. **PROCESSOR-блок** — `renderProcessor(await API.get("/api/system/processor"))`
   по `setInterval(refreshProcessor, 3000)` — **таймер никогда не
   останавливался** (утечка между страницами, `refreshProcessor` имел
   guard `currentPage !== "dashboard"`, но интервал жил всегда);
3. **GPU-блок** (`dash-gpu-*`) — из того же снапшота;
4. **Running-блок** — `ollama.running` из снапшота, ссылки Stop не было;
5. **History-графики** — `GET /api/history/{gpu,system}` по кнопке/загрузке.

### Найденные проблемы

1. **Нет навигации.** На Dashboard не было ни одной ссылки на Models /
   Running / Jobs / Chat / Agents — точка входа не вела никуда.
2. **Loading state отсутствовал.** `el.innerHTML = buildPage(snap)` строился
   только ПОСЛЕ `await API.get("/api/status")` — до ответа экран пуст.
3. **Silent failure.** `catch (e)` подменял ошибку статуса фейковым
   снапшотом `{ ollama: { online: false } }` → ошибка API выглядела как
   «Ollama offline», а пустой список и ошибка были неразличимы.
4. **Утечка таймера.** PROCESSOR-interval переживал уход со страницы;
   повторный вход в Dashboard создавал второй интервал (двойной polling).
5. **Running-блок не имел CPU/GPU split** и действий — только имя+VRAM.
6. **Race conditions.** `/api/status`-ответ мог примениться после ухода со
   страницы или после более свежего WS-обновления (нет seq-guard);
   процессор-ответы могли применяться в неверном порядке.
7. **GPU VRAM и Model VRAM визуально смешивались** в одном ряду метрик.
8. **Частичный снапшот стирал блоки.** `drawSnapshot` рендерил все секции
   из `|| {}` — снапшот без `ollama` затирал последнее известное состояние
   Running-блока пустотой.

### Источники данных (проверены, сохранены)

| Данные | Источник |
|---|---|
| GPU (util, VRAM, temp, power, fan, clocks) | realtime WS-снапшот → `snap.gpu` (тот же объект для topbar, GPU-страницы, PROCESSOR) |
| CPU / RAM / swap / disk / network | `snap.cpu/ram/…` (SystemCollector в том же снапшоте) |
| Ollama online / counts / running | `snap.ollama` (OllamaClient → `/api/ps`, `/api/tags`) |
| Model VRAM | `snap.gpu.ollama_vram.total_vram` и `size_vram` per model |
| PROCESSOR-блок | `GET /api/system/processor` (1-й приоритет: тот же realtime-снапшот, 2-й: собственная выборка) |
| История | `GET /api/history/{gpu,system}` (SQLite metrics) |
| Кэш первого рендера | `App.state.snapshot` (уже полученный WS-снапшот) |

**Единственный производитель снапшота** — `RealtimeService.build_snapshot`
(single-flight GPU sample, `_snapshot_lock`). Вторых источников GPU
telemetry в Dashboard нет и не появилось: GPU-блок, GPU-плитка и PROCESSOR
читают один и тот же payload. Закреплено тестами
`test_status_gpu_matches_processor_gpu*` (backend) и harness-сценарием
«GPU available: single-source block».

## Решения

### 1. Shell-first рендер + явные состояния

- `buildPage(null, "")` строит каркас немедленно: все динамические блоки в
  состоянии `⏳ Loading…`;
- после `/api/status`: available / offline / empty / error;
- ошибка статуса → красный alert с причиной + «Retry», **не** «No models»;
- ошибка PROCESSOR → `Processor information unavailable — <reason>`;
- GPU недоступен → дружелюбный empty-state с причиной («no NVIDIA GPU…»,
  подсказка «CPU-only Ollama works fine») — GPU-less хост норма;
- stale PROCESSOR: после >7с безуспешных опросов бейдж
  «· data stale (API unreachable)», DOM держит последние хорошие данные.

### 2. Навигация

Ряд кнопок вверху: **Models · Running · Jobs · Chat · Agents · GPU**.
Плитки Models и Running сами являются ссылками (`<a class="metric-tile
dash-link" href="#models|running">`). Running-блок имеет кнопку
«open Running →», пустой Running — ссылку на Models.

### 3. Running-блок (компактный)

- имя модели, `model VRAM` (ровно `size_vram`), бейдж split **только из
  реальных данных**: `size_vram < size → N% GPU`, `>= size → 100% GPU`,
  данных нет → ничего (без эвристик — тот же контракт, что Running v2);
- badge `loaded`; offline → «Ollama offline — running models unknown»;
  пусто → «No models loaded» + ссылка на Models;
- Stop сознательно **не дублируется**: полный workflow (flight-state,
  dup-guard, WS-удаление) живёт на странице Running; Dashboard не создаёт
  альтернативную логику запуска/остановки.

### 4. GPU / VRAM — без расхождений

- GPU-блок рендерит `snap.gpu` как есть (util-бар, VRAM-бар, temp, power,
  fan target/PWM, clocks, mem clock);
- **GPU VRAM** (NVML: `vram_used/vram_total`) и **Model VRAM (Ollama)**
  (`gpu.ollama_vram.total_vram`) — отдельные подписанные строки; тест
  `test_vram_not_replaced_by_ollama_model_vram` (backend) + frontend
  сценарий фиксируют, что это разные числа в разных строках;
- multi-GPU: `gpus[]` передаётся как есть (backend-тест
  `test_multi_gpu_snapshot_preserved`), PROCESSOR рендерит по карточке на
  устройство — ничего не схлопывается.

### 5. Race conditions / polling-дисциплина

- `procSeq` — монотонный счётчик: ответ PROCESSOR и `/api/status`
  применяется, только если он от последнего запроса/рендера;
- shell-first + `App.state.snapshot`-кэш исключают двойной `/api/status`;
- **ровно один** `setInterval` (PROCESSOR, 3с, страница-scoped:
  `currentPage !== "dashboard"` → no-op), очищается при re-render и
  `beforeunload`; тест `test_dashboard_polling_discipline` запрещает
  второй `setInterval(` в файле;
- WS `onSnapshot` — единственный live-канал; частичный снапшот обновляет
  только присутствующие секции (guard-и на `snap.ollama/gpu/cpu`),
  остальное держит последнее известное состояние;
- history-графики — по кнопкам 1m/5m/15m/1h, live-push из WS; ошибка
  истории не уносит страницу (inline «History unavailable»).

### 6. Чего НЕ сделано (намеренно)

- Новых backend API нет — все источники уже существовали.
- Тяжёлых графиков/бенчмарков нет — 10 компактных sparkline, как было.
- Stop/Run-логики на Dashboard нет — только ссылки на страницы-оркестраторы.
- Архитектура (realtime → WS → снапшот → страницы) не тронута.

## Сопутствующее исправление

- `tests/conftest.py::_reset_singletons`: добавлен сброс
  `remote_sys._processor` — PROCESSOR-коллектор кэширует payload (TTL 2с),
  без сброса payload одного теста протекал в следующий
  (`/api/system/processor` отдавал чужой `vram_used`). Это источник
  реальных расхождений GPU-чисел между блоками в тестовой среде.

## Изменённые файлы

| Файл | Изменение |
|---|---|
| `webui/static/js/pages/dashboard.js` | v2: shell-first, nav, running+split, секционные guard-и, procSeq, timer cleanup, stale-бейдж |
| `webui/static/css/app.css` | `.dash-nav`, `a.metric-tile.dash-link` |
| `tests/test_dashboard_frontend.py` | **новый** — node-харнесс реального dashboard.js, 15 сценариев |
| `tests/test_dashboard_v2.py` | **новый** — 10 backend-тестов консистентности |
| `tests/conftest.py` | сброс `remote_sys._processor` между тестами |
| `docs/DASHBOARD-V2.md` | **новый** — этот документ |

## Тесты

### Frontend (`tests/test_dashboard_frontend.py`, node-харнесс, реальный dashboard.js)

1. initial loading states (shell до ответа `/api/status`);
2. status API failure → error alert с причиной, НЕ «no models»;
3. Ollama offline → OFFLINE-плитка, running-блок объясняет offline;
4. no models → отдельный empty-state + ссылка на Models;
5. running models: имена, model VRAM, split-бейджи только из данных;
6. GPU available: GPU VRAM и Model VRAM отдельно, один источник;
7. GPU unavailable: причина + подсказка, CPU-плитка живёт;
8. PROCESSOR: good / unavailable / garbage (нет NaN/undefined в DOM);
9. stale PROCESSOR: порог, последние данные не теряются при ошибке API;
10. навигация: Models/Running/Jobs/Chat/Agents/GPU + ссылки-плитки;
11. WS live update: плитки/бары/running из того же снапшота;
12. WS partial snapshot: секционные guard-и, last-known сохраняется;
13. ровно один интервал, очистка между рендерами;
14. кэш снапшота исключает повторный `/api/status`;
15. гонка двух рендеров: устаревший ответ игнорируется (procSeq).

### Backend (`tests/test_dashboard_v2.py`)

1. `test_status_gpu_matches_processor_gpu` — GPU/VRAM/temp равны в обоих
   endpoints на свежем снапшоте (один источник);
2. `test_status_gpu_matches_processor_gpu_via_live_build` — равенство и на
   live-build пути;
3. `test_status_stale_snapshot_falls_through_to_live_build` — кэш старше
   REFRESH_INTERVAL не отдаётся;
4. `test_ollama_offline_in_snapshot_not_faked` — offline честный, остальной
   снапшот полон;
5. `test_no_models_running_zero_not_missing` — реальный ноль ≠ offline;
6. `test_gpu_unavailable_snapshot_still_complete` — GPU-less хост норма;
7. `test_model_vram_reported_separately_from_gpu_vram` — GPU VRAM ≠ model VRAM;
8. `test_multi_gpu_snapshot_preserved` — мульти-GPU не схлопывается;
9. `test_processor_endpoint_graceful_on_error` — деградация без 500;
10. `test_single_snapshot_producer_no_duplicate_sources` — архитектурный
    пин: один производитель снапшота, один GPU-сэмпл.

## Результат

**174 passed, 1 skipped** (было 162 passed — +12 новых тестов; skip —
требующий jsdom markdown-тест). Failures: нет.

## Ограничения

- CPU/GPU split в running-блоке — производная `size`/`size_vram` (Ollama
  не отдаёт split явно); контракт совпадает со страницей Running v2.
- Stale-порог PROCESSOR (7с ≈ 2 пропущенных опроса) — эвристика UI, не
  серверное поле; при появлении `ts` в payload `/api/system/processor`
  можно заменить на точное сравнение времён.
- History-графики обновляются push-ом из WS только по GPU-метрикам
  (util/vram/temp/power/fan); CPU/RAM/net/disk — по перезагрузке диапазона
  (как и было).
- Дашборд по-прежнему не выполняет Stop/Run — намеренно (см. «чего НЕ
  сделано»).
