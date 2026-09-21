"""Шов между базой и провайдерами: всё, что они знают друг о друге, описано здесь.

База принимает Command и отдаёт Reply с Limits. Как собрать команду и как разобрать
журналы — дело провайдера; как запустить процесс и можно ли тратить лимит — дело базы.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Profile:
    """Учётная запись CLI. Свой домашний каталог — свой вход и свои лимиты."""

    name: str
    home: Path

    def __post_init__(self) -> None:
        # Вход и зонды идут с cwd=home, сам ход — из папки хода: относительный путь
        # там означал бы разные каталоги, и свежий вход выглядел бы как его отсутствие.
        object.__setattr__(self, "home", Path(self.home).resolve())


@dataclass(frozen=True)
class Command:
    """Что запустить. Провайдер её собирает, база — выполняет."""

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: Path
    stdin: Path | None = None
    timeout: float | None = None


@dataclass(frozen=True)
class Reply:
    """Ответ одного хода, уже вынутый провайдером из журналов."""

    text: str
    session_id: str | None = None
    complete: bool = False          # дошли до терминального события, а не оборвались
    diagnostic: str | None = None
    usage: Mapping[str, object] = field(default_factory=dict)  # сырое, база не смотрит внутрь
    tokens: Usage | None = None     # то же самое, приведённое к общему виду
    model: str | None = None


@dataclass(frozen=True)
class Usage:
    """Токены одного хода, приведённые к общему виду.

    input — только неподкешированный остаток; reasoning уже входит в output и
    отдельно не оплачивается, он здесь ради видимости.
    """

    input: int = 0
    cached_input: int = 0
    cache_write: int = 0
    output: int = 0
    reasoning: int = 0

    @property
    def total(self) -> int:
        return self.input + self.cached_input + self.cache_write + self.output


@dataclass(frozen=True)
class Rates:
    """Цена за миллион токенов. None у кеша означает «как за обычный вход»."""

    input: Decimal
    output: Decimal
    cached_input: Decimal | None = None
    cache_write: Decimal | None = None
    currency: str = "USD"
    source: str = ""          # откуда цифры и на какую дату


@dataclass(frozen=True)
class Cost:
    amount: Decimal
    currency: str
    source: str               # cli — посчитал сам провайдер, иначе наша таблица
    parts: Mapping[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class Window:
    """Одно окно лимита. Имена окон произвольные: база не знает, какие они бывают."""

    name: str
    used_percent: float
    window: timedelta | None = None
    resets_at: datetime | None = None
    resets_hint: str | None = None  # когда провайдер даёт только текст, без точного времени

    def __post_init__(self) -> None:
        # Замер, которому нельзя верить, хуже отсутствующего: на нём строится отказ.
        if isinstance(self.used_percent, bool) or not 0 <= self.used_percent <= 100:
            raise ValueError(f"Недопустимая доля окна: {self.used_percent!r}")
        if self.window is not None and self.window <= timedelta(0):
            raise ValueError("Недопустимая длительность окна")


@dataclass(frozen=True)
class Limits:
    provider: str
    profile: str
    windows: tuple[Window, ...]
    measured_at: datetime
    source: str
    exact: bool                     # False — снимок мог устареть, мерили не по своему вызову
    plan: str | None = None
    credits: Decimal | None = None  # есть не у всех: отсутствие — None, а не ноль
    credits_unlimited: bool = False # провайдер сказал «без ограничений», числа нет

    @property
    def spendable(self) -> bool:
        """Есть ли за что работать, когда окно выбрано: остаток или безлимит."""
        return self.credits_unlimited or (self.credits or 0) > 0

    @property
    def worst(self) -> float | None:
        return max((window.used_percent for window in self.windows), default=None)

    def age(self, now: datetime | None = None) -> timedelta:
        return (now or datetime.now(UTC)) - self.measured_at


@runtime_checkable
class Adapter(Protocol):
    """Второй уровень: отправка запроса, чтение ответа и добыча лимитов."""

    name: str

    def environment(self, profile: Profile) -> Mapping[str, str]: ...

    def fingerprint(self) -> Mapping[str, str]:
        """Настройки, от которых зависит ответ: модель, усилие, режим песочницы."""

    def check(self, profile: Profile) -> Command: ...

    def verify(self, captured: str) -> None:
        """Бросает, если подписочного входа нет."""

    def login(self, profile: Profile) -> Command: ...

    def logout(self, profile: Profile) -> Command: ...

    def ask(self, entry, request: Mapping[str, object], profile: Profile) -> Command: ...

    def reply(self, entry, profile: Profile) -> Reply: ...

    def price(self, reply: Reply) -> Cost | None:
        """Во что ход обошёлся бы по API. None — если модель незнакома таблице."""

    def limits(self, profile: Profile, *, session: str | None = None,
               model: str | None = None) -> Limits | None:
        """Готовый замер или None, если у провайдера нет способа его получить.

        model — модель, которая пойдёт в ход: лимит считается по её корзине.
        Процессы адаптер запускает только примитивами базы: process.capture и Channel.
        """
