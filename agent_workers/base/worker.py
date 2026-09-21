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
from .entry import Entry, digest, hold, let_go, registry
from .guard import Guard, LimitPolicy
from .outbox import Outbox
from .process import capture, interactive, supervise

log = logging.getLogger(__name__)

UNFINISHED = ("prepared", "reserved", "running", "captured")


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
            pulse=None, stop=None, retry: bool = False, ensure_login: bool = False) -> dict:
        entry = Entry(self.root, key or self.key_for(request))
        if not entry.claim():
            # Папкой владеет живой процесс. Ни повторять, ни двигать его журналы нельзя:
            # иначе второй вызов оплатит ту же работу, а состояние напишут оба сразу.
            return self.result("in_progress", entry, None, None, None,
                               reason="ход уже выполняется другим процессом")
        conversation = None
        try:
            if not retry:
                # Готовый ответ не трогает беседу — отдаём его, не дожидаясь её очереди.
                done = self.finished(entry)
                if done is not None:
                    return done
            session = request.get("session")
            if session:
                # Продолжения одной беседы идут по очереди: два --resume разом читали бы
                # и дописывали одну и ту же историю наперегонки.
                conversation = hold(self.session_file(session, "lock"))
                if conversation is None:
                    return self.result("in_progress", entry, None, None, None,
                                       reason="эту беседу сейчас продолжает другой ход")
            result = self.attempt(entry, request, pulse=pulse, stop=stop, retry=retry,
                                  ensure_login=ensure_login)
            if session and result["state"] == "answered":
                # Беседа продвинулась: тот же вопрос дальше — это уже другой ход.
                self.advance(session)
            return result
        finally:
            let_go(conversation)
            entry.release()

    def session_file(self, session: object, suffix: str) -> Path:
        return self.root / ".sessions" / f"{digest(str(session))}.{suffix}"

    def generation(self, session: object) -> int:
        """Сколько ходов этой беседы прошло через нас. Часть ключа хода."""
        try:
            return int(self.session_file(session, "gen").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0

    def advance(self, session: object) -> None:
        path = self.session_file(session, "gen")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(str(self.generation(session) + 1), encoding="utf-8")

    def attempt(self, entry: Entry, request: Mapping[str, object], *,
                pulse=None, stop=None, retry: bool = False, ensure_login: bool = False) -> dict:
        if retry:
            entry.restart()

        done = self.finished(entry)
        if done is not None:
            return done
        if entry.attempted:
            # Маркер стоит, а владельца нет: это итог прошлой попытки, а не работа.
            return self.taken(entry)
        if ensure_login:
            # Готовый ответ выше отдан без входа; проверяем его, только когда ход нужен.
            try:
                self.check()
            except Exception as exc:
                raise RuntimeError(f"вход не подтверждён: {exc}") from exc
        model = str(request.get("model") or getattr(self.adapter, "model", "") or "")

        with registry(self.root):
            # Считаем и занимаем место разом: иначе несколько процессов, глядя на один
            # и тот же лоток, стартуют одновременно и перевалят за предел.
            occupied = self.outbox.occupied(besides=entry.folder.name)
            if occupied >= self.max_pending:
                # Лучше встать, чем молча забивать диск текстами, которые не забирают.
                broken = self.result("outbox_full", entry, None, None, None,
                                     reason=f"мест занято: {occupied}")
            else:
                # Бронь, а не запуск: если нас убьют до старта CLI, никто не примет
                # пустую папку за оборванный ход — её просто выполнят заново.
                entry.update(state="reserved")
                broken = None
        if broken is not None:
            self.discard(entry)
            return broken

        before = self.guard.measure("before", model=model)
        # Выключенный замер — это решение вызывающего, а не неизвестный лимит.
        reason = self.guard.blocked(before) if self.policy.before else None
        if reason:
            self.discard(entry)   # в папке ничего нет: ход не начинался
            return self.result("limit_reached", entry, None, before, None, reason=reason)

        try:
            command = self.adapter.ask(entry, request, self.profile)
        except BaseException:
            # Место уже занято, а хода не будет — включая Ctrl+C в этот момент:
            # иначе кривые задания и обрывы тихо забьют лоток.
            self.discard(entry)
            raise
        entry.mark_started()   # CLI сейчас запустится: с этого места ход уже стоил денег
        entry.update(state="running", pid=os.getpid())
        try:
            outcome = supervise(command, stdout=entry.stdout, stderr=entry.stderr,
                                pulse=pulse, stop=stop, timeout=self.timeout)
            entry.update(state="captured", pid=None, returncode=outcome.returncode,
                         interruption=outcome.interruption)
            reply = self.adapter.reply(entry, self.profile)
        except BaseException as exc:
            # Ctrl+C или сбой разбора: папка обязана стать «незавершённой», а не зависнуть
            # в «идёт» — иначе она занимает место в лотке, а забрать её нельзя.
            entry.update(state="incomplete", pid=None, diagnostic=type(exc).__name__)
            raise
        state = "answered" if reply.complete else "incomplete"
        entry.update(state=state, session_id=reply.session_id, diagnostic=reply.diagnostic)
        if outcome.interruption == "stopped":
            # Нас попросили остановиться: не задерживаем выход паузами и запусками CLI.
            return self.result(state, entry, reply, before, None)
        after = self.guard.measure("after", session=reply.session_id, model=model)
        return self.result(state, entry, reply, before, after)

    def key_for(self, request: Mapping[str, object]) -> str:
        """Ключ считаем от запроса вместе со всем, что влияет на выполнение.

        Модель, усилие, режим песочницы — смена любого из них должна заводить новый ход:
        иначе старый ответ выдался бы за ответ на других условиях.
        """
        effective = dict(self.adapter.fingerprint())
        effective.update({key: value for key, value in request.items() if value is not None})
        session = request.get("session")
        if session:
            # Беседа — изменяемая: тот же вопрос после её продвижения даёт другой ответ,
            # и старый нельзя выдавать из лотка. Номер хода в беседе входит в ключ.
            effective["generation"] = self.generation(session)
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
            entry.update(state="answered")
            return self.result("resumed", entry, reply, None, None)
        if state in UNFINISHED:
            # Записываем итог: так папка попадёт в лоток, и её можно будет забрать.
            state = "incomplete"
            entry.update(state=state, pid=None)
        # Повтор — только по явной просьбе: автоматика иначе будет бесконечно
        # переделывать то, что уже стоило денег.
        return self.result(state, entry, reply, None, None,
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
