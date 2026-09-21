"""Настройки одного воркера: одно подключение, названное прямо.

Воркер знает единственную учётную запись — каталог, который ему назвали. Списка
профилей здесь нет: несколько учёток живут как несколько каталогов, и выбирается
одна, при запуске. Порядок старшинства: флаг команды, переменная окружения, .env,
умолчание.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from .base.guard import LimitPolicy

FILENAME = ".env"
DEPTH = 4
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
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
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
        return cls(parse(path.read_text(encoding="utf-8")) if path else {}, path)

    def override(self, **named: str | None) -> Settings:
        """Флаги команды сильнее всего остального; пустые значения ничего не меняют."""
        given = {f"AGENT_{key.upper()}": value for key, value in named.items() if value}
        return replace(self, overrides={**self.overrides, **given})

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

    @property
    def home(self) -> Path:
        """Каталог учётной записи. Вне репозитория: в нём лежат токены входа."""
        named = self.get("AGENT_HOME")
        return Path(named) if named else Path.home() / ".agent-worker" / self.provider

    @property
    def model(self) -> str:
        return self.get("AGENT_MODEL")

    @property
    def runs(self) -> Path:
        named = self.get("AGENT_RUNS")
        return Path(named) if named else self.home / "runs"

    @property
    def max_pending(self) -> int:
        """Сколько готовых и не забранных результатов терпеть, прежде чем встать."""
        value = self.get("AGENT_MAX_PENDING", "50").strip()
        return int(value) if value else 50

    @property
    def policy(self) -> LimitPolicy:
        refuse = self.get("AGENT_REFUSE_ABOVE", "95").strip()
        return LimitPolicy(refuse_above=float(refuse) if refuse else None,
                           spend_credits=self.get("AGENT_SPEND_CREDITS", "1").lower() not in NO)
