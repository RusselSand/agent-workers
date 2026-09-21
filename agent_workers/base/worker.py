"""Один ход целиком: замер до, запрос, ответ, замер после.

Здесь нет ни одного знания о конкретной CLI: всё, что от неё зависит, приходит
адаптером. Менять этот файл придётся, только если поменяется сама схема работы —
например, ход перестанет быть «один процесс, один ответ».
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .contract import Adapter, Profile
from .entry import Entry, digest
from .guard import Guard, LimitPolicy
from .process import capture, interactive, supervise

log = logging.getLogger(__name__)


@dataclass
class Worker:
    adapter: Adapter
    profile: Profile
    root: Path
    policy: LimitPolicy = field(default_factory=LimitPolicy)
    sink: object = None
    timeout: float = 1200

    def __post_init__(self) -> None:
        self.guard = Guard(self.adapter, self.profile, self.policy, self.sink)

    def check(self) -> None:
        """Проверка подписочного входа. Бросает, если входа нет."""
        self.adapter.verify(capture(self.adapter.check(self.profile)))

    def login(self) -> int:
        self.profile.home.mkdir(parents=True, exist_ok=True)
        return interactive(self.adapter.login(self.profile))

    def logout(self) -> int:
        return interactive(self.adapter.logout(self.profile))

    def run(self, request: Mapping[str, object], *, key: str | None = None,
            pulse=None, stop=None) -> dict:
        entry = Entry(self.root, key or digest({"adapter": self.adapter.name, "request": request}))
        before = self.guard.measure("before")
        reason = self.guard.blocked(before)
        if reason:
            # Ничего не потрачено: отказ случился до запуска процесса.
            entry.update(state="limit_reached", error=reason)
            return {"state": "limit_reached", "reason": reason, "entry": entry,
                    "before": before, "after": None, "spent": {}, "reply": None,
                    "tokens": None, "cost": None}

        command = self.adapter.ask(entry, request, self.profile)
        if not entry.reserve_start():
            # Повторный заход в ту же папку: результат уже есть либо ход идёт в другом процессе.
            reply = self.adapter.reply(entry, self.profile)
            return {"state": "resumed" if reply.complete else "in_progress", "entry": entry,
                    "reply": reply, "before": before, "after": None, "spent": {},
                    "tokens": reply.tokens, "cost": self.price(reply)}
        outcome = supervise(command, stdout=entry.stdout, stderr=entry.stderr,
                            pulse=pulse, stop=stop, timeout=self.timeout)
        entry.update(state="captured", returncode=outcome.returncode,
                     interruption=outcome.interruption)
        reply = self.adapter.reply(entry, self.profile)
        entry.update(state="answered" if reply.complete else "incomplete",
                     session_id=reply.session_id, diagnostic=reply.diagnostic)
        after = self.guard.measure("after", session=reply.session_id)
        return {"state": "answered" if reply.complete else "incomplete", "entry": entry,
                "reply": reply, "before": before, "after": after,
                "spent": self.guard.spent(before, after),
                "tokens": reply.tokens, "cost": self.price(reply)}

    def price(self, reply):
        """Оценка стоимости — справка рядом с ответом: её отказ не портит сам ход."""
        try:
            return self.adapter.price(reply)
        except Exception as exc:
            log.warning("Оценка стоимости не удалась: %s", type(exc).__name__)
            return None
