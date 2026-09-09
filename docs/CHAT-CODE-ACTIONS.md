# Chat Code Block Actions — Save / Reply (2026-11)

## Что это

Каждый Markdown code block (```` ```lang … ``` ````) в Chat обрамляется
двумя компактными группами действий:

```
[ Save ]  [ Reply ]      ← НАД кодом
┌──────────────────────┐
│ def hello():         │
│     print("Hello")   │
└──────────────────────┘
[ Save ]  [ Reply ]      ← ПОД кодом
```

Кнопки сверху позволяют действовать, не прокручивая длинный блок;
кнопки снизу — после прочтения кода. Стиль — ghost-кнопки 11px,
полупрозрачные до hover (`opacity .55`), без emoji, в общей палитре
тёмного UI; код визуально доминирует.

Реализация: `webui/static/js/pages/chat.js` (функции `decorateCodeActions`,
`downloadCode`, `replyWithCode`), CSS: `.code-actions`, `.cb-btn` в
`webui/static/css/app.css`.

## Save

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

## Reply

- относится **только к своему блоку**: в composer подставляется
  ```Про этот код:\n```lang\n<код>\n``` именно этого блока;
- **не отправляет сообщение автоматически** — `sendOrStop`/`sendMessage`
  не вызываются; пользователь редактирует текст и сам жмёт Send
  (статус-строка напоминает об этом);
- используется **существующий composer** (`#chat-input`), второго
  редактора нет. Для поддержки перевода строк composer переведён с
  `<input type=text>` на `<textarea rows=1>` с авторазмером (до ~220px):
  **Enter — отправить, Shift+Enter — новая строка**;
- безопасность: значение задаётся через `input.value = …` (plain JS
  string) — HTML/JS из кода **не может исполниться** через composer;
  фокус и каретка ставятся в конец текста.

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
  `renderChatBody` — кнопки работают и после завершения стрима.

## Что не затронуто

- marked / DOMPurify pipeline (md.js) — без изменений; декорация
  добавляет узлы только **вокруг** готового `<pre>` и не трогает
  содержимое кода;
- подсветка синтаксиса (класс `language-*` на `<code>`) сохраняется;
- SSE-стриминг, Stop (AbortController), обычный текстовый Markdown,
  таблицы, цитаты — регрессионные тесты продолжают проходить.

## Тесты

`tests/test_chat_code_actions.py` — реальный chat.js + md.js + marked +
DOMPurify в jsdom (vm.runInContext — резолв глобалов как в браузере):

1. composer — textarea, рендер страницы;
2. у каждого блока ровно 2 группы действий (первый и последний ребёнок
   `<pre>`), кнопки Save/Reply с `data-cb-action`;
3. Save: fences/language не попадают в файл, содержимое байт-в-байт,
   ноль сетевых вызовов;
4. таблица расширений (python→.py, js, bash, json, ts, go, sql…);
   пустой/неизвестный/null язык → `snippet.txt`;
5. блок без языка сохраняется как .txt;
6. Reply: заполняет существующий composer, **не** вызывает fetch/API,
   не трогает состояние кнопки Send; `<script>` в коде остаётся текстом;
7. 3 независимых блока: Save нижней кнопкой 1-го / верхней 2-го / Reply
   3-го — каждая операция берёт свой блок;
8. streaming (2 delta + done): ровно один `<pre>`, ровно 2 группы
   кнопок после финального рендера, Save работает, дублей нет;
9. markdown/XSS регрессия на декорированном DOM (h1/bold/inline-code
   живы, `<script>` вырезан);
10. идемпотентность (повторная декорация ничего не добавляет) и
    отсутствие стекания обработчиков;
11. DOM-порядок для длинного блока: top bar → code → bottom bar.

Плюс source-контракт `test_code_actions_wiring_in_source`: и delta, и
final рендер идут через `renderChatBody` (никаких bypass-renderов),
`replyWithCode` не содержит sendOrStop/sendMessage, `downloadCode` —
без fetch/API, только Blob+download API.
