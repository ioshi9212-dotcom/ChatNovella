# ChatNovella Lite — isolated test branch

Рабочая тестовая реализация по [ТЗ](docs/lite/SPEC.md). Это отдельный движок;
старый код в `chatnovella/`, его тесты и инструкции оставлены для истории и
сравнения. Lite их не импортирует. Не сливать эту ветку в `main` и не подключать
её к действующему приложению Amvera.

- [Запуск и подключение](docs/lite/DEPLOY.md)
- [Протокол GPT](docs/lite/INSTRUCTIONS.md)
- [Архитектура и границы гарантий](docs/lite/ARCHITECTURE.md)
- [Изученные реализации](docs/lite/RESEARCH.md)
- [Результаты и незавершённая приёмка](docs/lite/REPORT.md)

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests_lite -v
CHATNOVELLA_LITE_DATA_DIR=./.lite-data uvicorn chatnovella_lite.server:app --host 127.0.0.1 --port 8081 --workers 1
```

MCP: `/mcp`. Actions/OpenAPI: `/openapi.json`. Проверка запуска: `/healthz`.
Не требуется внешний API генерации, отдельный review, scorecard или plan.
