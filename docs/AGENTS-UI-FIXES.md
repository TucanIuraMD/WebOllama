# Agents UI — исправления (2026-09-06)

## Контекст

Ручная проверка UI матрицы Agents (Models × Agents) после merge commit
`060fc68` (runtime agent/capability management) выявила две проблемы
в раскрытом виде модели (row expansion → model view).

Правки **только frontend + frontend-тесты**. Backend, Git history,
PROCESSOR и GPU stale workstream не затронуты. Архитектура Agents
не менялась: Agents/Capabilities остаются runtime-configurable,
benchmark/scoring не добавлялся.

## Обнаруженные проблемы

1. **Collapse — не контрол.** В заголовке раскрытой модели текст
   `(click model again to collapse)` был обычным текстом без
   интерактивности. Свернуть модель можно было только повторным кликом
   по имени модели, о чём сообщал этот текст.

2. **Строки оценок не кликабельны.** В раскрытой модели отображались
   строки `Hermes — — / OpenCode — — / Claude — — / OpenWebUI — —`
   (класс `.mv-row`), но клик по ним ничего не делал. Редактор оценки
   (Status / Capabilities / Note / Save / Reset) открывался только из
   ячеек самой матрицы, из модельного вида добраться до него было нельзя.

## Внесённые изменения

### 1. Настоящая кнопка Collapse
- Заголовок карточки раскрытой модели теперь содержит
  `<button class="btn btn-sm" data-action="agents-model" data-model="…">Collapse</button>`.
- Нажатие сворачивает модель (тот же обработчик, что и клик по имени
  модели — toggle `expandedModels` + `drawMatrix()`).
- Обычный текст `(click model again to collapse)` удалён.

### 2. Кликабельные строки оценок
- Каждая строка `.mv-row` теперь несёт
  `data-action="agents-cell" data-model="…" data-agent="…"` — клик по
  любой части строки открывает редактор оценки (Status:
  untested/failed/works/good, Capabilities, Note, Save, Reset) — тот же
  модальный редактор, что и у ячеек матрицы.
- Дополнительно в конце строки — явная кнопка `Edit` (доступный фолбэк,
  понятный label).
- CSS: `.mv-row { cursor: pointer; }`, hover-подсветка, кнопка прижата
  вправо (`margin-left: auto`).

### 3. Frontend-тесты
Расширены headless node-сценарии (`tests/test_agents_frontend.py`),
весь поток прогоняется на реальном `agents.js` в DOM-заглушке:

- раскрытие модели показывает кнопку `Collapse` с `data-action` и не
  содержит текстовой подсказки «click model again»;
- обе строки `.mv-row` кликабельны (`data-action="agents-cell"`) + кнопка `Edit`;
- редактор открывается из строки модельного вида (qwen3:8b × Hermes);
- Collapse работает независимо от редактора (редактор закрыт — collapse
  сворачивает модель);
- Save из редактора модельного вида отправляет корректный payload
  (model / agent_id / status / note / capabilities) и закрывает модал;
- после Save оценка сразу видна и в раскрытом виде, и в ячейке матрицы
  (slug capability), раскрытие модели сохраняется;
- повторное раскрытие модели показывает сохранённые данные;
- повторное открытие редактора пре-заполняет status/caps/note и
  показывает Reset to untested.

Итого в файле 25 node-проверок (было 15) + 2 static-теста исходника
(наличие Collapse-кнопки/кликабельных строк, отсутствие текстовой
подсказки).

## Изменённые файлы

| Файл | Изменение |
|---|---|
| `webui/static/js/pages/agents.js` | Collapse-кнопка в `modelView()`; кликабельные `.mv-row` + кнопка Edit; удалён текст-подсказка |
| `webui/static/css/app.css` | `.mv-row` cursor/hover, `.mv-edit` выравнивание |
| `tests/test_agents_frontend.py` | +8 node-сценариев по новым UX-требованиям, +1 static-регрессионный тест |
| `docs/AGENTS-UI-FIXES.md` | этот документ |

## Результаты тестов

- Полный suite: **125 passed / 0 failed** (pytest `tests/ -q`, ~22.5s;
  до правок — 118 passed: +7 новых тестов в agents frontend).
- Все 25 node-проверок agents UI — PASS, включая сценарии:
  collapse control; открытие editor по строке; Save assessment;
  повторное открытие сохранённой assessment; Reset; фильтры
  Agents/Capabilities/Status продолжают работать (покрыты существующими
  и ранее добавленными проверками).

## Проверенный browser flow (headless-прогон реального кода)

1. Клик по модели → раскрытие, кнопка Collapse в заголовке.
2. Клик по строке агента (или Edit) → модальный редактор оценки.
3. Save → POST `/api/agents/assessments`, модал закрыт, матрица и
   раскрытый вид обновлены мгновенно.
4. Повторное открытие модели/редактора → сохранённые данные на месте.
5. Reset → DELETE `/api/agents/assessments/{id}`, ячейка снова `—`.
6. Collapse сворачивает модель независимо от редактора.
7. Фильтры Agent / Capability / Status работают (delegated actions).

## Commit

- `7483e89` — fix(agents-ui): Collapse button + clickable assessment rows in model view

## Деплой на 192.168.80.22

Выполняется пользователем вручную после push (например `git pull` +
перезапуск сервиса WebOllama); Hermes на .22 не заходит.
