Нужно доработать много разных аспектов приложения и составить план, как раскатить изменения на уже работающий сервис. Вот те моменты, которые нужно доработать:

1) Добавить опцию поиска информации в интернете в режиме чата.
2) В меню 'SQL' перенести строку выбора диалекта + Схема базы + Мои схемы в строку заголовка SQL  в правый верхний угол, а выбор режима (написать запрос, объяснить запрос и тп) - переменстить вниз под поле ввода
3) Добавить в режиме SQL возможность проверки запросов на синтетических данных, которые будут генерироваться по схеме (должна быть возможность выключить этот режим)
4) В базе знаний добавить отдельных блок - министерства
5) В базу знаний добавить в загрузке документов возможность добавлять теги, а при использовании настраивать - какие теги использовать при поиске
6) Добавить возможность распознавания картинок (текст + описание, что на картинке) при добавлении документов в базу знаний (в том числе картинко из word-файлов), чат должен уметь выдавать эти картинки, если они нужны при ответе на вопрос
7) Улучшить качество RAG-поиска (возможно сделать agentic-RAG)
8) Добавить возможность распознавания голоса (возможно на CPU) и видео записей (отттуда достается только аудио) + возможность также загружать это в базу знаний
9) В режиме чата не работает режим вдумчивого ответа, вот логи ошибки с сервера:


root@UbuntuVM:~/LLM/llmDeploy# set -a; . deploy/.env; set +a
root@UbuntuVM:~/LLM/llmDeploy# curl -sS -i http://127.0.0.1:8000/v1/chat/completions 
  -H "Authorization: Bearer $VLLM_API_KEY" -H 'Content-Type: application/json' 
  -d '{"model":"default","messages":[{"role":"user","content":"2+2?"}],"reasoning_effort":"xhigh","max_tokens":2000}'
HTTP/1.1 200 OK
date: Sun, 11 Oct 2026 14:59:43 GMT
server: uvicorn
content-length: 813
content-type: application/json

{"id":"chatcmpl-8aebba4c540ed841","object":"chat.completion","created":1791730783,"model":"default","choices":[{"index":0,"message":{"role":"assistant","content":"\n\n4","refusal":null,"annotations":null,"audio":null,"function_call":null,"reasoning":"We need answer user's simple \"2+2? Need produce final. Likely answer 4. Maybe note arithmetic. final concise.\n"},"logprobs":null,"finish_reason":"stop","stop_reason":null,"token_ids":null,"routed_experts":null}],"service_tier":null,"system_fingerprint":"vllm-0.29.0-fdba2ad9","usage":{"prompt_tokens":56,"total_tokens":88,"completion_tokens":32,"prompt_tokens_details":null,"completion_tokens_details":{"reasoning_tokens":28}},"prompt_logprobs":null,"prompt_token_ids":null,"prompt_text":null,"kv_transfer_params":null,"ec_transfer_params":null,"metrics":null}root@UbuntuVM:~/LLM/llmDeploy# C="sudo docker compose -f deploy/docker-compose.yml"                 C="sudo docker compose -f deploy/docker-compose.yml"
$C logs --since 5m portal-api | grep -iE "error|trace|unavailable|overloaded"
$C logs --since 5m bifrost    | grep -iE "error|400|422|5[0-9][0-9]|reasoning"
$C logs --since 5m vllm       | grep -iE "error|400|422|reasoning|Traceback|max_tokens|context"
bifrost-1  | {"level":"warn","http.method":"POST","http.target":"/v1/chat/completions","http.status_code":400,"http.request_duration_ms":9,"http.remote_addr":"172.19.0.5:53234","http.user_agent":"python-httpx/0.28.1","trace_id":"efa2fe9ca6004bb087adba5130bd8ff5","request_id":"9853441e-8afd-4484-9e0b-d0e638de5509","time":"2026-10-11T15:00:19Z","message":"request completed"}
bifrost-1  | {"level":"warn","http.method":"POST","http.target":"/v1/chat/completions","http.status_code":499,"http.request_duration_ms":501,"http.remote_addr":"172.19.0.5:53224","http.user_agent":"python-httpx/0.28.1","trace_id":"0979641f030e46c1a798bad8204825b7","request_id":"a38dfd39-59d1-4376-8ae3-c21810e967f2","time":"2026-10-11T15:00:20Z","message":"request completed"}
bifrost-1  | {"level":"warn","http.method":"POST","http.target":"/v1/chat/completions","http.status_code":400,"http.request_duration_ms":8,"http.remote_addr":"172.19.0.5:53244","http.user_agent":"python-httpx/0.28.1","trace_id":"4a0d748c072d4c62bc7bb8f056f9074d","request_id":"ebdc119a-e6c9-44d8-9c81-55495116fa07","time":"2026-10-11T15:00:21Z","message":"request completed"}
bifrost-1  | {"level":"warn","http.method":"POST","http.target":"/v1/chat/completions","http.status_code":499,"http.request_duration_ms":501,"http.remote_addr":"172.19.0.5:53234","http.user_agent":"python-httpx/0.28.1","trace_id":"bda57abdeec94915ab478f5ea75ef9ef","request_id":"bd6efbd1-8472-4582-af33-90080a78e51a","time":"2026-10-11T15:00:21Z","message":"request completed"}
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793] An error occurred in `transformers` while applying chat template
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793] Traceback (most recent call last):
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]   File "/usr/local/lib/python3.12/dist-packages/vllm/renderers/hf.py", line 785, in safe_apply_chat_template
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]     plain = tokenizer.apply_chat_template(
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]   File "/usr/local/lib/python3.12/dist-packages/vllm/tokenizers/hf.py", line 62, in apply_chat_template
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]     return tok.apply_chat_template(*args, **kwargs)
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]   File "/usr/local/lib/python3.12/dist-packages/transformers/tokenization_utils_base.py", line 3108, in apply_chat_template
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]     rendered_chat, generation_indices = render_jinja_template(
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]                                         ^^^^^^^^^^^^^^^^^^^^^^
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]   File "/usr/local/lib/python3.12/dist-packages/transformers/utils/chat_template_utils.py", line 581, in render_jinja_template
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]     rendered_chat = compiled_template.render(
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]                     ^^^^^^^^^^^^^^^^^^^^^^^^^
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]   File "/usr/local/lib/python3.12/dist-packages/jinja2/environment.py", line 1295, inrender
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]     self.environment.handle_exception()
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]   File "/usr/local/lib/python3.12/dist-packages/jinja2/environment.py", line 942, in handle_exception
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]     raise rewrite_traceback_stack(source=source)
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]   File "<template></template>", line 49, in top-level template code
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]   File "/usr/local/lib/python3.12/dist-packages/jinja2/sandbox.py", line 401, in call
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]     return __context.call(__obj, *args, **kwargs)
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]   File "/usr/local/lib/python3.12/dist-packages/transformers/utils/chat_template_utils.py", line 479, in raise_exception
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793]     raise jinja2.exceptions.TemplateError(message)
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:19 [hf.py:793] jinja2.exceptions.TemplateError: Unexpected reasoning effort high. Supported types are xhigh (default), medium, and low.
vllm-1  | (APIServer pid=1) INFO:     172.18.0.3:45798 - "POST /v1/chat/completions HTTP/1.1" 400 Bad Request
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793] An error occurred in `transformers` while applying chat template
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793] Traceback (most recent call last):
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]   File "/usr/local/lib/python3.12/dist-packages/vllm/renderers/hf.py", line 785, in safe_apply_chat_template
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]     plain = tokenizer.apply_chat_template(
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]   File "/usr/local/lib/python3.12/dist-packages/vllm/tokenizers/hf.py", line 62, in apply_chat_template
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]     return tok.apply_chat_template(*args, **kwargs)
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]   File "/usr/local/lib/python3.12/dist-packages/transformers/tokenization_utils_base.py", line 3108, in apply_chat_template
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]     rendered_chat, generation_indices = render_jinja_template(
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]                                         ^^^^^^^^^^^^^^^^^^^^^^
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]   File "/usr/local/lib/python3.12/dist-packages/transformers/utils/chat_template_utils.py", line 581, in render_jinja_template
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]     rendered_chat = compiled_template.render(
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]                     ^^^^^^^^^^^^^^^^^^^^^^^^^
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]   File "/usr/local/lib/python3.12/dist-packages/jinja2/environment.py", line 1295, inrender
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]     self.environment.handle_exception()
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]   File "/usr/local/lib/python3.12/dist-packages/jinja2/environment.py", line 942, in handle_exception
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]     raise rewrite_traceback_stack(source=source)
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]   File "<template></template>", line 49, in top-level template code
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]   File "/usr/local/lib/python3.12/dist-packages/jinja2/sandbox.py", line 401, in call
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]     return __context.call(__obj, *args, **kwargs)
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]            ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]   File "/usr/local/lib/python3.12/dist-packages/transformers/utils/chat_template_utils.py", line 479, in raise_exception
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793]     raise jinja2.exceptions.TemplateError(message)
vllm-1  | (APIServer pid=1) ERROR 10-11 15:00:21 [hf.py:793] jinja2.exceptions.TemplateError: Unexpected reasoning effort high. Supported types are xhigh (default), medium, and low.
vllm-1  | (APIServer pid=1) INFO:     172.18.0.3:45798 - "POST /v1/chat/completions HTTP/1.1" 400 Bad Request
root@UbuntuVM:~/LLM/llmDeploy#

Продумай максимально технически грамотные и современные решения по каждому пункту, по всем техническим решениям - советуйся со мной, чтобы я обо всем был в курсе. Все изменения надо протестировать - только самые важные тесты, которые проверяют корректность, безопасность и надежность работы, без других лишних тестов. Если тебе надо получить прямой доступ к моделям - скажи мне, что нужно сделать, чтобы тебе его дать.
