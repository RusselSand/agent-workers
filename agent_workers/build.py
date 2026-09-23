"""Сборка воркера по настройкам: одно подключение, один каталог, один аккаунт."""

from __future__ import annotations

from .base.contract import Profile
from .base.worker import Worker
from .config import Settings
from .providers import ClaudeAdapter, CodexAdapter

ADAPTERS = {"claude": ClaudeAdapter, "codex": CodexAdapter}


def build(settings: Settings | None = None) -> Worker:
    settings = settings or Settings.load()
    provider = settings.provider
    if provider not in ADAPTERS:
        raise ValueError(f"Провайдер {provider!r} неизвестен; есть: " + ", ".join(ADAPTERS))
    options = {}
    if settings.model:
        options["model"] = settings.model
    if executable := settings.binary(provider):
        # Путь к CLI — из тех же настроек, что и всё остальное: .env, окружение, флаги.
        options["executable"] = executable
    adapter = ADAPTERS[provider](**options)
    # Имя профиля — имя каталога: так в отчётах видно ровно то, что названо в настройках.
    return Worker(adapter, Profile(settings.home.name, settings.home),
                  settings.runs, settings.policy)
