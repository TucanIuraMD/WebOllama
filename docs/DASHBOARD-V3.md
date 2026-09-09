# Dashboard v3 — visual redesign + REAL hardware metrics (2026-11)

## v3.1 UI polish: иконки + композиция карточек

### 1. Иконки — emoji → единый набор inline SVG

Emoji (🖥️ 🧠 🎮 💾 📦 ▶ ⚙ 💬 🤖 🌙 🔌) заменены монохромными
outline-иконками. В проекте не было icon-библиотеки (только точечные
SVG-ring'и и text-глифы сортировки в Models), поэтому выбран **inline SVG**
без внешних зависимостей: один визуальный язык — `viewBox 0 0 22 22`,
`stroke="currentColor"`, `stroke-width 1.6`, round caps, 17×17px,
`fill="none"` (контурный стиль, цвет наследуется от текста — иконки
автоматически принимают `good/bad/warn`-цвета плиток).

Источник — функция `iconSvg(name)` в `dashboard.js` (экспортируется для
тестов), глифы:

| name | глиф | где используется |
|---|---|---|
| `cpu` | chip (корпус + ножки + кристалл) | карточка CPU |
| `ram` | memory module (планка + чипы) | карточка RAM |
| `gpu` | graphics card (плата + кронштейн + вентилятор) | карточка GPU, empty-state, nav |
| `storage` | disk (корпус + бли + активность) | карточка Storage |
| `ollama` | server (стойка + LED) | плитка Ollama, empty-state offline, Chat-кнопка/ссылка |
| `models` | layers (стопка) | плитка Models, nav, empty «No models loaded» |
| `running` | play (круг + треугольник) | плитка Running, nav |
| `jobs` | activity (пульс) | плитка Jobs, nav |
| `power` | bolt (молния) | плитка Electricity |

Размещение: заголовки карточек (`.hw-icon`), подписи верхних плиток
(`.tile-icon`), быстрые ссылки (`.dash-nav .btn-sm`), empty-состояния
(`.empty .icon`). **Emoji полностью убраны из Dashboard** — закреплено
регрессионным тестом (проверка юникод-диапазонов 1F000–1FAFF,
2600–27BF, 2B00–2BFF по всей разметке страницы + uniform-размер/цвет
каждого `<svg class="icon">`).

### 2. Композиция hw-карточек (RAM / GPU / Storage — единая структура)

Фиксированный вертикальный порядок внутри карточки:

1. **head** — иконка + имя;
2. **`hw-main`** — основной показатель СЛЕВА (`.hw-pct-block`: большое
   значение + подпись), круговой индикатор СПРАВА (`.hw-ring-wrap`:
   SVG-ring + подпись-подпись, у GPU — VRAM-cap);
3. **`hw-bar`** — progress bar НА ВСЮ ширину карточки (вне flex-строки);
4. **`hw-kv`** — USED / FREE / TOTAL в ОДНУ горизонтальную строку
   (`display:flex; nowrap`, три равные ячейки; у GPU — Power /
   Temperature / Model VRAM / Fan Target / Fan PWM / Clocks — тоже
   одной строкой).

CSS: `.hw-kv` переведён с `grid 3 колонки` на `flex` c равными ячейками;
responsive fallback `@media (max-width: 480px)` — ячейки переносятся
(`flex-wrap`, `flex-basis 40%`). `.hw-icon`/`.tile-icon` получили
`inline-flex`-выравнивание SVG.

**CPU сохраняет логически соответствующую структуру**: utilization слева
+ ring справа (у CPU ring дублирует util — основной показатель), bar —
опциональный (load/threads, только когда load известен; у CPU нет
«Used/Free/Total» — вместо этого Cores / Threads / Frequency одной
строкой, fake-строка хранения не подставляется).

Не менялось: backend, API, telemetry, realtime/WS, polling, значения,
single-source architecture, Electricity, NVMe — только разметка и CSS.

### Регрессионные тесты (v3.1)

Добавлены 2 сценария в `tests/test_dashboard_v3_frontend.py` (итого 20):

- **«icons: no emoji anywhere, monochrome inline SVG markup instead»** —
  отсутствие emoji во всей разметке, наличие SVG-иконок в 4 hw-карточках,
  ровно 5 tile-иконок, единый размер (17px) и `currentColor`-обводка
  каждого инстанса, наличие всех 9 глифов через `dash.iconSvg`;
- **«hw-card composition: main left, ring right, full-width bar, one kv
  row»** — для RAM/GPU/Storage порядок `hw-main(pct+ring) → hw-bar →
  hw-kv`, набор kv-ключей (Used/Free/Total; Power/Temperature), ровно 3
  ячейки у RAM; CPU — своя логичная структура (Cores/Threads/Frequency,
  без fake-storage-строки).

---

## Задача

Визуально переработать Dashboard (тёмная тема сохранена) до «реально
готового» состояния: верхняя навигация, отдельные информативные карточки
оборудования (CPU / RAM / GPU / Storage), Running Models в стиле
`ollama ps` — **без смены архитектуры**: single-source realtime telemetry,
никаких новых samplers/polling-механизмов, никаких NVMe/SMART.

## Что изменилось (кратко)

| Блок | v2 | v3 |
|---|---|---|
| Верх | 4 metric-tiles + ряд кнопок | 5 навигационных плиток (Ollama / Models / Running / Jobs / Electricity) + ряд быстрых ссылок |
| Оборудование | GPU-блок + PROCESSOR, CPU/RAM/Storage без карточек | 4 карточки `hw-card`: CPU, RAM, GPU, Storage (ring + bar + kv) |
| Running Models | компактный список имя+VRAM | таблица `ps-table` в стиле `ollama ps`: Model / ID / Size / Processor / VRAM / Context / Duration / Status / Actions (Chat + Stop) |
| Electricity | compact tile (v1.3) | сохранён как 5-я плитка, без изменений логики |
| PROCESSOR | без изменений | без изменений (тот же 3s poll, тот же блок) |
| History | без изменений | без изменений (те же графики) |

## Карточки оборудования

### CPU (`drawCpu`)
- реальная загрузка из `snap.cpu.percent` (SystemCollector / psutil);
- **zero-value contract**: `0%` → `0%`, `100%` → `100%`;
  `fmtPct(null/undefined/NaN)` → `—` (данные отсутствуют ≠ 0);
- модель CPU (новое поле `snap.cpu.model` — читается из `/proc/cpuinfo`
  один раз за жизнь коллектора, без subprocess), cores/threads,
  load 1/5/15, частота.

### RAM (`drawRam`)
- Used / Free / Total (`fmtBytes`), процент, ring + bar;
- `100%` отображается как `100%`; `0 B` как `0 B`.

### GPU (`drawGpu`)
- **тот же единый источник**: `snap.gpu` из `RealtimeService.build_snapshot`
  (GPUCollector → NVML → nvidia-smi → optional remote). Второго sampler'а нет;
- utilization — большое число; VRAM — отдельный ring с Used/Total;
- температура, Power, Model VRAM (Ollama `size_vram` — подписана
  «Model VRAM (Ollama)», не смешивается с GPU VRAM);
- multi-GPU: первый GPU — основная карточка, остальные — строки
  `hw-mini-gpu` (index, name, util, VRAM, temp) — устройства никогда
  не схлопываются;
- GPU недоступен → честный empty-state с причиной
  («no NVIDIA GPU detected…», «CPU-only Ollama works fine»), CPU/RAM
  карточки не затрагиваются;
- `0%` → `0%`, `0 W` → `0 W`, `0 GB` → `0 GB`.

### Storage (`drawStorage`)
- агрегат диска из того же SystemCollector-снапшота (`snap.disk`):
  Used/Free/Total, процент, partition-строки (до 3);
- **NVMe/SMART/temperature НЕ добавлялись** — по требованию.

## Running Models (`ps-table`)

Данные — ровно `snap.ollama.running` (OllamaClient → `/api/ps`):

- **Model** — имя; **ID** — первые 12 символов digest;
- **Size / VRAM** — `size` и `size_vram` как есть (`fmtBytes`), без данных → `—`;
- **Processor** — derived split из уже существующей деривации
  `size_vram / size`: `<100% → "N% GPU / M% CPU"`, `≥100% → "100% GPU"`,
  нет size/size_vram → `—` **без эвристик**. Бейдж подписан
  «derived from /api/ps size vs size_vram — not a telemetry split»;
- **Context** — `context_length` (или `details.context_length`), без → `—`;
- **Duration** — честный обратный отсчёт до `expires_at` (`fmtDur`),
  без данных → `—`;
- **Status** — `● running` (строки таблицы = запущенные модели);
- **Actions** — `data-action="run-chat"` / `"run-stop"` — переиспользуют
  существующие обработчики страницы Running (window.Actions), без
  дублирования логики;
- offline → «🔌 Ollama offline — running models unknown», строк НЕ
  выдумывается; счётчик Running тоже `—` (неизвестно ≠ 0);
- пусто → «🌙 No models loaded» + ссылка на Models.

## Состояния (loading / error / partial / offline / stale)

1. **Shell-first**: `buildPage(null, "")` строит каркас немедленно —
   все карточки в `⏳ Loading…` пока `/api/status` в полёте;
2. **ошибка статуса** → красный alert «Dashboard data unavailable — …» +
   Retry; фейковых данных нет;
3. **частичный снапшот**: каждая секция `drawSnapshot` проверяет наличие
   своей части (`snap.cpu`, `snap.ram`, `snap.gpu`, `snap.disk`,
   `snap.ollama`, `snap.jobs`) и при отсутствии НЕ трогает DOM —
   последнее известное состояние сохраняется;
4. **offline Ollama** → плитка `● OLLAMA OFFLINE` (класс `bad`),
   таблица Running явно пишет «offline», счётчик `—`;
5. **stale PROCESSOR**: после >7с неудачных опросов бейдж
   «· data stale (API unreachable)»; DOM держит последние хорошие данные
   (refreshProcessor ловит ошибку внутри и не трогает карточки);
6. **race conditions**: seq-guard (`procSeq`) — /api/status-ответ и
   процессор-ответы применяются только если не устарели; повторный render
   инкрементирует seq и обнуляет старые continuation.

## Polling / WebSocket

- **единственный interval на странице** — PROCESSOR 3s poll, создаётся в
  `render()`, останавливается `stopProcessorPolling()` при каждом
  re-render и уходе со страницы (проверено тестом «no duplicate polling»
  и `test_dashboard_polling_discipline`);
- live-обновления — через существующий WS `onSnapshot` (drawSnapshot +
  charts.push + electricity soft-refresh);
- Electricity tile: 1 fetch за render + WS-cadence soft refresh с троттлом
  60s (`elecSummaryFetchedAt`), `force=true` обходит троттл — **новых
  таймеров нет**.

## Backend (в том же коммите, остаётся single-source)

- `webui/sys_collector.py`: в существующий sample добавлено поле
  `cpu.model` (однократное чтение `/proc/cpuinfo` с валидацией имени;
  ARM `model`-числа отбрасываются). Никаких subprocess/новых коллекторов.
- `webui/gpu_collector.py`: single-flight `sample()` (параллельные
  вызовы ждут ОДНУ коллекцию, кэш перезаписывается только завершённой),
  явные `ts` / `collected_at` / per-GPU `ts` для честной stale-детекции;
  fan-состояние читается параллельно (медленный journalctl не тормозит
  NVML-числа).
- `webui/realtime.py`: Ollama status с SLA 3s (`asyncio.wait_for`) —
  зависший llama-server больше не замораживает WS-снапшот; GPU/system
  стартуют сразу и параллельно; history пишет `ts` снапшота, а не момент
  persist.
- `webui/routers/status.py`: `/api/status` отдаёт кэш только если он
  свежий (`REFRESH_INTERVAL + 1s`), иначе живой build — застрявший
  realtime-loop не консервирует старое состояние навсегда.

## Тесты

- `tests/test_dashboard_v3_frontend.py` (новый, jsdom): 20 сценариев —
  полный рендер, zero-value semantics (0%/0 B/0 W/0 GB и 100%), таблица
  Running (колонки, derived split, actions), partial snapshot,
  GPU-unavailable, offline, API-error, Electricity states, WS live update,
  single-timer, race двух renders, snapshot-кэш, stale-PROCESSOR,
  троттлинг Electricity, навигация, responsive-разметка; плюс
  `test_dashboard_polling_discipline` (в исходнике ровно один
  `setInterval`).
- `tests/test_gpu_stale_regression.py` (новый): single-flight коллекция,
  timestamp-семантика, hung-Ollama не вносит стагнацию снапшота, offline
  вместо crash, history ts = ts снапшота, `/api/status` отклоняет
  протухший кэш.
- `tests/test_dashboard_frontend.py` (v2-layout flows) удалён: проверял
  разметку v2 (dash-gpu-body / grid grid-4 и т.п.), несовместимую с v3;
  валидные сценарии (stale PROCESSOR, race, throttle, кэш, force-refresh)
  перенесены в v3-файл. `tests/test_dashboard_v2.py` (backend-контракты:
  single-source GPU, multi-GPU, model-VRAM раздельно) остался и проходит.

## Известные ограничения

1. Processor split — деривация из `size`/`size_vram` (`/api/ps`), а не
   телеметрия; подписана как derived.
2. Fan Target/PWM на карточке GPU видны только если V100 fan-сервер
   доступен (`fan_available`), иначе `—`.
3. `dash-proc-state` stale-бейдж появляется только после неудачного
   refresh ПОСЛЕ 7с порога — в первые 7с недоступности API карточка
   выглядит обычной.
4. Мини-GPU строки (2-й и далее GPU) показывают util/VRAM/temp, но не
   имеют собственных колец/баров — компактность важнее паритета.
5. CPU `model name` может отсутствовать (нестандартные ядра/VM) — строка
   модели тогда просто не отображается (никаких заглушек).
