# agent-workers

Один ход CLI с подпиской (Claude Code, Codex) как библиотека: вход в учётную запись,
замер расхода подписки до и после хода, оценка того, во что ход обошёлся бы по API,
лоток с оплаченными ответами. Ни очереди, ни координатора: пакет принимает запрос и
ключ задачи и возвращает результат. Устройство, настройка и обязанности вызывающего
описаны в [agent_workers/README.ru.md](agent_workers/README.ru.md).

Пакетом пользуются kromka-agent-worker (обработчик `AGENT_TURN`) и council. До этого
репо у каждого была своя копия, и они разошлись: в council не было `LoginRequired`.
История пакета перенесена вместе с кодом.

## Как подключить

```toml
[project]
dependencies = ["agent-workers"]

[tool.uv.sources]
agent-workers = { git = "https://github.com/RusselSand/agent-workers", tag = "v0.4.0" }
```

Импорты прежние: `from agent_workers import build`. Команды
`python -m agent_workers status | login | logout | run | pending | collect` работают в
любом проекте, куда пакет установлен. Образец настроек подключения —
[agent_workers/.env.ru.example](agent_workers/.env.ru.example): скопируйте его в свой проект
как `.env`.

Для тестов кода, построенного на пакете, есть `agent_workers.testing`: `FakeAdapter`
вместо CLI, `QUIET`, `limits()`.

Репозиторий публичный: `uv sync` скачивает пакет по HTTPS без GitHub-токена и приглашения.

## Версии

Правка — PR сюда, после мерджа тег `vX.Y.Z`. Проект получает её, когда поднимает тег в
своём `pyproject.toml` и делает `uv lock`. Несовместимое изменение API — новая старшая
цифра.

Разбор вывода завязан на формат конкретных версий CLI. Новая версия Claude Code или
Codex, которая меняет вывод, — это правка здесь и новый тег, а в образе воркера — новые
закреплённые версии CLI.

## Разработка

```bash
uv sync
uv run pytest
uv run ruff check .
```

Python 3.14 (`.python-version`), `uv` скачает его сам. Боевой запуск — на Linux: на
Windows тесты групп процессов и прав на файлы пропускаются. Полный прогон на Linux
делается из копии с обычными правами, потому что с Windows-хоста файлы в томе видны
как 0777:

```bash
docker run --rm -v "$PWD:/src:ro" ghcr.io/astral-sh/uv:python3.14-bookworm-slim sh -c \
  'cp -r /src /tmp/app && cd /tmp/app && rm -rf .venv && find . -type f -exec chmod 644 {} + \
   && uv sync --frozen -q && uv run pytest -q -p no:cacheprovider'
```
