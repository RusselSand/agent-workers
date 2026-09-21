"""Замер лимита до и после хода и решение, можно ли вообще его начинать."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .contract import Adapter, Limits, Profile

log = logging.getLogger(__name__)


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


@dataclass
class Guard:
    adapter: Adapter
    profile: Profile
    policy: LimitPolicy = field(default_factory=LimitPolicy)
    sink: object = None       # вызываемое: получает каждый замер, если проекту это нужно
    sleep: object = time.sleep

    def measure(self, phase: str, *, session: str | None = None) -> Limits | None:
        if not (self.policy.before if phase == "before" else self.policy.after):
            return None
        reads = max(1, self.policy.settle_reads) if phase == "after" else 1
        latest = None
        for _ in range(reads):
            if phase == "after" and self.policy.settle_delay:
                self.sleep(self.policy.settle_delay)
            try:
                latest = self.adapter.limits(self.profile, session=session) or latest
            except Exception as exc:
                # Замер — не цель работы: его отказ не должен ронять сам ход.
                log.warning("Замер лимита (%s) не удался: %s", phase, type(exc).__name__)
                break
        if latest is not None and callable(self.sink):
            self.sink({"phase": phase, "limits": latest})
        return latest

    def blocked(self, limits: Limits | None) -> str | None:
        """Причина отказа или None. Неизвестный лимит — тоже решение политики."""
        if limits is None or (not limits.exact and limits.age() > self.policy.stale_after):
            return None if self.policy.on_unknown == "proceed" else "Лимит неизвестен"
        if self.policy.refuse_above is None or (limits.worst or 0) < self.policy.refuse_above:
            return None
        if self.policy.spend_credits and (limits.credits or 0) > 0:
            return None
        return f"Окно выбрано на {limits.worst:.0f}%"

    @staticmethod
    def spent(before: Limits | None, after: Limits | None) -> dict[str, float]:
        """Цена хода в процентах окна — то, ради чего замер делается дважды."""
        if not before or not after:
            return {}
        was = {window.name: window.used_percent for window in before.windows}
        return {window.name: round(window.used_percent - was[window.name], 3)
                for window in after.windows if window.name in was}


def now() -> datetime:
    return datetime.now(UTC)
