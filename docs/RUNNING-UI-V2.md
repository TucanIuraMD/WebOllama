# Running UI v2 — аудит и редизайн страницы Running (2026-09-07)

## Состояние до изменений

Страница Running (v1) была таблицей (`webui/static/js/pages/running.js`, 86 строк):

- **Данные**: `GET /api/ollama/running` (OllamaClient → `/api/ps`) при
  рендере; live-обновление через существующий WS-снапшот
  (`onSnapshot(snap)` → `snap.ollama.running`, дельта по сигнатуре
  name/size_vram/expires_at).
- **Колонки**: Model, Size, VRAM (=model `size_vram`), Params, Quant,
  Family, Until, Digest, Actions (одна кнопка ⏹ Unload).
- **Действия**: `run-stop` → `DELETE /api/ollama/models/{name}/stop`;
  после успеха — `setTimeout(re-render, 800)`.
- **Связи**: никаких переходов в Chat/Models.

Проверено на реальном Ollama 0.32.6: `/api/ps` отдаёт на модель
`name/model, size, size_vram, expires_at, context_length, details,
digest`. Отдельного поля CPU/GPU split **нет** (см. «CPU/GPU» ниже).

## Найденные проблемы

1. **Silent failure.** `catch (e)` глотал ошибку API и рисовал «No models
   loaded into memory» — офлайн/ошибка неотличимы от пустого списка.
2. **Нет состояния загрузки.** Пока летит первый запрос — пустая страница.
3. **Кнопка ⟳ не работала.** Она была `onclick="location.reload()"` —
   перезагрузка всего приложения вместо перерисовки страницы.
4. **Нет CPU/GPU split.** `size_vram < size` (частичная выгрузка слоёв на
   CPU) никак не подсвечивался — две колонки чисел, разницу видит только
   внимательный человек.
5. **Нет перехода в Chat.** Для загруженной модели — самой частой операции —
   приходилось уходить на Chat и выбирать модель вручную.
6. **Stop без loading-state и без защиты от двойного клика.** Двойной клик
   → два DELETE; ошибки API не восстанавливали кнопку.
7. **`expires_at`/`context_length` не всегда отрисовывались согласованно**;
   контекст вообще не показывался (в v1 его не было в UI).
8. **Обновление после Stop полагалось на `setTimeout(re-render)`** — при
   медленном WS страница «мигала» полной перестройкой; при быстром WS
   рендер выполнялся дважды.

## Причины

Страница писалась первой из трёх (Models/Running/Chat) и осталась в
виде «минимальной таблицы»: состояния не разделены (один empty-state на
все случаи), actions без guard'ов, cross-page навигация не была нужна
до появления Run на Models (v2). CPU/GPU split не показан потому, что
Ollama не отдаёт его явным полем — а раньше не решились выводить его
производным.

## Решение

### Структура (карточки вместо таблицы)

```
RUNNING

[заголовок: Running Models N] [⟳ = render(), не location.reload()]

run-grid: карточки (auto-fill minmax(340px,1fr))

┌────────────────────────────────────────┐
│ qwen3:8b                    ● running  │
│ Model VRAM   4.7 GB   (accent, mono)   │
│ Model size   4.7 GB                    │
│ CPU / GPU    100% GPU  (badge)         │
│ Params       7.6B    Quant  Q4_K_M     │
│ Context      32,768                    │
│ Unloads at   2026-09-08 10:47:57       │
│ ─────────────────────────────────────  │
│ 💬 Chat          ⏹ Stop                │
└────────────────────────────────────────┘

Total model VRAM: 18.2 GB (sum size_vram из /api/ps — не GPU telemetry)
```

### VRAM: две разные величины — не смешиваются

- **Model VRAM** — ровно `size_vram` из `/api/ps`, по каждой модели и в
  сумме. Это НЕ GPU telemetry (`gpu_collector`/PROCESSOR не используются;
  в коде запрещено тестом `test_running_page_uses_existing_sources_only`).
- **GPU VRAM** (15.4/16 GB и т.п.) живёт на Dashboard/GPU-страницах и
  сюда не переносится.

### CPU/GPU split — без эвристик, из того же payload

Ollama не отдаёт split явным полем, но `size` (полный размер весов) и
`size_vram` (резидентно в VRAM) есть:

- `size_vram < size` → бейдж `N% GPU / (100-N)% CPU`, где
  `N = round(size_vram/size*100)`, title с точными байтами обеих частей;
- `size_vram ≥ size` → `100% GPU`;
- `size_vram == 0` или поля нет → `—` (ничего не выдумываем).

### Live update — существующий WS, второй механизм не создан

`onSnapshot` остался единственным live-каналом; сигнатура расширена
`context_length` (перерисовка при смене контекста). Новое:

- `ollama.online === false` → состояние «Ollama unavailable», при этом
  **последние известные данные не выбрасываются** (count сохраняется);
- `snap.ollama.running` отсутствует (временно недоступен) → перерисовка
  из last-known, страница не ломается;
- сигнатура не изменилась → redraw пропускается (нет мигания);
- гонка «WS обновил → 800мс fallback re-render» устранена renderSeq:
  устаревший fetch после refresh/stop просто игнорируется.

### Actions

- **Stop**: per-model `stopping` Set — двойной клик no-op; кнопка
  disabled + «⏳ stopping…» на время полёта; ошибка → toast + `draw()`
  (кнопка восстановлена, retry возможен); успех → toast; карточку
  убирает WS-снапшот, fallback re-render срабатывает только если WS
  молчит.
- **Chat**: `#chat?model=<name>` — deep link (ниже).
- **⟳**: перерисовывает страницу (`render(page-container)`), а не всё
  приложение.

### Models ↔ Running ↔ Chat

- **Models → Run** → `/api/ps` → WS-снапшот → карточка появляется в
  Running без reload (проверено тестом «model appears after Run»).
- **Running → Stop** → карточка исчезает, Models `running=false`
  (тот же OllamaClient; контракт закреплён backend-тестом
  `test_running_after_models_run_flow_sync`).
- **Running → Chat**: `route()` в app.js теперь парсит query из hash
  (`#chat?model=...` → `App.state.pageQuery`); chat.js применяет
  preselect, только если модель существует в `/api/ollama/models`
  (иначе — прежнее поведение: первая модель). История чата (внутреннее
  состояние модуля) не затронута.

### Состояния (иерархия, проверяется тестом)

1. `loading` — до завершения первого запроса (⏳ Loading running models…);
2. `error` — API вернул ошибку: «Failed to load running models» + причина.
   **Не** «No models loaded»;
3. `offline` — WS `online=false`: «Ollama unavailable» + last-known данные;
4. `empty` — реально 0 моделей: «No models loaded into memory» + ссылка
   на Models;
5. карточки — нормальный режим.

## Источник данных

| Данные | Источник |
|---|---|
| Список, size, size_vram, expires_at, context_length, details | `GET /api/ollama/running` → OllamaClient → `/api/ps` (verbatim) |
| Live-обновления | существующий realtime WS-снапшот (`snap.ollama.running`), `ollama.online` |
| CPU/GPU split | производное от `size`/`size_vram` того же payload |
| Stop | `DELETE /api/ollama/models/{name}/stop` (без изменений) |
| GPU VRAM (не здесь) | Dashboard/GPU pages (PROCESSOR/gpu_collector) |

## Изменённые файлы

| Файл | Изменение |
|---|---|
| `webui/static/js/pages/running.js` | переписан: карточки, 5 состояний, split, Chat-link, stop-guard, renderSeq |
| `webui/static/js/app.js` | `route()`: парсинг query из hash → `App.state.pageQuery` (3 строки логики) |
| `webui/static/js/pages/chat.js` | preselect модели из `pageQuery.model` (5 строк) |
| `webui/static/css/app.css` | `.run-grid/.run-card/.run-card-head/.run-card-grid/.run-card-actions` |
| `webui/mock/__init__.py` | running стартует пустым (честный «свежий Ollama»); preload кладёт `expires_at` + top-level `context_length` (как реальный `/api/ps`) |
| `tests/test_running_frontend.py` | **новый** — node-харнесс, реальный running.js + chat.js, 17 сценариев |
| `tests/test_running_v2.py` | **новый** — 8 backend-тестов контракта `/api/ollama/running` |
| `tests/test_models_v2.py` | приспособлен к пустому старту mock (run перед проверкой флага) |
| `tests/test_ollama_client.py` | то же: load перед ps/status/stop |
| `tests/test_api.py` | то же + изоляция rate-limiter (dangerous limiter общий) |
| `docs/RUNNING-UI-V2.md` | **новый** — этот документ |

## Тесты

### Frontend (`tests/test_running_frontend.py`, node-харнесс, реальный running.js/chat.js)

1. loading state во время полёта первого запроса;
2. API error → error-state, НЕ «No models loaded»;
3. Ollama offline (WS online=false) → offline-state, last-known данные сохранены;
4. empty state отличим от ошибки (+ссылка на Models);
5. карточка GPU-модели: model VRAM, 100% GPU, context, unload time, статус;
6. CPU/GPU split: `size_vram < size` → процентный бейдж; `size_vram=0` → ничего не выдумано; мульти-модельность;
7. total — именно сумма model VRAM, подпись «not GPU telemetry»;
8. live update: модель появляется после Run (WS) без reload;
9. live update: модель исчезает после Stop (WS);
10. сигнатура не изменилась → redraw пропущен (нет мигания);
11. несколько моделей: все карточки, count совпадает;
12. Stop success: disabled+label в полёте, WS убирает карточку, toast;
13. двойной Stop: второй клик в полёте — no-op (1 вызов API);
14. Stop error: error-toast, кнопка восстановлена, retry возможен;
15. Running → Chat deep link (`#chat?model=...`, encodeURIComponent);
16. Chat открывается с предвыбранной моделью (pageQuery → selected), список моделей не потерян;
17. источник данных: `/api/ollama/running` + `size_vram`, без `/api/gpu`.

### Backend (`tests/test_running_v2.py`)

1. `running` требует авторизации (401 без сессии);
2. пустой список, когда ничего не загружено;
3. raw-поля `/api/ps` проходят verbatim (size, size_vram, expires_at, context_length, details);
4. Stop отражается в running;
5. несколько моделей — у каждой свой `size_vram` (основа мульти-GPU/мульти-модели без серверных эвристик);
6. Ollama offline → 502, никогда не пустой 200-список;
7. Models ↔ Running sync: run → running-флаг и /running согласованы.

### Изменённые существующие тесты (причина — честный пустой старт mock)

- `test_ollama_client.py`: `test_tags_and_ps`/`test_status_reports_online`/
  `test_stop_unloads_via_keep_alive` — теперь явно загружают модель
  (`c.load()`), а не полагаются на вечно-загруженный дефолт;
- `test_api.py::test_running_models` — run перед проверкой; автоз-фикстура
  изоляции rate-limiter (общий dangerous-limiter, иначе 429);
- `test_models_v2.py::test_models_list_carries_capabilities_and_running` —
  run перед проверкой running-флага.

## Результат

**162 passed, 1 skipped** (skip — markdown-тест, требующий jsdom; полный
набор: Models v2/v3, Chat v2/v3, Agents, GPU telemetry, API, Running v2).

## Известные ограничения / backlog

- **CPU/GPU split — производная величина** (size vs size_vram), т.к.
  Ollama `/api/ps` не отдаёт split явно; при появлении явного поля в API —
  заменить источник, UI-бейдж уже готов.
- **Per-GPU разбивка** (какая модель на каком GPU) в `/api/ps`
  отсутствует — отображается только агрегированный по модели split.
  Мульти-GPU не ломается: каждая модель независима, суммарный VRAM
  честный; для per-GPU данных нужно ждать поля от Ollama (не
  предполагать GPU 0).
- Preselect в Chat применяется один раз на render; повторный переход
  с той же моделью при уже открытом чате не сбрасывает выбор
  (существующее поведение страницы Chat сохранено).
- Точная детализация «частичная выгрузка слоёв» (сколько слоёв на CPU)
  Ollama не отдаёт — показываются только проценты от размера весов.
