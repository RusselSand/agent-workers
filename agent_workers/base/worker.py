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

from .contract import Adapter, Profile, Reply
from .entry import Entry, digest, hold, let_go, private_dir, registry
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
                    if not entry.state_path.exists():
                        # Папку завели зря: без состояния её не увидит ни лоток, ни уборка.
                        self.discard(entry)
                    return self.result("in_progress", entry, None, None, None,
                                       reason="эту беседу сейчас продолжает другой ход")
            return self.attempt(entry, request, pulse=pulse, stop=stop, retry=retry,
                                ensure_login=ensure_login)
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

    def advance(self, session: object, to: int) -> None:
        """Продвинуть беседу не меньше чем до `to`. Повторный вызов ничего не портит."""
        if self.generation(session) >= to:
            return
        path = self.session_file(session, "gen")
        private_dir(path.parent)
        path.write_text(str(to), encoding="utf-8")

    def settle(self, entry: Entry) -> None:
        """Ответ получен — беседа продвинулась. Номер записан в самой папке, поэтому
        если процесс умер между ответом и продвижением, следующее чтение доделает."""
        meta = entry.meta
        if meta.get("session") and isinstance(meta.get("advances"), int):
            self.advance(meta["session"], meta["advances"])

    def attempt(self, entry: Entry, request: Mapping[str, object], *,
                pulse=None, stop=None, retry: bool = False, ensure_login: bool = False) -> dict:
        if not retry:
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
        # У повтора папка уже занята прежним результатом: ни брони, ни уборки — пока
        # все проверки не пройдены, старую попытку не трогаем, чтобы отказ её не стёр.
        fresh = not retry

        with registry(self.root):
            # Считаем и занимаем место разом: иначе несколько процессов, глядя на один
            # и тот же лоток, стартуют одновременно и перевалят за предел.
            occupied = self.outbox.occupied(besides=entry.folder.name)
            if occupied >= self.max_pending:
                # Лучше встать, чем молча забивать диск текстами, которые не забирают.
                broken = self.result("outbox_full", entry, None, None, None,
                                     reason=f"мест занято: {occupied}")
            else:
                if fresh:
                    # Бронь, а не запуск: если нас убьют до старта CLI, никто не примет
                    # пустую папку за оборванный ход — её просто выполнят заново.
                    entry.update(state="reserved")
                broken = None
        if broken is not None:
            if fresh:
                self.discard(entry)
            return broken

        before = self.guard.measure("before", model=model)
        # Выключенный замер — это решение вызывающего, а не неизвестный лимит.
        reason = self.guard.blocked(before) if self.policy.before else None
        if reason:
            if fresh:
                self.discard(entry)   # в папке ничего нет: ход не начинался
            return self.result("limit_reached", entry, None, before, None, reason=reason)

        if stop is not None and stop():
            # Остановку попросили, пока шли проверки: платный ход не начинаем.
            if fresh:
                self.discard(entry)
            return self.result("aborted", entry, None, before, None,
                               reason="остановка запрошена до запуска")

        if retry:
            entry.restart()   # проверки пройдены — теперь прежнюю попытку можно отодвинуть
        try:
            command = self.adapter.ask(entry, request, self.profile)
        except BaseException as exc:
            if fresh:
                # Место уже занято, а хода не будет — включая Ctrl+C в этот момент:
                # иначе кривые задания и обрывы тихо забьют лоток.
                self.discard(entry)
            else:
                # Архив прежней попытки цел; папка остаётся видимой в лотке.
                entry.update(state="incomplete", diagnostic=f"ask_error: {type(exc).__name__}")
            raise
        session = request.get("session")
        if session:
            # Продвижение беседы записываем в саму папку ещё до запуска: тогда ответ и
            # его следствие для беседы неразделимы, что бы ни случилось с процессом.
            entry.update(session=str(session), advances=self.generation(session) + 1)
        entry.mark_started()   # CLI сейчас запустится: с этого места ход уже стоил денег
        entry.update(state="running", pid=os.getpid())
        try:
            outcome = supervise(command, stdout=entry.stdout, stderr=entry.stderr,
                                pulse=pulse, stop=stop, timeout=self.timeout)
        except BaseException as exc:
            # Ctrl+C: папка обязана стать «незавершённой», а не зависнуть в «идёт» —
            # иначе она занимает место в лотке, а забрать её нельзя.
            entry.update(state="incomplete", pid=None, diagnostic=type(exc).__name__)
            raise
        entry.update(state="captured", pid=None, returncode=outcome.returncode,
                     interruption=outcome.interruption)
        reply = self.parse(entry)
        state = "answered" if reply.complete else "incomplete"
        entry.update(state=state, session_id=reply.session_id, diagnostic=reply.diagnostic)
        if state == "answered":
            self.settle(entry)
        if outcome.interruption == "stopped":
            # Нас попросили остановиться: не задерживаем выход паузами и запусками CLI.
            return self.result(state, entry, reply, before, None)
        after = self.guard.measure("after", session=reply.session_id, model=model)
        return self.result(state, entry, reply, before, after)

    def parse(self, entry: Entry) -> Reply:
        """Разбор журналов. Сбой разбора — это незавершённый ход, а не падение воркера:
        ход уже оплачен, и его папка не должна раз за разом ронять всех, кто её читает."""
        try:
            return self.adapter.reply(entry, self.profile)
        except Exception as exc:
            log.warning("Разбор ответа %s не удался: %s", entry.folder.name, type(exc).__name__)
            return Reply("", None, False, f"reply_error: {type(exc).__name__}: {exc}", {},
                         model=entry.meta.get("model"))

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
        reply = self.parse(entry)
        if not reply.complete:
            return None
        self.settle(entry)   # доделать продвижение беседы, если процесс упал до него
        return self.result("resumed", entry, reply, None, None)

    def taken(self, entry: Entry) -> dict:
        """Прошлая попытка кончилась ничем: отдаём её итог, а не «всё ещё идёт»."""
        state = entry.meta.get("state") or "incomplete"
        reply = self.parse(entry)
        if reply.complete:
            entry.update(state="answered")
            self.settle(entry)
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
