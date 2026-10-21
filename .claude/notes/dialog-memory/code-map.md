> Исследование 2026-10-20 до начала работ (справочно, не источник истины). Где расходится с `docs/portal-design.md` 0.7 (§5.7, §13) — прав дизайн. В частности, панель памяти — не модальный `SidePage`, а скрываемая колонка справа (решения 10–12). Номера строк кода могли устареть.

## backend-context
# Как бэкенд портала сейчас собирает запрос к модели

Ниже разбор по семи пунктам задания. Файлы не менялись. Пути указаны от `portal/backend/`, если не сказано иное.

## 1. Бюджеты, оценка токенов и усечение истории

**Бюджеты**
- Окно модели берётся из `MAX_MODEL_LEN=65536` (`deploy/.env.example:43`). vLLM запускается с `--max-model-len` (`deploy/docker-compose.yml:140-141`). Порталу значение передаётся окружением (`docker-compose.yml:348`), читается в `core/settings.py:668-672` и в `core/container.py:195`. Запасного значения нет: без переменной портал не стартует.
- Бюджет запроса считается так: `_budget = max_model_len - plan.max_tokens - llm.safety_margin_tokens` (`dialogs/generation.py:404-406`). Запас `safety_margin_tokens: 4096` задан в `config/app.yaml:57`.
- Предел ответа `max_tokens` зависит от вида диалога:
  - чат: `fast: 4096`, `thorough: 16384` (`app.yaml:118-120`), выбирается в `generation.py:400`;
  - sql и cogis: 8192 (`app.yaml:140`, `app.yaml:234`);
  - docparse: 4096 (`config/docparse.yaml:9`).
- Получается, что на запрос остаётся 57344 токена в режиме «Быстро» и 45056 в режиме «Вдумчиво».
- Резерв базы знаний `kb.context_max_tokens: 8000` (`config/kb.yaml:37`) используется в двух местах:
  - при предварительной проверке `_check_fits` он вычитается из бюджета, если поиск будет выполняться (`generation.py:408-418`);
  - сами найденные места отбираются по порядку выдачи, пока «правила плюс блок» укладываются в 8000 (`kb/retrieval.py:115-117`, `kb/context.py:54-61`). Фрагменты не обрезаются: фрагмент, который не влез, и все следующие за ним отбрасываются.
  - В `fit_history` резерв отдельно не вычитается. Найденное к этому моменту уже вклеено в текст вопроса (`generation.py:716-718`), поэтому учитывается как часть вопроса.
- Изображения:
  - не больше 8 на запрос, это лимит модели (`llm/ports.py:10`, `--limit-mm-per-prompt` в `docker-compose.yml:146-147`);
  - в одном вопросе тоже не больше 8 (`generation.py:287-288`);
  - перед отправкой картинка уменьшается до 1568 px по длинной стороне (`app.yaml:59`, `llm/bifrost.py:34-51`).
- Вложения: текст вложения длиннее `max_model_len * chars_per_token` отсекается ещё при чтении (`core/container.py:212-214`). Не больше 10 вложений на вопрос (`app.yaml:117`), вопрос не длиннее 32000 символов (`app.yaml:99`).
- docparse ведёт свой бюджет: `max_model_len - max_output_tokens - safety_margin - dialog_reserve_tokens(8192)` (`tools/docparse.py:211-216`, `docparse.yaml:11`). Под историю вопросов к документу остаётся примерно 8192 токена.

**Оценка токенов.** Настоящего токенизатора нет. Порт `TokenEstimator` прямо помечен как «точного счёта у портала нет» (`llm/ports.py:100-109`). Реализация `RatioTokenEstimator` (`llm/estimator.py:6-20`) считает:
- текст: `ceil(len(text) / chars_per_token)`, где `chars_per_token: 2.0`;
- изображение: фиксированно `tokens_per_image: 3200` (`app.yaml:64-65`).

В комментарии к конфигу сказано, что кириллица у Qwen занимает около трёх символов на токен, а коэффициенты взяты с запасом и их нужно сверить на ВМ (`app.yaml:61-63`). Значит, оценка завышена примерно в 1,5 раза.

**Алгоритм `fit_history`** (`dialogs/context.py:102-127`):
1. Постоянная часть: `fixed = text(system) + text(question) + image() * len(question.images)`.
2. Сначала отбрасываются первые `min_dropped` сообщений. По оставшейся истории считаются токены и картинки.
3. Затем в цикле с начала истории: если `fixed + tokens <= budget`, картинок не больше 8 и `history[dropped].role == "user"`, цикл останавливается. Иначе отбрасывается ещё одно сообщение, суммы уменьшаются вычитанием.
4. Функция возвращает число отброшенных сообщений. Отбрасывается всегда префикс, сообщения целиком, и оставшаяся история обязана начинаться с вопроса пользователя (тест `tests/test_context.py:119-125`).

Отдельная проверка `fits_alone` (`context.py:97-99`) отклоняет вопрос ещё до открытия потока ошибкой `message_too_long` (`generation.py:417-418`).

**Повтор при переполнении** (`generation.py:726-743`). Если Bifrost или vLLM ответили 400 и в тексте ответа есть строка `"maximum context length"` (`bifrost.py:185-187`, `app.yaml:60`), запрос повторяется один раз. Отбрасывается `max(dropped*2, dropped + ceil(remaining/2))` сообщений (`generation.py:741-742`). Если и это не помогло, ответ получает `error`/`message_too_long`.

**Отдаёт ли модель число токенов (usage).** Нет, портал его не запрашивает и не читает:
- в `_payload` нет `stream_options.include_usage` (`bifrost.py:164-179`);
- `_events` поле `usage` игнорирует (`bifrost.py:191-207`);
- `complete_with_reason` из ответа берёт только `content` и `finish_reason` (`bifrost.py:143-152`).

vLLM возвращает usage в ответе без потока всегда, а в потоке — только с `stream_options`. Пропускает ли это поле Bifrost — **проверить на ВМ**. Отдельный путь к `/tokenize` у vLLM порталу закрыт: `portal-api` подключён только к сетям `edge`, `portal`, `sandbox` (`docker-compose.yml:367-372`), а к сети vLLM (`inference`) — только Bifrost (`docker-compose.yml:120-122`).

## 2. Что и в каком порядке уходит в запрос

Запрос собирают `_answer` (`generation.py:701-743`) и `_model_messages` (`generation.py:777-787`).

1. **Системное сообщение** `plan.system`:
   - чат: `chat.system_prompt` (`app.yaml:121-129`, `generation.py:397`);
   - sql: промпт действия с подставленным диалектом, а также схема БД блоком `<схема>` (`tools/dialog_tools.py:121-127`);
   - cogis: промпт действия и, если документации CoGIS нет в базе, `no_documentation_notice` (`dialog_tools.py:207-214`);
   - docparse: `dialog_system_prompt` (`dialog_tools.py:272`);
   - если выполнялся поиск, в конец добавляется `retrieval.rules` (`generation.py:716`).
2. **История** `history[dropped:]`, собирается в `history_turns` (`context.py:80-90`):
   - у вопроса пользователя: блоки `<вложение>` всех его вложений, затем текст; сканы страниц прикладываются картинками заново на каждом ходе (`context.py:64-77`);
   - у ответа: только `content`, размышления не входят;
   - ответ с пустым текстом пропускается, поэтому подряд могут идти два вопроса пользователя;
   - в историю попадают сообщения со всеми статусами, в том числе остановленные и ошибочные, если у них есть текст (`dialogs/repositories.py:169-176`).
3. **Текущий вопрос**:
   - чат: `<база_знаний>` (если был поиск), затем блоки `<вложение>`, затем текст (`generation.py:718`, `context.py:77`);
   - cogis: найденное в базе знаний, затем текст (`dialog_tools.py:216-221`);
   - sql: только текст;
   - docparse: `<документ>`, затем `<реквизиты>`, затем текст (`dialog_tools.py:266-273`). Документ входит в **каждый** запрос, но в истории остаётся только исходный текст прежних вопросов, потому что `message.content` хранится без блоков.
4. Найденное в базе знаний в историю **не сохраняется** (`generation.py:714-715`). В ответе хранятся только процитированные источники, причём одной цитатой `quote` (`generation.py:643-646`, `generation.py:854-866`).

Отдельные вызовы модели: переписывание поискового запроса по последним 6 сообщениям, не больше 4000 символов (`kb.yaml:216-217`, берёт `search_history` из `generation.py:152-158`), и предложение названия диалога (`generation.py:826-851`).

## 3. Узнаёт ли пользователь об усечении

Да, но грубо, одной плашкой без подробностей:
- сервер шлёт SSE-событие `context_truncated {dropped_messages}`, не больше одного раза (`generation.py:723-724`, контракт `docs/portal-api.md:1455-1459,1784`). После повтора событие не шлётся заново, а в базе сохраняется итоговое число (`generation.py:727`);
- поле хранится в `messages.dropped_messages` (`dialogs/tables.py:39`, `repositories.py:229-244`) и отдаётся клиенту в `MessageView` (`dialogs/schemas.py:216,239`);
- фронтенд принимает событие (`frontend/src/api/stream.ts:16`, `frontend/src/chat/state.ts:247-248`) и показывает плашку `longDialog` только под последним ответом, если `dropped_messages > 0` (`frontend/src/components/chat/DialogScreen.tsx:345-353`, `frontend/src/screens/DocumentsScreen.tsx:438-441`). Текст плашки: «самые ранние сообщения модель больше не учитывает…» (`frontend/src/texts.ts:445-484`).

Чего нет: процента заполнения, того, какие именно сообщения выпали, и предупреждения до отправки.

## 4. Сохраняется ли реальный расход токенов (usage, prompt_tokens)

Нигде. Поиск по `usage` и `prompt_tokens` в `src` дал только `shutil.disk_usage`. В таблице `messages` нет колонок под токены (`tables.py`). В конфиге Bifrost стоит `disable_content_logging: true` (`deploy/bifrost/config.json:6`); хранит ли Bifrost сами счётчики токенов в своих журналах — проверить на ВМ.

## 5. Размышления в режимах «Быстро» и «Вдумчиво»

- Режим переводится в уровень размышлений: `_EFFORT = {"fast": "low", "thorough": "xhigh"}` (`generation.py:85`). Уровень `xhigh` в запрос не передаётся: Bifrost переписывает его в `high`, которого шаблон модели не знает (`bifrost.py:170-173`). У sql и cogis уровень `medium`, у docparse `low` (`app.yaml:139,233`, `docparse.yaml:16`).
- Размышления приходят в `delta.reasoning_content` или `delta.reasoning` (`bifrost.py:199-201`, `--reasoning-parser qwen3` в `docker-compose.yml:148-149`). Они стримятся событием `reasoning_delta` и сохраняются в `messages.reasoning` и `reasoning_seconds` (`generation.py:807-812,655-656`).
- В историю размышления **не входят** (`context.py:83`).
- Важно: в vLLM `max_tokens` ограничивает размышления и ответ вместе, поэтому резерв 16384 в режиме «Вдумчиво» уходит в основном на размышления. Заполненность контекста зависит от режима: при одной и той же истории доля разная.

## 6. Куда подключать менеджер контекста

- **Загрузка состояния диалога.** В `_answer` в той же транзакции, где читаются `history` и `message_attachments` (`generation.py:705-709`), можно читать закрепления, исключения и сводку диалога.
- **Исключения и подмена сводкой.** Фильтр в `history_turns(messages, attachments)` (`context.py:80-90`): пропускать исключённые сообщения и вложения и подставлять ход-сводку вместо свёрнутого префикса. То же нужно сделать в `search_history` (`generation.py:152`), чтобы переписывание поиска не видело исключённого.
- **Закрепление.** `fit_history` (`context.py:102-127`) сейчас возвращает `int`, то есть длину отброшенного префикса. Её используют срез `history[dropped:]` (`generation.py:729`) и повтор `more` (`generation.py:741`). Для закреплений нужна выборочная политика: вернуть индексы сохраняемых сообщений или «сводка + хвост». Это меняет сигнатуру и тесты `tests/test_context.py:103-132`.
- **Индикатор заполненности.** Функции `question_turn`, `history_turns`, `fit_history`, `fits_alone` чистые, их можно переиспользовать в эндпоинте оценки. Сложность в том, что `tool.plan` асинхронный и ходит в БД (`dialog_tools.py:114-136,258-276`), а поиск в базе знаний до отправки неизвестен — его можно учитывать резервом 8000, как это делает `_check_fits`.
- **Реальный расход.** Добавить `stream_options: {"include_usage": true}` в `_payload` (`bifrost.py:164-168`; это стандартное поле OpenAI, правило `bifrost.py:1` не нарушается), событие `Usage` в `llm/ports.py`, разбор в `_events`, сохранение через `finish_answer` (`repositories.py:229`, как сейчас `dropped_messages`).
- **Точная оценка.** Новая реализация порта `TokenEstimator` (`llm/ports.py:100`) на токенизаторе Qwen (новая зависимость) или калибровка коэффициентов по сохранённому usage.
- **Сжатие (суммаризация).** `ChatModel.complete` и `CompletionModel.complete_with_reason` (`llm/ports.py:76,95`). Образец короткого вызова — `_suggest_title` (`generation.py:826-851`); для фоновой работы есть воркер.
- **Перенос в другой диалог или режим.** Создание диалога — `service.py:226`. Перенесённый контекст нужно класть блоком `data_block("контекст", …)` (`context.py:34-50`) в сообщение пользователя, в `AnswerPlan.question` или в первый ход, но не в system: правило из `context.py:67-68`. Для docparse свободно только около 8192 токенов `dialog_reserve_tokens`.
- **Вид всех запросов.** `AnswerPlan` (`dialogs/ports.py:244-259`) и `DialogTool.plan` — сюда можно добавить поле переносимого контекста для всех видов диалога сразу.

## 7. Риски

- **Неточная оценка.** `chars_per_token=2.0` завышает кириллицу примерно в 1,5 раза, картинка всегда считается за 3200. Индикатор на этой оценке будет показывать «полнее, чем на самом деле», пока коэффициенты не откалиброваны по usage. Переполнение распознаётся по строке ошибки (`app.yaml:60`), это хрупко.
- **Сломанные инварианты.** Закрепления и исключения нарушают два допущения: история начинается с вопроса пользователя (`context.py:122`), а повтор при переполнении отбрасывает именно префикс (`generation.py:741`). Подряд идущие вопросы пользователя уже бывают сейчас (`context.py:88`).
- **Вложения каждый ход.** Вложения и сканы старых вопросов отправляются заново на каждом ходе: тратится контекст, а лимит в 8 картинок вытесняет историю (`context.py:64-77,121`). Исключение вложений даст здесь больше всего.
- **Перенос теряет найденное.** Найденное в базе знаний в историю не попадает, поэтому перенос контекста его теряет. Остаются только процитированные `quote`.
- **Внедрение указаний через сводку.** Сводку пишет модель по недоверенным данным (вложениям, документам). Её нужно передавать как блок данных в сообщении пользователя, а не в system; защиту даёт `data_block`.
- **Перегенерация.** Перегенерация берёт историю до вопроса (`generation.py:313-357`). Закрепления, исключения и сводки должны давать тот же результат и при ней. Правки контекста во время генерации нужно выполнять под той же блокировкой диалога (`generation.py:475`).
- **Все виды диалога сразу.** Логика истории общая для chat, sql, cogis и docparse (`generation.py:706`). У docparse и sql тяжёлая часть (документ, схема) собирается в `plan` каждый раз заново и в историю не входит, поэтому переносить надо и её, иначе потеряется.
- **Что проверить на ВМ.** Пропускает ли Bifrost `usage` в потоке при `stream_options`. Доступен ли `/tokenize` через Bifrost (напрямую к vLLM портал не достаёт, `docker-compose.yml:120-122,367-372`).
- **Порядок работ по процессу.** Новые поля, события и эндпоинты по правилам проекта сначала вносятся в `docs/portal-design.md` и `docs/portal-api.md` (через `portal-architect`) и только потом в код (`CLAUDE.md`, раздел «Правила проекта»).

## data-model
## Модель данных и API диалогов и инструментов: что есть для менеджера контекста

### 1. Таблицы (`portal/backend/src/portal/dialogs/tables.py`, ограничения в `migrations/versions/0002_dialogs.py`)

**`dialogs`** (`tables.py:8-17`): `id`, `owner_id` (FK users, RESTRICT), `kind`, `title`, `created_at`, `updated_at`.
- CHECK `kind IN ('chat','sql','cogis','docparse')` (`0002_dialogs.py:28`).
- Индекс `ix_dialogs_owner_kind_updated` (`0002_dialogs.py:32`).
- У диалога нет полей «родитель», «сводка» или «версия».

**`messages`** (`tables.py:19-41`):
- поля: `id`, `dialog_id` (CASCADE), `position`, `role`, `content`, `status`, `error_code`, `reasoning`, `reasoning_seconds`;
- `params` JSONB — параметры вопроса для повторной генерации;
- `sources` JSONB — снимок источников БЗ с `quote` и `fragment_id`, `sources_found`;
- `sql_check`, `sql_run`, `sql_dangers`;
- `dropped_messages` — сколько сообщений отрезано при усечении;
- `created_at`.

Ограничения:
- UNIQUE `(dialog_id, position)` (`0002_dialogs.py:57`), то есть история строго линейная;
- CHECK `role IN ('user','assistant')` (`:58`);
- CHECK на `status` (`:59`).

**`attachments`** (`tables.py:43-62`):
- `message_id` (NULL — ещё не отправлено), `storage_key`, `text_content` (у записи — расшифровка), `image_pages` int[], поля расшифровки;
- принимаются только в `chat`: `service.py:388-392` (`require_chat`).

**`docparses`** (`tables.py:64-84`): PK = `dialog_id`, плюс `fields` JSONB, `summary`, `summary_status`, `document_text`.

**`sql_schemas`** (`tools/tables.py:7-17`): схемы SQL пользователя. С диалогом связаны только через `messages.params.schema_id`.

Отдельной истории у инструментов нет: `sql`, `cogis` и `docparse` — это строки `dialogs.kind`, и сообщения у них лежат в той же таблице `messages`.

### 2. Жизненный цикл сообщения

**Отправка** — `POST /{id}/messages` (`routes.py:167-183`):
- тело разбирается по виду диалога (`routes.py:136-164`);
- вопрос сохраняется под блокировкой строки диалога (`generation.py:447-461`), позиция = `last.position+1` (`generation.py:253-258`, `:290`);
- ответ сохраняется в конце через `finish_answer` (`repositories.py:216-246`).

**Повторная генерация** — `POST /{id}/regenerate` (`routes.py:186-208`, `generation.py:313-357`):
- работает только для последнего ответа: прежний физически удаляется (`generation.py:353-354`, `repositories.py:208-214`);
- вопрос и его `params` переиспользуются;
- в `sql` можно заменить `schema_id` (`generation.py:342-346`).

**Остановка:** отдельного маршрута нет. Клиент закрывает соединение → `consumer_gone` → `"stopped"` (`watch.py:66-69`) → статус `stopped` (`generation.py:694-699`).

**Нет вообще:**
- правки вопроса или ответа;
- удаления отдельного сообщения через API;
- ветвления, копирования (fork) и снимков.

Уже есть:
- метод репозитория `delete_message` (`repositories.py:208`);
- метод `set_question_params` (`repositories.py:270-278`).

Брошенный `streaming` сбрасывается в `error/interrupted` (`repositories.py:258-268`, `:382-392`).

### 3. «Режимы» и как каждый собирает контекст

Типы в `domain.py`:
- `DialogKind = chat | sql | cogis | docparse` (`domain.py:10`);
- `AnswerMode = fast | thorough` — только в чате (`domain.py:13`).

Действия в параметрах вопроса (`schemas.py:47-67`):
- `sql`: `action` = write, explain, debug, optimize, плюс `dialect`, `schema_id`, `test_run`;
- `cogis`: `action` = write, explain, debug.

Общая сборка запроса для всех видов — `generation.py:701-743`:
- история = все сообщения до вопроса (`repositories.py:169-176`) → `history_turns` (`context.py:80-90`);
- в истории вложения прежних вопросов отправляются заново: текст **и картинки** (`context.py:84-87`);
- размышления и пустые ответы пропускаются;
- `fit_history` отрезает целые сообщения с начала: бюджет и не больше 8 картинок, оставшаяся история начинается с вопроса (`context.py:102-127`);
- если отрезано, идёт событие `context_truncated` (`generation.py:723-724`); фронтенд его обрабатывает (`portal/frontend/src/chat/state.ts:247`);
- если модель отвергла запрос как длинный, один повтор с меньшей историей (`generation.py:735-742`).

Бюджет: `MAX_MODEL_LEN − max_tokens − safety_margin` (`generation.py:404-406`). При поиске в БЗ вычитается ещё `kb.context_max_tokens` (`generation.py:408-418`).

Входные значения:
- `MAX_MODEL_LEN=65536` (`deploy/.env.example:43`; `settings.py:668`);
- `safety_margin_tokens=4096` (`config/app.yaml:57`);
- `chars_per_token=2.0`, `tokens_per_image=3200` (`config/app.yaml:64-65`);
- `context_max_tokens=8000` (`config/kb.yaml:37`);
- `chat.max_output_tokens`: fast 4096, thorough 16384 (`config/app.yaml:118-120`).

Токены оцениваются грубо: `RatioTokenEstimator` (`llm/estimator.py:6-16`).

Что именно уходит в запрос по видам:
- **chat** (`generation.py:386-402`): системное сообщение из конфигурации; вопрос = блоки вложений + текст (`context.py:64-77`). Найденное в БЗ идёт **только в текущий вопрос** и в историю не попадает (`generation.py:713-718`). Сохраняется лишь снимок `sources`.
- **sql** (`tools/dialog_tools.py:114-140`): в системное сообщение входят промпт по `action`, `{dialect}` и блок схемы. Вопрос — сырой текст.
- **cogis** (`dialog_tools.py:203-222`): промпт по `action`; при каждом вопросе всегда идёт поиск `selection={"shared"}, cogis_only=True`.
- **docparse** (`dialog_tools.py:258-276`): в **каждый** вопрос заново подставляются весь `document_text` и таблица реквизитов. В историю попадает только сырой `content`. Сам диалог создаётся маршрутом `POST /api/docparse` (`tools/docparse.py:255-258`). Краткое содержание пишется одним потоковым вызовом модели (`docparse.py:503-541`) и хранится в `docparses.summary`. Это готовый образец «сжатия»: поток, статусы `SummaryStatus` (`domain.py:97`), повторы сохранения.

### 4. Копирование и экспорт сейчас

- Есть только экспорт в DOCX: `GET /{id}/export[?message_id]` (`routes.py:211-227`, `service.py:323-354`, `export.py:173-195`).
- Копирования, переноса в другой диалог, импорта и снимков нет.
- `GET /api/config` отдаёт только `dialogs.message_max_chars` (`core/config_routes.py:24`). Ни `max_model_len`, ни бюджета фронтенду не передаётся, маршрута «заполненность контекста» нет.

### 5. Маршруты (`/api/dialogs`, `dialogs/routes.py`)

| Метод и путь | Строки | Назначение |
|---|---|---|
| `GET ?kind&limit&cursor` | `:72` | список диалогов; пустые скрыты (`repositories.py:85-98`) |
| `POST {kind: chat\|sql\|cogis}` | `:87` | создать диалог (`schemas.py:28`) |
| `GET /{id}` | `:95` | у `docparse` — с результатом разбора |
| `PATCH /{id}` | `:106` | переименовать |
| `DELETE /{id}` | `:114` | удалить |
| `GET /{id}/messages?limit&cursor` | `:121` | курсор по `position` (`repositories.py:159-167`) |
| `POST /{id}/messages` | `:167` | отправить вопрос, ответ потоком |
| `POST /{id}/regenerate` | `:186` | ответить заново |
| `GET /{id}/export` | `:211` | DOCX |
| `POST /{id}/attachments` | `:235` | загрузить файл |
| `POST /{id}/attachments/media` | `:252` | загрузить запись |
| `GET`, `DELETE /{id}/attachments/{aid}` | `:277`, `:285` | вложение |
| `GET /{id}/attachments/{aid}/file` | `:294` | файл вложения |

Инструменты (`tools/routes.py`):
- CRUD `/api/sql/schemas` (`:41-80`);
- `POST /api/docparse` (`:88`).

### 6. Миграции

- Файлы `NNNN_<область>[_vXY].py`: `0001_auth` … `0007_kb_v06`. Строковые `revision = "000N"`, `down_revision` — предыдущая.
- Последняя ревизия **`0007`** (`0007_kb_v06.py:14`).
- В docstring указан раздел `docs/portal-api.md` (§9.20 — 0.15, §9.21 — 0.16).
- Только добавление: `downgrade` поднимает `NotImplementedError` (`0005_dialogs_v05.py:64-68`). Перенос данных делается SQL внутри `upgrade` (`0005:21-61`).
- Тесты: `tests/test_migrations_v05.py:59-83` (`upgrade` до `"0004"`, засев данных старой версии, `upgrade "head"`) и `tests/test_kb_migration.py:59-118`.
- **Ловушка:** номер head зашит в двух тестах — `test_migrations_v05.py:83` и `test_cli.py:175` (`== "0007"`). При добавлении `0008` их нужно обновить.
- Применение миграций: `portal migrate` (`cli.py:64`, `:335`).

### 7. Где хранить сводку, снимок и копию при минимальной правке схемы (`0008_dialogs_context`)

- **Сжатие.** Добавить в `dialogs` поля `context_summary TEXT NULL` и `summary_before_position INT NULL`. В `_answer` (`generation.py:706`) брать историю с `position >= summary_before_position` и ставить сводку первым блоком через `data_block`. Отдельная роль `summary` хуже: нужно менять CHECK роли (`0002:58`), `MessageRole` и `history_turns`. Поток сжатия — по образцу `docparse._summarize`.
- **Оставить только нужное.** Добавить в `messages` поля `in_context BOOL NOT NULL DEFAULT true` и при закреплении `pinned BOOL DEFAULT false`. Фильтр — в `history_turns`; закреплённые сообщения учесть в `fit_history` (сейчас он режет строго с начала). Исключать вложения проще флагом у сообщения: отдельного порядка у вложений нет (`generation.py:283-285`). Места из БЗ в истории сейчас нет вовсе: закреплять их значит хранить `quote` из `messages.sources`, например в `dialogs.pinned_context JSONB`.
- **Перенос в другой диалог того же вида или ветка.** Добавить в `dialogs` поля `parent_dialog_id UUID NULL FK ON DELETE SET NULL` и `forked_from_position INT` и копировать строки `messages` с позициями 0..k. Вложения ссылаться на чужой файл не могут: удаление диалога удаляет файлы по `storage_key` (`service.py:316-319`). Значит, нужно копировать файл, но у `FileStorage` есть только `save`, `path`, `delete`, без copy (`files/ports.py:122-132`).
- **Перенос в другой режим.** Сырые сообщения копировать нельзя: `params` зависят от вида, и `SqlTool.plan` упадёт на `params["action"]` у скопированного вопроса чата при «Ответить заново» (`dialog_tools.py:121`, `generation.py:347-349`). Значит, переносить сводку или выбранный текст как `context_summary` нового диалога. Создание диалога с начальным контекстом — новое тело `POST /api/dialogs`; `docparse` создаётся только маршрутом разбора.
- **Версии в простой форме.** Ветка = копия (пункт выше) или таблица `dialog_snapshots(id, dialog_id FK CASCADE, created_at, label, up_to_position, summary)`. Важно: пустые диалоги удаляются через `empty_ttl_hours` (`service.py:214-233`), а копия с сообщениями под это не попадает.
- **Счётчик заполненности.** Нужен новый `GET /api/dialogs/{id}/context`: оценка через `TokenEstimator` системного сообщения, истории и резерва против `_budget`. Бюджет зависит от `mode` (4096 или 16384) и от включения БЗ (−8000), поэтому нужны query-параметры. Добавить `max_model_len` в `/api/config`.
- Каждое новое поле и маршрут по правилам проекта сначала вносятся в `docs/portal-design.md`, потом через `portal-architect` в `docs/portal-api.md` (§5, §9, новый §9.22).

Файлов не менял. Основные пути: `/Users/ivan/Desktop/ScanControl/LLM/portal/backend/src/portal/dialogs/{tables,domain,context,generation,repositories,routes,service}.py`, `/Users/ivan/Desktop/ScanControl/LLM/portal/backend/src/portal/tools/dialog_tools.py`, `/Users/ivan/Desktop/ScanControl/LLM/portal/backend/migrations/versions/`.

## frontend
Фронтенд портала: где встраивать менеджер контекста

Отвечаю по `portal/frontend/src`. Файлы не менял.

## 1. Каркас и маршруты
- `App.tsx:57-86`. Порядок провайдеров: `MediaUploadsProvider` → `ChatProvider` → `DocparseProvider` → `AppShell`. Маршруты: `/chat/:id?`, `/sql/:id?`, `/cogis/:id?`, `/documents/:id?`, `/knowledge`, `/admin/users`, `/profile`.
- `components/AppShell.tsx:9-20` содержит рейку и `<main>`. `components/AppRail.tsx:20-26` задаёт 5 разделов. Пока идёт разбор, у раздела «Документы» вместо иконки крутится индикатор (`AppRail.tsx:56,67`).

## 2. Экран чата: дерево компонентов
```
ChatScreen (screens/ChatScreen.tsx:6) → DialogScreen kind="chat" params={mode,collections,tags}
 DialogScreen (components/chat/DialogScreen.tsx:138)
 ├─ HistoryPanel (:299) — скрыт, пока открыт DocumentViewer (:298)
 ├─ .area
 │  ├─ PageHeader (:313) [title | settings=headerSettings | editor=TitleEditor(:35)] + Kebab (:331-335)
 │  ├─ props.toolbar (:340), props.banner (:341)
 │  ├─ .body → EmptyState | Loader | Notice | MessageList (:279)
 │  ├─ Notice «длинный диалог» при lastAnswer.dropped_messages>0 (:345-353)
 │  └─ Composer (:355-392)
 ├─ viewer.element (:396) — DocumentViewer, SidePage 560px справа (kb/DocumentViewer.tsx:320; DialogScreen.module.css .withViewer padding-right:560px)
 └─ ConfirmModal удаления (:397)
```
- Состояние и действия: `chat/ChatProvider.tsx`. Интерфейс `ChatApi` в `:79-113`, `useDialogs(kind)` в `:126`, `runStream` в `:527`, `send` в `:600`, `regenerate` в `:684`, `addDialog` в `:700`, `rename`/`remove` в `:713/:717`.
- Редьюсер `chat/state.ts`. Типы: `DialogView` (`:35`), `Draft` (`:61`), `ChatState` (`:76`). Сам редьюсер `chatReducer` в `:316`.
- Режим (`portal.chat.mode`, `ChatProvider.tsx:29-34,182`) и выбранные базы (`chat/knowledge.ts:23-36`) — глобальные и хранятся в localStorage. Теги живут в памяти вкладки. Параметры вопроса в истории не сохраняются (`state.ts:11-13`). Поэтому ни диалог, ни сообщение не знают, с каким режимом и базами их задали. Для переноса контекста это важно.

## 3. Лента сообщений (`components/chat/MessageList.tsx`)
- Лента — протокол: слева «Вы»/«Ответ» (`:332-381`). Вопрос — текст и `AttachmentChip`ы (`:337-359`), ниже предупреждения `sql_dangers` (`:362-366`).
- Ответ рендерит `Answer` (`:90`): `ThinkingBlock`, `MarkdownView` (с `sql_check`/`sql_run`), `SourceFigure`, `SourceCard` (`:146-180`), строки «ничего не найдено», «остановлен», `length_limit` (`:182-202`).
- Строка действий ответа (`:203-238`): «Скопировать», «Ответить заново» (только у последнего), «Скачать в DOCX». Все три — `Link component="button"`. У вопроса пользователя строки действий нет.
- «Показать более ранние сообщения» — подгрузка по курсору `olderCursor` (`:325-331`). К контексту не относится.
- Автопрокрутка и кнопка «К последнему» — `:296-312`, `:387-399`.

## 4. Панель ввода (`components/chat/Composer.tsx`)
- Порядок: вложения (`:113-136`), `Textarea` (до 10 строк, `:137-152`), строка `.controls` (`:153-201`).
- Только в чате (`chat?: ChatControls`, `:23-37`):
  - «Прикрепить» (`:164-171`);
  - `Switcher` «Быстро / Вдумчиво» (`:172-178`, `MODES` в `:17-20`);
  - `KnowledgePicker` (`:179-185`): кнопка и `Tooltip`-панель с `Checkbox` по scope и `TokenInput` для тегов (`KnowledgePicker.tsx:82-147`).
- Слот `controls` (`:188`) занимает действие SQL. Справа прижата кнопка «Отправить»/«Остановить» (`.submit {margin-left:auto}`, `Composer.module.css:32`).
- Под полем: подсказка (`:203`), `note` (`:206`), `draft.notice` с `role="alert"` (`:207-211`). Строка `.controls` переносится (`flex-wrap`, `Composer.module.css:25-30`).

## 5. Боковая панель диалогов (`components/chat/HistoryPanel.tsx`)
- Кнопка «Новый …» (`:46`). Группы «Сегодня / Вчера / На этой неделе / Раньше» (`:13,38-41`), «Показать ещё» (`:91-97`).
- Меню строки `Kebab` (`:78-82`): «Переименовать» (необязательно, `onRename?`), «Скачать в DOCX», «Удалить». Такое же меню в заголовке (`DialogScreen.tsx:331-335`) и в разборе документов (`DocumentsScreen.tsx` ~`:424-429`, без «Переименовать»).
- Удаление — `ConfirmModal`. Переименование — правка названия на месте, `TitleEditor` (`DialogScreen.tsx:35-99`).

## 6. Инструменты («режимы»)
Все инструменты построены как диалоги: `DialogKind = 'chat'|'sql'|'cogis'|'docparse'` (`api/types.ts:130`). Параметры вопроса у каждого вида свои, `QuestionParams` в `types.ts:268-272`.

| Инструмент | Как устроен | Параметры вопроса |
|---|---|---|
| SQL (`screens/SqlScreen.tsx:203-298`) | `DialogScreen kind="sql"` | `{action, dialect, schema_id, test_run}`. Действие — `Switcher` в `composerControls` (`:287`). Диалект, схема и проверка на тестовых данных — `Select`/`Toggle` в `headerSettings` (`:234-285`). «Мои схемы» — `SidePage` `SchemasPanel` (`sql/SchemasPanel.tsx:154`, width 640, `blockBackground`) |
| CoGIS (`screens/CogisScreen.tsx:40-68`) | `DialogScreen kind="cogis"` | `{action}`. Действие — `Switcher` в `toolbar` (`:50-53`). Если документации нет, `banner` с предупреждением (`:55-66`) |
| Разбор документов (`screens/DocumentsScreen.tsx:250-457`) | Свой экран, но собран из тех же `HistoryPanel` (~`:414`), `MessageList` с `header=DocparseResultView` (~`:385`) и `Composer` без chat-контролов (~`:398`) | `chat.send(id, {}, …)`. Сам разбор идёт отдельным потоком в `docparse/DocparseProvider.tsx:251-283`: `progress`, `extraction_started`, `extraction`, `delta` |

Вложения, режим и база знаний есть только у чата (`ChatProvider.tsx` ~`:603`, `isChat = 'mode' in params`). При переносе в инструмент их придётся отбрасывать или превращать в текст.

## 7. Поток SSE
- Разбор потока: `api/stream.ts`. Тип `StreamEvent` (`:6-21`) и белый список `KNOWN_EVENTS` (`:23-39`). Неизвестные события молча пропускаются (`:41-44`), поэтому новое событие нужно добавить в оба места.
- Типы событий: `start`, `sql_question_check`, `sql_check`, `sql_run`, `progress`, `extraction_started`, `extraction`, `search_started`, `sources`, `context_truncated{dropped_messages}`, `reasoning_delta`, `delta`, `title`, `done{status:'complete'|'length_limit'}`, `error{code,message,details?}`.
- Применение к состоянию: `applyStreamEvent` в `chat/state.ts:190-310`. `context_truncated` записывает `dropped_messages` в последний ответ (`state.ts:247-248`).
- Цикл чтения: `ChatProvider.tsx` ~`:549-557`. Там же `error dialog_deleted`: черновик переносится в новый диалог (`draftMoved`).
- Вызовы API: `api/client.ts`. `sendMessage` (`:345`), `regenerate` (`:349`), `exportDialog` (`:353`), `listMessages` (`:338`), `createDialog` (`:329`), `renameDialog` (`:333`).

## 8. Что уже говорит об обрезке контекста
- `Message.dropped_messages` (`types.ts:258`) и событие `context_truncated` → `Notice kind="info"` над панелью ввода (`DialogScreen.tsx:345-353`, `DocumentsScreen.tsx` ~`:434-438`). У чата, SQL и CoGIS есть кнопка «Новый …» (`longDialogAction`).
- Тексты: `texts.ts:445,457,469,482-484` («самые ранние сообщения модель больше не учитывает…»). У docparse `longDialogAction:false`.
- Тесты на это: `ChatScreen.test.tsx:491-495`, `ToolScreens.test.tsx:327-331,1476`, `state.test.ts:171-178`.
- Похожее, но к контексту не относится: `length_limit` ответа (`texts.ts:290-291`, `MessageList.tsx:195-197`), `SqlRunRows.truncated`, `KbDocument.images_truncated`.
- Индикатора заполненности нет совсем. В `PortalConfig` (`types.ts:71-109`) нет ни лимита контекста, ни счёта токенов.

## 9. Тексты, темы, тесты
- Все строки в `texts.ts` (1096 строк), объект `texts` с `:103`. Тексты по видам диалога — `texts.dialogs[kind]` (`:435-486`), тексты чата — `texts.chat` (`:245+`). Проверяет их `texts.test.ts`. Новые строки добавляются туда же, в `dialogs[kind]` или новую секцию `context`.
- Темы: `theme/ThemeProvider.tsx:29-30` (`LIGHT_THEME`/`DARK_THEME` через `ThemeFactory`) и `theme/ThemeBridge.tsx:18-30` (CSS-переменные `--p-*`). Стили — CSS modules, цвета только через `--p-*` (`styles/global.css:3`).
- Готовая полоса заполнения — `components/ProgressBar.tsx:10` с `role=progressbar` и aria.
- Тесты: vitest 5.0.3 + jsdom (`vite.config.ts` `test`, `src/test/setup.ts`), моки API в `src/test/mockApi.ts`. 16 файлов, 241 `it`; крупнейшие — `ChatScreen.test.tsx` (63) и `ToolScreens.test.tsx` (48). Playwright: `e2e/tests` (18 файлов, около 160 `test(`), из них `chat.e2e.ts` (15), `tools.e2e.ts` (14), `states.e2e.ts` (21). Полный прогон — `npm run check` (typecheck, lint, test, build).
- `@skbkontur/react-ui` 6.4.2. Чаще всего используются `Button`, `SingleToast`, `Link`, `Spinner`, `Input`, `Hint`, `Loader`, `MenuItem`, `Kebab`, `Gapped`, `ScrollContainer`, `Modal`, `Switcher`, `Tooltip`, `TokenInput`, `RadioGroup`, `Checkbox`. По одному разу: `SidePage`, `Select`, `Toggle`, `DropdownMenu`, `MiniModal`, `Paging`, `FileUploader`. `Tabs` не используется.

## 10. Куда встраивать
1. **Индикатор заполненности.** Строка `.controls` в `Composer.tsx:153-200`, перед `.submit`: компактная кнопка «Контекст: 63 %» с полосой `ProgressBar`, по нажатию открывает панель контекста. Сделать его новым общим пропсом `Composer` (например, `context?`), а не через `controls`: слот `controls` занят SQL, а разбор документов `controls` не передаёт. Тогда индикатор будет во всех четырёх видах. `headerSettings` не подходит: его занимает SQL. Данных для индикатора сейчас нет. Нужно новое SSE-событие (например, `context_usage {used_tokens, limit_tokens}`) в `stream.ts:6,23` и `applyStreamEvent` (`state.ts:190`), поле в `DialogView` или `Message`, а для открытого без потока диалога — REST-запрос по аналогии с `client.ts:338`. Это работа `portal-architect`/`backend-engineer`.
2. **Панель контекста.** `SidePage` по образцу `SchemasPanel`: модальная, `blockBackground`, `sql/SchemasPanel.tsx:154`. Монтировать в `DialogScreen` рядом с `ConfirmModal` (`:397`) и отдельно в `DocumentsScreen`. Немодальный вариант как у `DocumentViewer` (`blockBackground={false}`, раскладка `.withViewer`) конфликтует с просмотром источника за правую половину экрана. Значит, либо панели взаимоисключающие, либо панель модальная. Рекомендую модальную. В панели: список сообщений с переключателем «учитывать / исключить / закрепить», вложения и источники (`Checkbox`), шкала, кнопки «Сжать» и «Перенести», снимки.
3. **Пометки на сообщениях.** Строка действий ответа (`MessageList.tsx:203-238`), туда добавляется `Link`, например «Не учитывать». У вопроса строку действий придётся завести (`:336-367`). Исключённым сообщениям — приглушённый стиль на `.row` (`:333`). Альтернатива проще: пометки только в панели контекста, без правки ленты.
4. **«Перенести в новый чат или инструмент».** Пункт в `Kebab` заголовка (`DialogScreen.tsx:332-334`) и строки истории (`HistoryPanel.tsx:79-81`, проп по образцу `onRename?`), плюс кнопка в панели контекста. Выбор цели: `Modal` с `RadioGroup` (чат / SQL / CoGIS). Вложенных меню в `Kebab` проект не использует. После создания — `addDialog` (`ChatProvider.tsx:700`) и `navigate(`/${kind}/${id}`)`.
5. **«Сжать».** Действие в `Notice` «длинный диалог» (`DialogScreen.tsx:347-352`): вместо или рядом с «Новый чат», это естественная точка входа. Ещё — в `Kebab` и панели контекста. Если сжатие идёт потоком, удобно переиспользовать `runStream` (`ChatProvider.tsx:527`). Результат показывать отдельной строкой ленты («Сводка вместо N сообщений»), новый вид строки в `MessageList.tsx:332`.
6. **Версии.** Сейчас «Ответить заново» заменяет последний ответ, версий в интерфейсе нет. Снимки или ветки проще всего держать в той же панели как список с «Восстановить» или «Открыть копией». В истории копия появится как обычный новый диалог через `addDialog`.

Внимание: в `DocumentsScreen` повторена логика `DialogScreen` (заголовок, `Kebab`, `Notice`, `Composer`). Любой общий элемент менеджера контекста придётся встраивать в оба экрана.

## design-docs
# Отчёт: что в дизайн-документах сказано о контексте и что затронет менеджер контекста

## 1. Версии документов и как в них записываются изменения

| Документ | Версия | Как записываются изменения |
|---|---|---|
| `docs/portal-design.md` (ПД) | **0.6**: «Версия 0.6 — решения владельца от 2026-10-18 (версия 0.5 утверждена 2026-10-11)» (`docs/portal-design.md:3`) | Абзацы «Изменение 0.N (повод, дата): что поменялось (§…)» в шапке (`:10`, `:13`, `:26`, `:33`, `:51`); отдельно «Уточнение дата» (`:7`). Решения сведены в таблицу §2 (`:96-119`), решения оркестратора — в нумерованный список §12 (`:1369+`, пп. 1–38). Порядок выкатки каждой версии — свой подраздел §8.N: §8.2 для 0.5 (`:1007`), §8.3 для 0.6 (`:1124`). Критерии приёмки нумеруются сквозь версии (§10: 1–14, 15–25 для 0.5, 26–28 для 0.6, `:1182-1305`). |
| `docs/portal-api.md` (ПК) | **0.16**, «на утверждение», 2026-10-18, сверен с ПД 0.6 (`docs/portal-api.md:3-11`) | Блоки «Изменение 0.N (повод, ПД версии X)» в шапке со списком «- **Тема**: … (§…)» (`:14`, `:83`, `:153`…`:234`). Внутри 0.16 есть подпункты «Правки по ревью 0.16», «Решения по вопросам 0.16 (… номер версии прежний)» (`:42`, `:51`). Допущения: А-n в §15.2 (`:6004`, до А-78). Открытые вопросы: §15.4 (`:6139`). Миграции получают подраздел §9.2x (`:3724`, `:3747`). |
| `docs/portal-ui.md` (ПИ) | **0.13**, «на утверждение», 2026-10-18 (`docs/portal-ui.md:3`) | «Изменение 0.N (под ПД X и ПК Y): …» (`:10-68`). Допущения: Д-n в §11.2 (`:3201`). Запросы к контракту: API-n в §12 (`:3233`). Чек-лист дизайн-ревью: §13 (`:3306`). |
| `docs/design.md` | 0.1 от 2026-09-15 (`docs/design.md:3`) | Блок «Изменения дата (…)» (`:5`) |

## 2. Что уже определено

### Окно модели и параллельность (`docs/design.md`)
- `--max-model-len 65536`, `--max-num-seqs 32`, `--limit-mm-per-prompt '{"image": 8}'`, `--enable-prefix-caching` (`docs/design.md:319-324`).
- «Критерий приёмки: ≥ 8 параллельных запросов при `max-model-len` 65536» (`:309-310`); после снижения доли видеопамяти до ~0.81 критерий перепроверяется на ВМ (`:311-312`, `:514`).
- «Контекст 64k покрывает ~100 страниц текста… расширить до 262k можно переменной, но это уменьшит параллельность» (`:333-334`). Переменная `MAX_MODEL_LEN` (`:347`).
- Это же повторено в ПД: `docs/portal-design.md:306-307`, `:1064`.

### Чат (ПД §5.1)
- Единственное требование о длине: «Портал сам следит за длиной контекста (65 536 токенов): при переполнении старые сообщения отбрасываются, пользователю показывается заметка об этом» (`docs/portal-design.md:452-453`).
- Повторная генерация — только последнего ответа (`:424`).
- Вложений больше 8 изображений или страниц отклоняются целиком (`:426-429`).

### Сборка запроса к модели (ПК §5.4, `docs/portal-api.md:1392-1465`)
- Состав запроса:
  1. Системное сообщение из YAML.
  2. История: «вопросы и ответы по порядку. Размышления в историю не входят. Ответ с пустым текстом… пропускается» (`:1399-1400`).
  3. Текущий вопрос: перед ним найденные места базы знаний, текст разбора, текст вложений (`:1401-1405`).
- Вложения прежних вопросов (текст и картинки) входят в историю. В ПК это сказано только косвенно: лимит 8 изображений соблюдается отбрасыванием истории (`:1453`). В коде подтверждено: `history_turns` → `question_turn(message.content, attachments…)` (`portal/backend/src/portal/dialogs/context.py:80-90`), подсчёт картинок истории — `context.py:119`.
- Найденные места базы знаний: «подставляются только в текущий вопрос, в историю они не попадают» (`docs/portal-api.md:1420-1421`). На них резерв `kb.context_max_tokens` = 8000 (`:1440-1443`, `:5724`).
- Токены оцениваются, а не считаются: «токенизатор модели порталу через Bifrost недоступен **[А-3]**» (`:1447-1448`). Параметры: `llm.chars_per_token` / `llm.tokens_per_image` = 2.0 / 3200 (`:5690` примерно; в таблице §13.6), порт `TokenEstimator.text/image` (`:5031-5033`).
- Бюджет: `MAX_MODEL_LEN − предел длины ответа − llm.safety_margin_tokens` (`:1449`).
  - `llm.safety_margin_tokens` = 4096 (`:5689`).
  - `chat.max_output_tokens.fast` / `.thorough` = 4096 / 16384 (таблица §13.6).
  - Для «Вдумчиво» остаётся около 45 тыс. токенов на запрос.
- Отбрасывание: «История отбрасывается целыми сообщениями, начиная с самых ранних… не больше 8 изображений **[А-4]**. Оставшаяся история начинается с вопроса пользователя» (`:1452-1454`). Если модель всё равно отказала, один повтор с отбрасыванием «не меньше половины оставшейся» (`:1460-1463`).
- Если системное сообщение и текущий вопрос не помещаются сами — `422 message_too_long`, поток не открывается (`:1450-1451`).
- Допущения: А-3 «Оценка по коэффициентам… точного счёта нет», А-4 «Целые сообщения, начиная с ранних» (`:6012-6013`). А-2: при повторной генерации «прежний не хранится» (`:6011`), то есть вариантов ответа нет.
- `Message.dropped_messages`: «Сколько ранних сообщений не вошло в запрос к модели при этом ответе» (`:1223`), столбец `messages.dropped_messages` (`:3507`).
- Событие SSE `context_truncated { dropped_messages }`: «Не больше одного раза… После поиска» (`:1784`). Порядок событий — `:1793-1795`.
- Переписывание запроса для поиска берёт последние `kb.rewrite.history_messages` / `history_max_chars` = 6 / 4000 (`:2969`, `:5795`).
- Разбор документов: документ целиком входит в каждый запрос, «Частями он не подаётся и не обрезается» (`:2187-2189`). Бюджет `MAX_MODEL_LEN − docparse.max_output_tokens − llm.safety_margin_tokens − docparse.dialog_reserve_tokens` (`:2192-2193`). Резерв покрывает в том числе «историю вопросов» (`:2196-2197`).
- Извлечённый текст вложения хранится не длиннее `MAX_MODEL_LEN × llm.chars_per_token` символов (`:1657-1658`).

### «Режимы» = виды диалога
- «одна сущность **диалог** с полем `kind`: `chat` | `sql` | `cogis` | `docparse`. У каждого вида своя история» (`docs/portal-api.md:1162-1164`).
- Маршруты:
  - `POST /api/dialogs` принимает только `chat|sql|cogis` (`:1301`);
  - диалог `docparse` создаётся только через `POST /api/docparse` (`:1315`);
  - вложения есть только у `chat`, иначе `409 wrong_dialog_kind` (`:1645`).
- Параметры вопроса лежат в `messages.params` (`:1388-1390`, `:3514-3515`).
- Модель данных: `dialogs (id, owner_id, kind, title, created_at, updated_at)` (`:3476-3486`); `messages.position UNIQUE (dialog_id, position)` (`:3494`). Полей родителя, ветки, версии, закрепления или исключения нет.

### Интерфейс (ПИ)
- Заметка о длинном диалоге: `Notice` над панелью запроса, пока `dropped_messages > 0`: «Диалог стал слишком длинным: самые ранние сообщения модель больше не учитывает. Для новой темы лучше начать новый чат». Действие — `Link` «Новый чат» (`docs/portal-ui.md:1326-1331`). Варианты текста для SQL, CoGIS и разбора — `:1805`.
- Состояние потока «История урезана» (`:1232`).
- Словарь запрещает в интерфейсе слова «контекст, окно, токены» (`:2957`), а также «чанки, фрагменты, контекст» (`:2946`).
- Действия под ответом: «Скопировать · Ответить заново · Скачать в DOCX» (`:433`). «Ответить заново» есть только у последнего ответа и заменяет его, «прежний вариант не хранится» (`:494-495`).
- Лента показывает последние 50 сообщений, выше — «Показать более ранние» (`:502`).
- Индикатора заполненности контекста нет нигде.

## 3. Что прямо или косвенно вне рамок

- Список «Вне рамок» в ПД (`docs/portal-design.md:82-87`): самостоятельная регистрация, SSO, «библиотека шаблонов запросов», «совместные чаты», агентный поиск, «вызов инструментов моделью (tool calling)». Ветвления, редактирования сообщений, сжатия, переноса и закреплений в нём нет. Их просто нигде не описывают, а правило «не делаем, пока не внесено сюда» (`:82`) означает, что всё это сначала вносится в ПД.
- Сводки (summary) истории нет нигде. Есть только «краткое содержание» документа в разборе (ПД §5.5, `:698`), а «Оборванное краткое содержание заново не формируется» (`:701`).
- Редактирования вопроса нет: в ПИ только «Ответить заново» для последнего ответа (`docs/portal-ui.md:494`), в ПК только `regenerate` (`docs/portal-api.md:1599-1604`).
- Версий ответов нет (А-2, `docs/portal-api.md:6011`).
- В `Message` нет счёта токенов, только `dropped_messages`. Точного счёта токенов нет (А-3).
- Сервер не хранит выбор коллекций базы знаний: «Последний выбор интерфейс запоминает в браузере; сервер его не хранит» (`docs/portal-api.md:1360-1361`).

## 4. Какие разделы затронет менеджер контекста

### `docs/portal-design.md` → 0.7
- Шапка: абзац «Изменение 0.7 (…)».
- §1 «Вне рамок» (`:82-87`): что из ветвления и редактирования остаётся вне рамок.
- §2 «Принятые решения» (`:96-119`): строки «Менеджер контекста», «Сжатие», «Версии/снимки».
- §5.1 Чат (`:452-453`): заменить пункт об отбрасывании на индикатор заполненности, сжатие, закрепление и исключение, перенос.
- §5.3, §5.4, §5.5: перенос в режимы и из режимов (история у каждого инструмента своя; у `docparse` резерв истории — `docparse.dialog_reserve_tokens`).
- §5.2: если менеджер позволит закреплять найденные места базы знаний (сейчас они в историю не попадают).
- §5.6: экспорт сводки или снимка.
- §6 Интерфейс (`:714-757`): новый элемент, словарь.
- §7: новые параметры YAML и, возможно, флаг функции (таблица флагов `:784-790`).
- §3.4 (`:300-312`): время GPU на сжатие.
- §8 (`:823+`): недоверенный текст в сводке, изоляция владельца при переносе.
- Новый §8.4 «Обновление работающего сервиса (изменение 0.7)» по образцу §8.3 (`:1124-1145`): миграция, порядок выкатки, откат.
- §10: критерии 29+.
- §11: риски (качество сводки, оценка токенов).
- §12, если будут решения оркестратора.

### `docs/portal-api.md` → 0.17
- Шапка: «Изменение 0.17».
- §4 `GET /api/config`: отдать бюджет, если интерфейс считает заполненность.
- §5.1 `Message` и `Dialog`: поля заполненности, закреплено/исключено, сводка, родитель или источник.
- §5.2 маршруты: сжатие, перенос, снимки, исключение и закрепление.
- §5.3: тело запроса.
- **§5.4 «Что уходит модели»** (`:1392-1465`) — главная точка: новый порядок сборки (сводка, закреплённое, история) и новые правила отбрасывания вместо А-4.
- §5.6: повторная генерация при снимках, А-2.
- §5.9: экспорт.
- §6.2 / §6.3: новые события SSE (например, заполненность после `context_truncated`) и коды ошибок.
- §7.1–7.3: перенос в `sql`, `cogis`, `docparse`.
- §9.6 / §9.7 и новые таблицы, плюс миграция в §9.22 (по образцу `:3747`).
- §9.15 / §9.16: изоляция владельца и каскад удаления.
- §12.1: запрос сжатия к модели.
- §12.2: ответ заглушки стенда на запрос сжатия.
- §13.1 / §13.3: модуль `portal/dialogs/` (`:4895`) и порт `TokenEstimator` (`:5031`).
- §13.6: параметры.
- §14: проверка «чужие данные».
- §15.2 / §15.4: новые А-n и открытые вопросы.

### `docs/portal-ui.md` → 0.14
- Шапка: «Изменение 0.14».
- §4.1 `Composer` (`:299`): индикатор заполненности рядом с «Быстро / Вдумчиво».
- §4.2 лента (`:411`, действия `:433`, `:494`): закрепить/исключить, «Перенести».
- §4.6 `Notice`.
- §5.5 Чат: заметка о длинном диалоге (`:1326-1331`), меню заголовка и истории.
- §5.7–5.9: инструменты, тексты `:1805`.
- §6: сводка состояний (`:2641`).
- **§8.2 словарь** (`:2946`, `:2957`): сейчас слово «контекст» запрещено, нужен термин для пользователя.
- §8.3: реестр текстов.
- §9: доступность.
- §10: свои компоненты.
- §11.2: Д-n.
- §12: API-n (API-6, API-8 `:3248`).
- §13: чек-лист.

### `docs/design.md`
- §5 и §6 (`:295-334`) не трогаются, если `MAX_MODEL_LEN` не меняется. Если сжатие добавит обращения к модели, нужна заметка о нагрузке в §13 (риски, `:508`).

### Порядок по правилам проекта
Владелец правит ПД (агентам это запрещено), затем `portal-architect` делает ПК, `ui-designer` — ПИ (`CLAUDE.md` проекта, этап 1 пайплайна).

**Внимание:** две точки требуют решения до проектирования.
- Термин «контекст» запрещён словарём ПИ (`docs/portal-ui.md:2957`).
- Точного счёта токенов нет (А-3). Индикатор заполненности будет оценкой с запасом: `chars_per_token` = 2.0 при запасе 4096 токенов. Без нового доступа к токенизатору (через Bifrost он недоступен) показанный процент будет завышен.

## release
**Как выкатывать изменения на работающий портал и шаблон release-0.7.md**

## 1. Ветки и коммиты
- `git log` устроен так: один коммит `feat(portal): …` на пакет в ветке `feat/*`, потом владелец сливает её в `main` merge-коммитом (`5e6451d` → `5e35a5d Merge branch 'feat/kb-limits'…`; `443c75c`/`8dddc89` → `2d4727f`). Локальные ветки: `feat/kb-limits`, `feat/portal`, `main`.
- Вместе с кодом в коммит релиза входят и документы. Для 0.6 это `docs/release-0.6.md` (новый), `docs/runbook.md`, `docs/user-guide.md`, `docs/vpn-launch.md`, `docs/portal-api.md`, `docs/portal-design.md` (§8.3), `docs/portal-ui.md`, а также `Makefile`, `deploy/caddy/Caddyfile`, `deploy/bifrost/config.json` (`git show --stat 5e6451d`).
- В дизайне на каждый релиз есть свой раздел: `docs/portal-design.md:1007` (§8.2, 0.5) и `docs/portal-design.md:1124` (§8.3, 0.6). Для нашей фичи нужен §8.4 «Обновление работающего сервиса (изменение 0.7)». Его вносит владелец до кода (правило CLAUDE.md).

## 2. Порядок релиза
Источники: `scripts/release.sh:10-21`, `docs/runbook.md:1096-1386`, `docs/release-0.6.md`.

На ВМ: `LIVE=/root/LLM/llmDeploy` (рабочий клон), `REL=/root/LLM/llmRelease` (релизный клон), `BAK=/var/lib/docker/portal-backups` (`docs/release-0.6.md:7-9`). Команды выполняются от root в `tmux`; при ошибке работа останавливается (`docs/release-0.6.md:3-4`).

**До окна (сервис работает):**
1. Релизный клон: `git clone … "$REL" && git checkout <релиз>` (`docs/release-0.6.md:33`).
2. `cd "$REL" && make release-prepare LIVE="$LIVE" PROFILES=embeddings,rerank GPU_MEM_UTIL=0.81` (`Makefile:221`, вызывает `install.sh --prepare-from`, `scripts/install.sh:21-30,901`). Что делает команда:
   - запускает preflight `--release`, нужно ≥ 135 ГБ (`docs/runbook.md:1121-1126`);
   - вешает на текущие образы портала тег коммита рабочего клона, это путь отката;
   - собирает `portal-api`, `portal-worker`, `portal-web`, `sql-sandbox` с тегом `rev-parse --short=12` (`scripts/install.sh:218-219,561-571`);
   - загружает веса;
   - печатает готовые команды окна и отката.
   
   `PROFILES` и `GPU_MEM_UTIL` должны совпадать с `make portal` в окне (`docs/runbook.md:1115-1117`).
3. `make release-rehearse LIVE="$LIVE"` (`Makefile:226`, `scripts/release.sh:84-122`):
   - `pg_dump` рабочей базы загружается во временный PostgreSQL в изолированной сети;
   - на нём из образа релиза выполняется `portal migrate`;
   - признак успеха — строка «репетиция миграций прошла за N с».

**Окно (объявить сотрудникам и владельцам ключей API):**
4. `cd "$REL" && make release-backup LIVE="$LIVE" DIR="$BAK"` (`Makefile:231`). Копия включает `portal-db.dump`, `portal-files.tgz`, `qdrant-data.tgz`, `bifrost-data.tgz` и `SHA256SUMS`. На время копии `portal-api`, `portal-worker`, `qdrant` и `bifrost` останавливаются (`scripts/backup.sh:24-35`). Каталог копии не может лежать на диске `/`.
5. `cd "$LIVE" && git checkout <релиз>`. С этого момента и до успешного `make portal` нельзя выполнять `make up` и перезагружать ВМ (`docs/runbook.md:1216-1220`).
6. `cd "$LIVE" && make portal PROFILES=… GPU_MEM_UTIL=…` (`Makefile:163`, `install.sh --stage portal --skip-preflight`). Порядок внутри (`docs/runbook.md:1228-1251`):
   - прежний `.env` сохраняется в `deploy/rollback/<прежний тег>/.env` (`scripts/install.sh:377,444`);
   - в `.env` дописываются новые переменные из `.env.example` и выставляется `PORTAL_IMAGE_TAG` (`scripts/install.sh:453-454`);
   - `compose pull` (`:516`); сборка пропускается, если образы с этим тегом уже есть и в `portal/` нет незакоммиченных правок (`scripts/install.sh:552-566`);
   - сначала модели, каждая после healthcheck предыдущей (`scripts/install.sh:613-634`), затем остальные сервисы через `stack_up.sh --wait`;
   - Bifrost и Caddy всегда пересоздаются: `compose up -d --force-recreate --no-deps --wait bifrost caddy` (`scripts/install.sh:644-648`). Сайт и `/v1` недоступны несколько секунд.
7. Проверка:
   - `make ps`, `make key NAME=smoke REQUESTS=100`, `make smoke KEY=…`, `make smoke-portal`, `make revoke ID=…`;
   - сверка `sha256sum` Caddyfile в контейнере и на диске;
   - ручной сценарий в браузере (`docs/release-0.6.md:54-69`).

**После окна:** фоновые задачи релиза, если есть (в 0.6 это `portal-reprocess-*`, `docs/release-0.6.md:73-80`). Закрытие релиза: `rm -rf "$REL" "$LIVE/deploy/rollback"` (в `deploy/rollback` лежат секреты прежнего `.env`), копию увезти с ВМ. До закрытия нельзя выполнять `docker image prune` (`docs/runbook.md:1140`).

## 3. Миграции
- Alembic: `portal/backend/migrations/versions/0001…0007_kb_v06.py`. Следующая будет `0008_*` с `down_revision="0007"`.
- Миграции применяются при старте контейнера: `CMD ["sh", "-c", "portal migrate && exec portal serve"]` (`portal/backend/Dockerfile:38`; `portal/backend/src/portal/cli.py:64-68` делает `command.upgrade(config, "head")`).
- `portal-worker` зависит от `portal-api`, поэтому стартует на уже готовой схеме (`deploy/docker-compose.yml:392-394`).
- Обратного хода нет. `downgrade()` бросает `NotImplementedError` со ссылкой на восстановление из копии (`portal/backend/migrations/versions/0007_kb_v06.py:28-31`). Для 0008 надо делать так же и упомянуть это в разделе «Откат».
- Есть тест миграций `portal/backend/tests/test_migrations_v05.py`. Для 0008 понадобится аналогичный тест.

## 4. Откат
Таблица состояний — `docs/runbook.md:1331-1337`, команды — `docs/release-0.6.md:82-99`. Последовательность: `git checkout <прежний коммит>` в LIVE, затем из `REL` одно из двух:
- `make release-rollback LIVE="$LIVE"` — без копии, если новый `portal-api` ещё не стартовал;
- `make release-rollback LIVE="$LIVE" FROM=<копия>` — если миграции уже применены. Команда проверяет копию и просит ввести `yes`; всё созданное после копии теряется (`scripts/release.sh:35-45,180`).

Как понять, какое состояние наступило:
- `ls -A "$LIVE/deploy/rollback"`;
- `docker ps -a --filter name=portal-api --format '{{.Image}}'`: если тег равен 12 символам коммита релиза, стартовал уже новый `portal-api`.

Первая линия при сбое одной функции — флаг (`docs/runbook.md:714-743`, §3.8): ключ `<модуль>.enabled: false` в `deploy/portal/config.override.yaml`, затем `docker compose restart portal-api portal-worker`. Пересборка и откат не нужны. Накладка объединяет словари, а списки и скаляры заменяет целиком (`docs/runbook.md:576-591`).

## 5. Что важно для менеджера контекста
- **Размер контекста.** Портал берёт его только из `MAX_MODEL_LEN`: переменная передаётся в `portal-api` (`deploy/docker-compose.yml:347-348`), та же, что у vLLM `--max-model-len` (`:140-141`). Индикатору заполненности новая переменная `.env` не нужна. Если фича изменит `MAX_MODEL_LEN` или аргументы vLLM, `make portal` пересоздаст модель, а это минуты простоя чата и `/v1`. Этого лучше избежать.
- **Флаг.** Рекомендую дать фиче флаг `context.enabled` (или по подфункциям, например `context.compress.enabled`) в `portal/backend/config/app.yaml` и добавить его в таблицу §3.8. Тогда для неё работает дешёвый откат без восстановления копии.
- **Кэш фронтенда.** Статика собирается в образ `portal-web` (Caddy внутри, `portal/frontend/Dockerfile:13-68`). У ассетов стоит `Cache-Control … immutable` (`:34`), у `index.html` — `no-cache` (`:40`). После релиза браузеры получат новый интерфейс без ручной очистки кэша. Уже открытые вкладки работают со старым JS до перезагрузки — стоит упомянуть в памятке.
- **Новые переменные `.env`.** Дописываются автоматически из `.env.example`; в release-доке писать «Новых переменных `.env` в 0.7 нет / есть: …» (`docs/release-0.6.md:14`).
- **Сжатие контекста.** Если суммаризация пойдёт через модель, она нагружает GPU. Это проверяется на ВМ и вносится в раздел «Что замерить на ВМ».
- **Bifrost и Caddy.** Если `deploy/bifrost/config.json` и `Caddyfile` не меняются, их пересоздание всё равно произойдёт (короткий перерыв). Шаги проверки 8–9 из 0.6 тогда сокращаются.
- **Миграция данных.** Если версии или снимки контекста потребуют миграции существующих диалогов, это делается внутри 0008 без ручного шага. Если нужен фоновый пересчёт, по образцу `portal-reprocess-*` выносится в «После окна».

## 6. Шаблон `docs/release-0.7.md`
По образцу `docs/release-0.6.md`, около 100 строк; 0.5 был длинным (417 строк) из-за переноса containerd, повторять его не нужно.

```
# Релиз 0.7: менеджер контекста — раскатка на работающий 0.6
Пояснения — runbook.md §…, §4.6. Всё — [ВМ]. Команды от root, в tmux. Ошибка — стоп.
export LIVE=… REL=… BAK=…; grep COMPOSE_PROFILES|GPU_MEM_UTIL из .env
PROFILES/GPU_MEM_UTIL — подставить. Новых переменных .env: нет|список.
## Перед началом      — работает 0.6 (make ps, git log -1), origin/main = <релиз>, status пусто, df, секреты вне ВМ, [накладка не конфликтует с новыми ключами]
## Подготовка          — 1 клон REL; 2 make release-prepare; 3 make release-rehearse («применяется 0008»)
## Бэкап (начало окна) — 4 make release-backup; предупредить про /v1 (пересоздание Bifrost)
## Обновление          — 5 git checkout <релиз>; 6 make portal … (что делает: .env→rollback, теги, 0008 при старте portal-api, Bifrost/Caddy)
## Проверка            — 7 ps/smoke/smoke-portal; 8 браузер: индикатор контекста, перенос в другой чат/режим, сжатие, закрепление/исключение, версии; 9 логи portal-api/worker
## [После окна]        — фоновые задачи, если есть; закрыть релиз: rm -rf "$REL" "$LIVE/deploy/rollback", копию — с ВМ
## Откат               — сначала флаг context.* (§3.8); у 0008 нет обратного хода → release-rollback с FROM; варианты без копии
## Что замерить на ВМ  — точность счёта токенов против vLLM, время/качество сжатия, задержка первого токена
```

Синхронно с ним обновляются:
- `docs/runbook.md:1103-1104` — ссылка «релиз 0.7 — `release-0.7.md`»;
- таблица флагов §3.8;
- `docs/user-guide.md`;
- `docs/portal-design.md` §8.4 (правит владелец).