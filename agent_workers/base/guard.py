"""Замер лимита до и после хода и решение, можно ли вообще его начинать."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .contract import Adapter, Limits, Profile, Window

log = logging.getLogger(__name__)

# Некоторые CLI считают время сброса от момента ответа: микросекунды двух замеров
# разные. Настоящий сброс сдвигает его на целое окно — часы и дни, а не доли секунды.
JITTER = timedelta(minutes=1)
# Дольше ждать учёта расхода незачем: ответ уже готов, а замер — справка.
MAX_SETTLE = 60.0


@dataclass
class LimitPolicy:
    before: bool = True
    after: bool = True
    refuse_above: float | None = 95.0
    stale_after: timedelta = timedelta(minutes=15)
    on_unknown: str = "proceed"  # proceed | refuse
    # Выбранное окно — не всегда стоп: если провайдер сообщает остаток, работа идёт за него.
    spend_credits: bool = True
    # Учёт расхода догоняет вызов не мгновенно: сразу после хода счётчик ещё прежний.
    settle_reads: int = 2
    settle_delay: float = 2.0

    def __post_init__(self) -> None:
        # Порог вне 0–100 молча меняет расход подписки: nan и отрицательные запрещают всё,
        # inf и больше 100 выключают отказ. Выключают его явно — через None.
        limit = self.refuse_above
        if limit is not None and (isinstance(limit, bool) or not 0 <= limit <= 100):
            raise ValueError(f"Порог отказа — процент от 0 до 100 или None: {limit!r}")
        # Пауза нужна уже после оплаченного хода: сорвись time.sleep там — и вызывающий
        # получил бы исключение вместо готового ответа. Проверяем заранее.
        if not 0 <= self.settle_delay <= MAX_SETTLE:
            raise ValueError(f"Пауза перед замером «после» — от 0 до {MAX_SETTLE} с: "
                             f"{self.settle_delay!r}")


@dataclass
class Guard:
    adapter: Adapter
    profile: Profile
    policy: LimitPolicy = field(default_factory=LimitPolicy)
    sleep: Callable[[float], None] = time.sleep

    def measure(self, phase: str, *, session: str | None = None,
                model: str | None = None) -> Limits | None:
        """model — модель, которая пойдёт в ход: лимит считается по её корзине."""
        if not (self.policy.before if phase == "before" else self.policy.after):
            return None
        reads = max(1, self.policy.settle_reads) if phase == "after" else 1
        latest = None
        for _ in range(reads):
            if phase == "after" and self.policy.settle_delay:
                self.sleep(self.policy.settle_delay)
            try:
                latest = self.adapter.limits(self.profile, session=session, model=model,
                                             fresh_within=self.policy.stale_after) or latest
            except Exception as exc:
                # Замер — не цель работы: его отказ не должен ронять сам ход.
                log.warning("Замер лимита (%s) не удался: %s", phase, type(exc).__name__)
                break
        return latest

    def blocked(self, limits: Limits | None) -> str | None:
        """Причина отказа или None. Неизвестный лимит — тоже решение политики."""
        if limits is None or (not limits.exact and limits.age() > self.policy.stale_after):
            return None if self.policy.on_unknown == "proceed" else "Лимит неизвестен"
        if self.policy.refuse_above is None or (limits.worst or 0) < self.policy.refuse_above:
            return None
        if self.policy.spend_credits and limits.spendable:
            return None
        return f"Окно выбрано на {limits.worst:.0f}%"

    @staticmethod
    def spent(before: Limits | None, after: Limits | None) -> dict[str, float]:
        """Цена хода в процентах окна — то, ради чего замер делается дважды.

        Окно, которое за время хода успело обнулиться, пропускаем: разница там
        отрицательная и означает не расход, а начало нового отсчёта.
        """
        if not before or not after:
            return {}
        was = {window.name: window for window in before.windows}
        spent = {}
        for window in after.windows:
            earlier = was.get(window.name)
            if earlier is None or rolled_over(earlier, window):
                continue
            delta = round(window.used_percent - earlier.used_percent, 3)
            if delta >= 0:
                spent[window.name] = delta
        return spent


def rolled_over(earlier: Window, later: Window) -> bool:
    """Сброс узнаём по заметному сдвигу времени сброса вперёд, а не по неравенству."""
    if earlier.resets_at is None or later.resets_at is None:
        return False
    return later.resets_at - earlier.resets_at > JITTER


def now() -> datetime:
    return datetime.now(UTC)
