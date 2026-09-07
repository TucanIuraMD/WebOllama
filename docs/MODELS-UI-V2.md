# Models UI v2 — аудит и capabilities (2026-09-07)

## Исходное состояние (аудит)

Страница Models (v1) уже имела: список моделей из `/api/ollama/models`,
поиск по имени, сортировку (name/size/params/quant/family/modified),
family-фильтр, multi-select + batch-delete, Details-modal (license /
modelfile / template), Copy-modal, Delete с подтверждением, Pull → job →
redirect на #jobs, Create-modal. Running-страница показывала загруженные
модели с Stop; WS-снапшот обновлял её в реальном времени. Чат брал
список моделей из того же `/api/ollama/models`.

Проверено на реальном Ollama (в测试 среде разработки, без .22):
`GET /api/tags` отдаёт `capabilities` для каждой модели
(`["completion","tools","thinking"]`, `["completion","vision"]`, …),
`POST /api/show` — `capabilities` + `model_info["{arch}.context_length"]`.

## Найденные проблемы

1. **Capabilities нигде не показывались.** `/api/ollama/models` пробрасывал
   `capabilities` из Ollama (поле уже приходило), но фронт игнорировал его:
   ни колонки, ни фильтра, ни в Details (там они были строкой, без
   визуального выделения).
2. **Нет действия Run/Load.** Загрузить модель в VRAM со страницы Models
   было невозможно — только Running → Stop; модель для чата приходилось
   грузить сторонним способом (генерацией).
3. **Нет колонки статуса.** Models не показывал, какие модели running
   (состояние было видно только на странице Running).
4. **Silent failure при офлайне.** `render()` глотал ошибку и показывал
   обычный empty-state «No models found» — офлайн неотличим от пустой
   библиотеки.
5. **Details показывал `context_length` из `details`** — реальный Ollama
   кладёт его в `model_info["{arch}.context_length"]`; UI всегда
   показывал «—».
6. **Delete/batch-delete делали полный re-render страницы** (сброс
   фильтров/скролла), вместо обновления списка на месте.

## Причины

- Capabilities — относительно новая возможность `/api/tags`; страница
  писалась до её появления и просто не читала поле.
- Run отсутствовал, потому что в Ollama нет отдельного endpoint загрузки
  (пустой `POST /api/generate` + `keep_alive` — документированный
  механизм preload), и он нигде не оборачивался.
- Офлайн-состояние смешано с empty-state, т.к. `catch (e)` в `render()`
  не различал «пусто» и «недоступно».

## Изменения

### Backend (webui/routers/ollama.py, webui/ollama_client.py, webui/mock/__init__.py)

- `GET /api/ollama/models` — к каждому тегу добавляется `running`
  (источник — тот же OllamaClient, `/api/ps`; при недоступности ps —
  список всё равно отдаётся, без флагов). Capabilities пробрасываются
  как есть — **без вычислений по имени модели**.
- `POST /api/ollama/models/{name}/run` — новый endpoint: `load()` в
  клиенте = пустой `POST /api/generate` + `keep_alive=5m` (документированный
  preload Ollama); аудит `run`; 502 с понятным сообщением при ошибке.
- `POST /models/{name}/show` — поднимает `context_length` из
  `model_info` на верхний уровень.
- Мок приведён к реальному Ollama: `/api/tags` отдаёт capabilities;
  пустой generate = загрузка в `running`; `keep_alive=0` = выгрузка;
  `/api/show` отдаёт `capabilities` + `model_info["{family}.context_length"]`.

### Frontend (webui/static/js/pages/models.js, css, index — без изменений)

- Колонка **Capabilities**: чипы `Tools / Thinking / Completion / Vision`;
  неизвестная capability (например, появившаяся в будущем версия Ollama)
  рендерится с классом `cap-unknown` (пунктирная рамка, title
  «Unknown capability») и **не** подпадает ни под один известный фильтр.
- Фильтр **CAPABILITIES [All-подход через чипы]**: мультивыбор, модель
  должна иметь ВСЕ выбранные (AND). Модель без данных capabilities
  никогда не матчится — «unknown» ≠ «поддерживает всё».
- Колонка **Status**: `● running` (зелёный бейдж) / `○ not loaded`.
- Кнопка **Run/Stop** в строке (переключается по состоянию): Run → POST
  run → короткий poll `/api/ollama/running` до появления модели →
  refresh списка; Stop → DELETE stop → refresh. Кнопка блокируется на
  время запроса (per-model busy set — повторный клик невозможен).
- Состояния: offline → баннер `⚠ Ollama unavailable — <причина>` +
  empty-state «Ollama unavailable»; пустая библиотека → «No models found.
  Pull one from the library.»; пустой результат фильтров → «No models
  match the current filters».
- Delete/batch-delete обновляют список на месте (`refreshList()`):
  фильтры и скролл сохраняются, полного rebuild нет.
- CSS: `.badge.cap`, `.badge.cap-unknown`, `.badge.running-badge`,
  `.cap-chip` (+`.active`) — в существующей палитре.

### Не тронуто (workstream)

Agents, PROCESSOR, GPU telemetry, GPU stale snapshot — без изменений.
Ollama capabilities НЕ смешаны с Agents capabilities (каталог Coding/
Chat/… остался в Agents-странице и БД; в Models — только 4 Ollama-значения).

## API flow

```
Список:   GET /api/ollama/models
          → OllamaClient.tags()      (GET  /api/tags — capabilities, size, details)
          + OllamaClient.running_models() (GET /api/ps — running-флаги)
Run:      POST /api/ollama/models/{name}/run
          → OllamaClient.load()      (POST /api/generate {"model","keep_alive":"5m"})
          → poll GET /api/ollama/running (в UI)
Stop:     DELETE /api/ollama/models/{name}/stop
          → OllamaClient.stop()      (POST /api/generate {"model","keep_alive":0})
Details:  POST /api/ollama/models/{name}/show → POST /api/show
Delete:   DELETE /api/ollama/models/{name}    → POST /api/delete
Pull:     POST /api/ollama/models/pull → job (JobManager) — без изменений
```

Running-страница и Чат получают состояние из тех же источников
(`/api/ps` через WS-снапшот и `/api/ollama/models` соответственно) —
Run из Models мгновенно отражается во всех трёх местах без нового
state-store.

## Capabilities — источник

Только `GET /api/tags` (поле `capabilities` модели, Ollama ≥ 0.3.x).
Никаких эвристик по имени модели; неизвестные значения показываются
как unknown. Значения: `tools`, `thinking`, `completion`, `vision`.

## Изменённые файлы

| Файл | Изменение |
|---|---|
| `webui/routers/ollama.py` | running-флаги в списке; новый `run` endpoint; context_length в show |
| `webui/ollama_client.py` | новый метод `load()` |
| `webui/mock/__init__.py` | capabilities/tags, generate: load+unload, show: model_info |
| `webui/static/js/pages/models.js` | capabilities-колонка+фильтр, статус, Run/Stop, состояния, in-place delete |
| `webui/static/css/app.css` | стили чипов/бейджей |
| `tests/test_models_v2.py` | **новый** — 8 backend-тестов |
| `tests/test_models_frontend.py` | **новый** — 12 frontend-сценариев (node harness) |
| `docs/MODELS-UI-V2.md` | **новый** — этот документ |
| `CHANGELOG.md` | 1.3.0 |

## Тесты

Backend (`tests/test_models_v2.py`, FastAPI TestClient + mock Ollama):
1. Список несёт capabilities из Ollama verbatim + running-флаги.
2. Run → модель в `/api/ps` и `running=true` в списке (Models↔Running↔Chat контракт).
3. Stop → из `/api/ps` исчезла, `running=false`; повторный Stop — не 5xx.
4. Run неизвестной модели → 502 «not found».
5. Run без авторизации → 401.
6. Show отдаёт context_length из model_info + capabilities; unknown → 502.
7. Модель без capabilities-поля → `[]` (unknown, не «поддерживает всё»).
8. Ollama offline → 502 с «offline» (не пустой список).

Frontend (`tests/test_models_frontend.py`, node + реальный models.js):
1. Рендер 4 моделей: чипы capabilities, unknown-marking, статус, Run/Stop.
2. Фильтр Vision сужает список (клик по чипу → only vision-модель).
3. Мультивыбор AND: Tools+Vision → пусто + правильный empty-state.
4. Поиск + фильтр вместе: Vision + «qwen» → только qwen2.5vl:7b.
5. Unknown capability не матчит известный фильтр (mystery/weirdcap vs Tools).
6. Run: POST → poll → refresh → кнопка Stop; success-toast.
7. Run-ошибка (model not found) → error-toast.
8. Stop: DELETE → refresh, not-loaded.
9. Stop-ошибка (offline) → error-toast.
10. Delete: удаление без полного rebuild страницы (контейнер не перерисовывается).
11. Offline: баннер + offline empty-state (не «No models found»).
12. Empty library → pull-hint; пустой фильтр → filter-hint; очистка фильтров скрывает empty-state.

## Результат

**153 passed, 1 skipped** (skip — markdown-тест без jsdom; полный набор
включая Models v2, Chat v2/v3, Agents, GPU telemetry, API).

## Известные ограничения / backlog

- Pull остаётся redirect-на-jobs (job-based прогресс уже есть в Jobs
  странице); инлайн-прогресс в Models не добавлялся — существующий
  механизм честный, дублировать не стали.
- Контекст фильтров сбрасывается при уходе со страницы (page state,
  как и в v1).
- `keep_alive` для Run фиксирован (5m, дефолт Ollama); настройка
  per-run — backlog.
