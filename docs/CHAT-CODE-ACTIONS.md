# Chat Code Block Actions — Копировать / Сохранить / Ответить (2026-11, v1.1)

## Что это

Каждый Markdown code block (```` ```lang … ``` ````) в Chat обрамляется
двумя одинаковыми группами действий:

```
[ Копировать ]  [ Сохранить ]  [ Ответить ]   ← НАД кодом
┌───────────────────────────────────────────┐
│ def hello():                              │
│     print("Hello")                        │
└───────────────────────────────────────────┘
[ Копировать ]  [ Сохранить ]  [ Ответить ]   ← ПОД кодом
```

Кнопки сверху позволяют действовать, не прокручивая длинный блок;
кнопки снизу — после прочтения кода. Стиль — ghost-кнопки 11px,
полупрозрачные до hover (`opacity .55`), без emoji, в общей палитре
тёмного UI; код визуально доминирует.

Реализация: `webui/static/js/pages/chat.js` (функции `decorateCodeActions`,
`copyCode`, `downloadCode`, `replyWithCode`), CSS: `.code-actions`,
`.cb-btn` (+ состояния `.cb-ok` / `.cb-err`) в `webui/static/css/app.css`.

## Копировать (v1.1)

- копирует `textContent` соответствующего `<code>` — **без fences** и
  **без language tag**;
- **клиент-only**: `navigator.clipboard.writeText()`; ни одного
  fetch/API-вызова (проверено тестом);
- фолбэк: `navigator.clipboard` существует только в secure context — на
  plain http (LAN-хосты, напр. Hermes) используется legacy-путь
  `document.execCommand('copy')` через скрытую textarea;
- **визуальная обратная связь**: на 1.5 с текст кнопки меняется на
  «Скопировано» (класс `.cb-ok`, зелёный) или «Ошибка» (класс `.cb-err`,
  красный), затем восстанавливается;
- **отказоустойчивость**: отказ Clipboard API (NotAllowedError и т.п.),
  отсутствие API и исключения execCommand обрабатываются — Chat не
  падает, содержимое блока и транскрипт не меняются, остальные кнопки
  (в т.ч. на других блоках) продолжают работать.

## Сохранить

- сохраняет **именно содержимое** соответствующего блока: fences ```` ``` ````
  и language tag **не попадают в файл** (marked уже снял их при рендере,
  берётся `textContent` элемента `<code>`);
- **filename/extension** по language tag (см. таблицу ниже); неизвестный /
  отсутствующий язык → `snippet.txt`;
- **клиент-only**: `Blob` + `URL.createObjectURL` + `<a download>`,
  никакого fetch/API-вызова, данные не покидают браузер;
- объектный URL освобождается через 1 с после клика.

| Language tag | Filename      |
|--------------|---------------|
| python/py    | script.py     |
| javascript/js/node | script.js |
| typescript/ts | script.ts   |
| bash/sh/shell/zsh/console | script.sh |
| json         | data.json     |
| yaml/yml     | config.yaml / config.yml |
| toml/ini     | config.toml / config.ini |
| html/xml/css | page.html / doc.xml / style.css |
| sql          | query.sql     |
| rust/rs      | main.rs       |
| go/golang    | main.go       |
| c/cpp/c++    | main.c / main.cpp |
| java/kotlin/kt | Main.java / Main.kt |
| cs/c#        | Program.cs    |
| ruby/rb      | script.rb     |
| php/perl/pl  | script.php / script.pl |
| lua/r/matlab | script.lua / script.r / script.m |
| markdown/md  | note.md       |
| diff/patch   | patch.diff / patch.patch |
| dockerfile   | Dockerfile    |
| makefile     | Makefile      |
| *что угодно другое / нет тега* | snippet.txt |

## Ответить

- относится **только к своему блоку**: в composer подставляется
  ```Про этот код:\n```lang\n<код>\n``` именно этого блока;
- **не отправляет сообщение автоматически** — `sendOrStop`/`sendMessage`
  не вызываются; пользователь редактирует текст и сам жмёт Send
  (статус-строка напоминает об этом);
- используется **существующий composer** (`#chat-input`), второго
  редактора нет. Composer — `<textarea rows=1>` с авторазмером (до
  ~220px): **Enter — отправить, Shift+Enter — новая строка**; после
  «Ответить» каретка ставится в конец, текст можно дописать и отправить —
  контекст диалога (`messages`) при этом не теряется;
- безопасность: значение задаётся через `input.value = …` (plain JS
  string) — HTML/JS из кода **не может исполниться** через composer.

## Инварианты действий (v1.1)

- все три действия работают **только со своим блоком** — несколько
  code blocks в одном ответе полностью независимы (проверено тестами:
  верхняя кнопка 1-го блока, нижняя кнопка 2-го, у каждого своё
  содержимое);
- действия **не мутируют** code block: содержимое `<code>`, количество
  групп кнопок и текст транскрипта не меняются после Copy/Save/Reply;
- уже полученный ответ **не теряется**: Copy/Save/Reply не очищают
  транскрипт и не трогают `messages` — следующий send уносит полный
  корректный контекст (строгое чередование user/assistant, проверено
  тестом);
- inline code (`` `код` ``) никогда не получает кнопок — декорируются
  только `<pre>`.

## Streaming-устойчивость и отсутствие утечек

- chat.js перерисовывает ответ целиком на каждый delta
  (`renderChatBody(bodyNode, reply)` = `innerHTML = renderMarkdown(...)`
  + `decorateCodeActions`), поэтому дублирование кнопок между delta
  **невозможно по построению**: старое поддерево DOM отбрасывается
  целиком вместе со своими кнопками и их обработчиками;
- `decorateCodeActions` идемпотентна: `<pre>` уже с `.code-actions`
  пропускается (защита от любых будущих путей частичного обновления);
- обработчики вешаются **только на свежесозданные кнопки** (никакой
  делегации на общий контейнер) — повторная декорация не стекает
  листенеры, утечек нет;
- финальный рендер (после `done`/Stop/ошибки) проходит через тот же
  `renderChatBody` — все три кнопки работают и после завершения стрима;
- **Stop** (AbortController) и SSE-пайплайн не изменялись.

## Что не затронуто

- marked / DOMPurify pipeline (md.js) — без изменений; декорация
  добавляет узлы только **вокруг** готового `<pre>` и не трогает
  содержимое кода; XSS-защита сохранена (регрессионный тест на
  декорированном DOM);
- подсветка синтаксиса (класс `language-*` на `<code>`) сохраняется;
- backend/SSE — без изменений; никакого нового polling или отдельного
  механизма обновления Chat нет;
- обычный текстовый Markdown, таблицы, цитаты — регрессионные тесты
  продолжают проходить.

## Тесты

`tests/test_chat_code_actions.py` — реальный chat.js + md.js + marked +
DOMPurify в jsdom (vm.runInContext — резолв глобалов как в браузере),
**19 проверок** в одном сквозном сценарии:

1. composer — textarea, рендер страницы;
2. у каждого блока ровно 2 группы из **трёх** кнопок
   (Копировать/Сохранить/Ответить с `data-cb-action`), первый и
   последний ребёнок `<pre>`;
3. Save: fences/language не попадают в файл, содержимое байт-в-байт,
   ноль сетевых вызовов;
4. таблица расширений; пустой/неизвестный/null язык → `snippet.txt`;
5. блок без языка сохраняется как .txt;
6. Reply: заполняет существующий composer, **не** вызывает fetch/API,
   не трогает кнопку Send; `<script>` в коде остаётся текстом;
7. 3 независимых блока: Save/Reply берут свой блок;
8. streaming (2 delta + done): ровно один `<pre>`, ровно 2 группы,
   Save работает, дублей нет;
9. markdown/XSS регрессия на декорированном DOM;
10. идемпотентность и отсутствие стекания обработчиков;
11. DOM-порядок: top bar → code → bottom bar;
12. **Copy верхней кнопкой 1-го блока**: точное содержимое, без
    fences/тега, ноль fetch/API;
13. **Copy нижней кнопкой 2-го блока** + независимость (затем 3-й блок);
14. **фидбек «Скопировано»** + класс `.cb-ok`;
15. **отказ Clipboard API** → «Ошибка», `.cb-err`, Chat жив: Copy и Save
    на других блоках работают после отказа;
16. **отсутствие clipboard API** (http-контекст) → фолбэк без краха;
17. inline code не получает кнопок;
18. действия не мутируют блок/транскрипт, reply не теряется;
19. **полный поток**: стрим → Copy+Save+Reply → редактирование composer →
    Enter → в `messages` уходит полный корректный контекст (чередование
    ролей, первый вопрос, ответ с кодом, отредактированный текст), и
    после второго ответа кнопки снова работают.

Плюс source-контракт `test_code_actions_wiring_in_source`: и delta, и
final рендер идут через `renderChatBody` (никаких bypass-renderов),
`replyWithCode` не содержит sendOrStop/sendMessage, `downloadCode` и
`copyCode` — без fetch/API, copy использует `navigator.clipboard.writeText`
+ execCommand-фолбэк + фидбек, в bar HTML ровно три `data-cb-action`.

Полный Chat regression suite (`test_chat_frontend`, `test_chat_markdown`,
`test_chat_stream`, `test_running_frontend`) продолжает проходить.

