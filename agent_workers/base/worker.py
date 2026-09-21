"""Один ход целиком: замер до, запрос, ответ, замер после.

Здесь нет ни одного знания о конкретной CLI: всё, что от неё зависит, приходит
адаптером. Менять этот файл придётся, только если поменяется сама схема работы —
например, ход перестанет быть «один процесс, один ответ».

Папка хода переживает перезапуски, поэтому порядок такой: сначала разбираемся, что
в ней уже лежит, и только потом тратим лимит. Готовый ответ отдаётся даром, чужой
живой процесс не трогается, а оборванная попытка не выдаёт себя за идущую.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .contract import Adapter, Profile
from .entry import Entry, digest
from .guard import Guard, LimitPolicy
from .process import alive, capture, interactive, supervise

log = logging.getLogger(__name__)

EPOCH = "AGENT_PROCESS_EPOCH"   # у контейнера после перезапуска PID начинаются заново
DELIVERED = ("answered", "incomplete")   # ход кончился, результат ждёт получателя


@dataclass
class Worker:
    adapter: Adapter
    profile: Profile
    root: Path
    policy: LimitPolicy = field(default_factory=LimitPolicy)
    timeout: float = 1200
    max_pending: int = 50     # столько готовых и не забранных результатов терпим

    def __post_init__(self) -> None:
        self.guard = Guard(self.adapter, self.profile, self.policy)

    def check(self) -> None:
        """Проверка подписочного входа. Бросает, если входа нет."""
        self.adapter.verify(capture(self.adapter.check(self.profile)))

    def login(self) -> int:
        self.profile.home.mkdir(parents=True, exist_ok=True)
        return interactive(self.adapter.login(self.profile))

    def logout(self) -> int:
        return interactive(self.adapter.logout(self.profile))

    def run(self, request: Mapping[str, object], *, key: str | None = None,
            pulse=None, stop=None, retry: bool = False) -> dict:
        entry = Entry(self.root, key or digest({"adapter": self.adapter.name, "request": request}))
        if retry:
            entry.restart()

        done = self.finished(entry)
        if done is not None:
            return done

        if not entry.reserve_start():
            # Маркер уже стоит. Либо ход идёт прямо сейчас, либо это след оборванного.
            return self.taken(entry)

        waiting = len(self.pending())
        if waiting >= self.max_pending:
            # Лучше встать, чем молча забивать диск текстами, которые никто не забирает.
            entry.release_start()
            entry.drop()
            return self.result("outbox_full", entry, None, None, None,
                               reason=f"не забрано результатов: {waiting}")

        before = self.guard.measure("before")
        reason = self.guard.blocked(before)
        if reason:
            # Ничего не потрачено и ничего не записано: отказ до сборки запроса.
            entry.release_start()
            entry.drop()   # в папке ничего нет: ход не начинался
            return self.result("limit_reached", entry, None, before, None, reason=reason)

        command = self.adapter.ask(entry, request, self.profile)
        entry.update(state="running", pid=os.getpid(), epoch=os.environ.get(EPOCH))
        outcome = supervise(command, stdout=entry.stdout, stderr=entry.stderr,
                            pulse=pulse, stop=stop, timeout=self.timeout)
        entry.update(state="captured", pid=None, returncode=outcome.returncode,
                     interruption=outcome.interruption)
        reply = self.adapter.reply(entry, self.profile)
        state = "answered" if reply.complete else "incomplete"
        entry.update(state=state, session_id=reply.session_id, diagnostic=reply.diagnostic)
        after = self.guard.measure("after", session=reply.session_id)
        return self.result(state, entry, reply, before, after)

    def pending(self) -> list[Entry]:
        """Готовые результаты, которых ещё не забрали. Папка живёт, пока её не заберут."""
        if not self.root.is_dir():
            return []
        entries = (Entry(self.root, path.name) for path in sorted(self.root.iterdir())
                   if path.is_dir() and (path / "state.json").is_file())
        return [entry for entry in entries if entry.meta.get("state") in DELIVERED]

    def collect(self, entry: Entry | str) -> None:
        """Забрали — папку сносим. Сколько хранить, решает тот, кто забирает."""
        entry = entry if isinstance(entry, Entry) else Entry(self.root, entry)
        entry.drop()

    def finished(self, entry: Entry) -> dict | None:
        """Готовый ответ стоит ноль: отдаём его, не трогая ни лимит, ни состояние."""
        if entry.meta.get("state") != "answered":
            return None
        reply = self.adapter.reply(entry, self.profile)
        return self.result("resumed", entry, reply, None, None) if reply.complete else None

    def taken(self, entry: Entry) -> dict:
        """Кто-то уже занял эту папку: либо работает, либо когда-то не доработал."""
        meta = entry.meta
        if meta.get("state") == "running" and alive(meta.get("pid"), epoch=meta.get("epoch"),
                                                    current_epoch=os.environ.get(EPOCH)):
            return self.result("in_progress", entry, None, None, None)
        # Процесса нет: это итог прошлой попытки. Повтор — только по явной просьбе,
        # иначе автоматика будет бесконечно переделывать то, что уже стоило денег.
        reply = self.adapter.reply(entry, self.profile)
        state = "resumed" if reply.complete else meta.get("state") or "incomplete"
        if state == "running":
            state = "incomplete"    # процесс умер, не дописав состояние
        return self.result(state, entry, reply, None, None,
                           reason=None if reply.complete else "прошлая попытка не завершилась; "
                                  "повтор — run(..., retry=True)")

    def result(self, state, entry, reply, before, after, *, reason=None) -> dict:
        return {"state": state, "entry": entry, "reply": reply, "reason": reason,
                "before": before, "after": after,
                "spent": self.guard.spent(before, after),
                "tokens": reply.tokens if reply else None,
                "cost": self.price(reply) if reply else None}

    def price(self, reply):
        """Оценка стоимости — справка рядом с ответом: её отказ не портит сам ход."""
        try:
            return self.adapter.price(reply)
        except Exception as exc:
            log.warning("Оценка стоимости не удалась: %s", type(exc).__name__)
            return None
