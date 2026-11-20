# Что изучено и что заимствовано

Исходники просмотрены до реализации; приватные репозитории читались GitHub
коннектором, без копирования их данных или архивов пользователей.

| Источник | Просмотрено | Решение Lite |
|---|---|---|
| ChatNovella `e8571b4` | store.py, mcp_server.py, правила и тесты | Версии, chunk receipts, timeline append, атомарное подтверждение. Новый код отдельно, старые карточки/scorecard не импортированы |
| roman_ai `refactor/simple-relationships-file`, `df1668131e33e262682ba99099c4af4d679b1257` | app/relationship_file_runtime.py, app/relationship_runtime.py | Единый файл; числовые NPC→POV, словесные направленные NPC→NPC, обычная дельта ±3 |
| 1206_v2 `d5c4d61b15da403049ae97785522401bb2662920` | app/knowledge_state_runtime_patch.py | Стартовые знания в каноне, приобретённые отдельно по владельцу. Патчи/дублирующие индексы не перенесены |
| new_1206_v3 `fb8a023db464f63228cfa36e1ea27f5b58c4e7ce` | api_contracts/context_builder_rules_1206.md | Выбор личной памяти по участвующим ID, без сводок вместо полных материалов |
| Iteractiv-ROMAN `main`, tree `dd3122faf2ba0303c18c3af6f2ef803e22ba6799` | app/engine.py, начало файла, схема транзакций/истории | Простой prepare→commit, идемпотентность, before-image для отката; SQLite-схема целиком не перенесена |
| Iteractiv-ROMAN `fix/context-and-audit-15-60`, tree `5ffc31701f9251a4298c1155f9ecd2b5cc9c11b0` | app/audit.py | Явная область 15/60, контроль всех владельцев, первичные источники не сжимаются |
| ai_novella `main`, tree `0f1c45b3a19335b8d156fa11c6461f9c120ec2b0` | tests/test_revision_and_knowledge.py, tests/test_memory_receipt.py | Тесты изоляции владельцев, исправлений/опровержений, полного чтения контекста |

Заимствованы принципы, а не многослойная архитектура RomanAI. Полный перенос
механик каждого исходника не заявляется; выбран минимальный активный контракт.
