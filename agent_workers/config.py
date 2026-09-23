"""Настройки одного воркера: одно подключение, названное прямо.

Воркер знает единственную учётную запись — каталог, который ему назвали. Списка
профилей здесь нет: несколько учёток живут как несколько каталогов, и выбирается
одна, при запуске. Порядок старшинства: флаг команды, переменная окружения, .env,
умолчание.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .base.guard import LimitPolicy

FILENAME = ".env"
DEPTH = 4
YES = ("1", "true", "yes", "да")
NO = ("0", "false", "no", "нет")


def find(start: Path | None = None) -> Path | None:
    current = (start or Path.cwd()).resolve()
    for folder in (current, *current.parents[:DEPTH]):
        candidate = folder / FILENAME
        if candidate.is_file():
            return candidate
    return None


def parse(text: str) -> dict[str, str]:
    """KEY=value, кавычки, комментарии и export. Больше .env ничего и не нужно."""
    values: dict[str, str] = {}
    for line in text.lstrip("﻿").splitlines():  # BOM оставляет блокнот Windows
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ("\"", "'"):
            # Кавычки закрываются раньше комментария: AGENT_MODEL="opus" # закреплено
            quote = value[0]
            closing = value.find(quote, 1)
            value = value[1:closing] if closing > 0 else value[1:]
        else:
            value = value.split(" #")[0].strip()
        if key:
            values[key] = value
    return values


@dataclass(frozen=True)
class Settings:
    values: Mapping[str, str] = field(default_factory=dict)      # из .env
    path: Path | None = None
    overrides: Mapping[str, str] = field(default_factory=dict)   # из командной строки

    @classmethod
    def load(cls, start: Path | None = None) -> Settings:
        path = find(start)
        if path is None:
            return cls({}, None)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # Файл нашёлся, но испорчен или закрыт: это ошибка настройки, а не сбой.
            raise ValueError(f"Файл настроек не прочитать: {path}: {exc}") from None
        return cls(parse(text), path)

    def override(self, **named: str | None) -> Settings:
        """Флаги команды сильнее всего остального; пустые значения ничего не меняют."""
        given = {f"AGENT_{key.upper()}": value for key, value in named.items() if value}
        return Settings(self.values, self.path, {**self.overrides, **given})

    def get(self, key: str, default: str = "") -> str:
        """Пустое значение задано намеренно: `AGENT_REFUSE_ABOVE=` снимает порог."""
        for source in (self.overrides, os.environ, self.values):
            if key in source:
                return source[key]
        return default

    @property
    def provider(self) -> str:
        provider = self.get("AGENT_PROVIDER")
        if not provider:
            raise ValueError("Не сказано, куда подключаться: задайте AGENT_PROVIDER "
                             "в .env или флаг --provider")
        return provider

    def path_of(self, key: str) -> Path | None:
        """Путь из настроек, абсолютный, или None, если не задан.

        Относительный путь из .env считается от каталога этого файла: .env ищется вверх
        по дереву, и из какого подкаталога ни запусти, это должен быть один и тот же
        каталог — иначе другой вход, другой лоток и оплаченные ответы, которых не видно.
        Из окружения и флагов — от текущего каталога, как принято у путей в командах.
        """
        for source in (self.overrides, os.environ, self.values):
            if key not in source:
                continue
            if not source[key]:
                return None
            named = Path(source[key])
            if source is self.values and self.path is not None:
                named = self.path.parent / named   # абсолютный путь это не изменит
            return named.resolve()
        return None

    @property
    def home(self) -> Path:
        """Каталог учётной записи. Вне репозитория: в нём лежат токены входа."""
        return (self.path_of("AGENT_HOME")
                or (Path.home() / ".agent-worker" / self.provider).resolve())

    @property
    def model(self) -> str:
        return self.get("AGENT_MODEL")

    def binary(self, provider: str) -> str | None:
        """Путь к CLI провайдера, если назван: AGENT_CLAUDE_BINARY, AGENT_CODEX_BINARY.
        Относительный — по тем же правилам, что и прочие пути из настроек."""
        found = self.path_of(f"AGENT_{provider.upper()}_BINARY")
        return str(found) if found else None

    @property
    def runs(self) -> Path:
        return self.path_of("AGENT_RUNS") or (self.home / "runs").resolve()

    def flag(self, key: str, default: bool) -> bool:
        """Да или нет. Опечатка — ошибка настройки, а не «да»: иначе `flase` в
        AGENT_SPEND_CREDITS молча разрешил бы тратить кредиты."""
        value = self.get(key).strip().lower()
        if not value:
            return default
        if value in YES:
            return True
        if value in NO:
            return False
        raise ValueError(f"{key} — да ({', '.join(YES)}) или нет ({', '.join(NO)}); "
                         f"сейчас: {value!r}")

    @property
    def policy(self) -> LimitPolicy:
        spend = self.flag("AGENT_SPEND_CREDITS", default=True)
        refuse = self.get("AGENT_REFUSE_ABOVE", "95").strip()
        try:
            return LimitPolicy(refuse_above=float(refuse) if refuse else None,
                               spend_credits=spend)
        except ValueError:
            raise ValueError(f"AGENT_REFUSE_ABOVE — процент от 0 до 100, а пустое значение "
                             f"снимает порог; сейчас: {refuse!r}") from None
