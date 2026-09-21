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
from .entry import Entry, digest, registry
from .guard import Guard, LimitPolicy
from .outbox import Outbox
from .process import capture, interactive, supervise

log = logging.getLogger(__name__)

UNFINISHED = ("prepared", "running", "captured")


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
        self.outbox = Outbox(self.root)

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
        entry = Entry(self.root, key or self.key_for(request))
        if not entry.claim():
            # Папкой владеет живой процесс. Ни повторять, ни двигать его журналы нельзя:
            # иначе второй вызов оплатит ту же работу, а состояние напишут оба сразу.
            return self.result("in_progress", entry, None, None, None,
                               reason="ход уже выполняется другим процессом")
        try:
            return self.attempt(entry, request, pulse=pulse, stop=stop, retry=retry)
        finally:
            entry.release()

    def attempt(self, entry: Entry, request: Mapping[str, object], *,
                pulse=None, stop=None, retry: bool = False) -> dict:
        if retry:
            entry.restart()

        done = self.finished(entry)
        if done is not None:
            return done
        if entry.attempted:
            # Маркер стоит, а владельца нет: это итог прошлой попытки, а не работа.
            return self.taken(entry)

        with registry(self.root):
            # Считаем и занимаем место разом: иначе несколько процессов, глядя на один
            # и тот же лоток, стартуют одновременно и перевалят за предел.
            occupied = self.outbox.occupied()
            if occupied >= self.max_pending:
                # Лучше встать, чем молча забивать диск текстами, которые не забирают.
                broken = self.result("outbox_full", entry, None, None, None,
                                     reason=f"мест занято: {occupied}")
            else:
                entry.mark_started()   # место занято: следующий стартующий нас увидит
                broken = None
        if broken is not None:
            self.discard(entry)
            return broken

        before = self.guard.measure("before")
        # Выключенный замер — это решение вызывающего, а не неизвестный лимит.
        reason = self.guard.blocked(before) if self.policy.before else None
        if reason:
            self.discard(entry)   # в папке ничего нет: ход не начинался
            return self.result("limit_reached", entry, None, before, None, reason=reason)

        try:
            command = self.adapter.ask(entry, request, self.profile)
        except Exception:
            # Место уже занято, а хода не будет: иначе кривые задания забьют лоток.
            self.discard(entry)
            raise
        entry.update(state="running", pid=os.getpid())
        outcome = supervise(command, stdout=entry.stdout, stderr=entry.stderr,
                            pulse=pulse, stop=stop, timeout=self.timeout)
        entry.update(state="captured", pid=None, returncode=outcome.returncode,
                     interruption=outcome.interruption)
        reply = self.adapter.reply(entry, self.profile)
        state = "answered" if reply.complete else "incomplete"
        entry.update(state=state, session_id=reply.session_id, diagnostic=reply.diagnostic)
        after = self.guard.measure("after", session=reply.session_id)
        return self.result(state, entry, reply, before, after)

    def key_for(self, request: Mapping[str, object]) -> str:
        """Ключ считаем от запроса вместе со всем, что влияет на выполнение.

        Модель, усилие, режим песочницы — смена любого из них должна заводить новый ход:
        иначе старый ответ выдался бы за ответ на других условиях.
        """
        effective = dict(self.adapter.fingerprint())
        effective.update({key: value for key, value in request.items() if value is not None})
        return digest({"adapter": self.adapter.name, "request": effective})

    def pending(self) -> list[Entry]:
        return self.outbox.pending()

    def collect(self, entry: Entry | str) -> None:
        self.outbox.collect(entry)

    def discard(self, entry: Entry) -> None:
        """Уборка после несостоявшегося хода: её отказ не должен ронять ответ вызывающему."""
        try:
            entry.drop()
        except (OSError, RuntimeError) as exc:
            log.warning("Папку %s убрать не удалось: %s", entry.folder.name, exc)

    def finished(self, entry: Entry) -> dict | None:
        """Готовый ответ стоит ноль: отдаём его, не трогая ни лимит, ни состояние."""
        if entry.meta.get("state") != "answered":
            return None
        reply = self.adapter.reply(entry, self.profile)
        return self.result("resumed", entry, reply, None, None) if reply.complete else None

    def taken(self, entry: Entry) -> dict:
        """Прошлая попытка кончилась ничем: отдаём её итог, а не «всё ещё идёт»."""
        state = entry.meta.get("state") or "incomplete"
        reply = self.adapter.reply(entry, self.profile)
        if reply.complete:
            return self.result("resumed", entry, reply, None, None)
        # Повтор — только по явной просьбе: автоматика иначе будет бесконечно
        # переделывать то, что уже стоило денег.
        return self.result("incomplete" if state in UNFINISHED else state, entry, reply,
                           None, None,
                           reason="прошлая попытка не завершилась; повтор — run(..., retry=True)")

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
