"""Один ход целиком: замер до, запрос, ответ, замер после.

Здесь нет ни одного знания о конкретной CLI: всё, что от неё зависит, приходит
адаптером. Менять этот файл придётся, только если поменяется сама схема работы —
например, ход перестанет быть «один процесс, один ответ».

Ключ хода — идентификатор задачи от координатора. Уникальность задачи, аренда и
решение о повторе — его забота. Воркер отвечает за одно: оплаченный ответ не
теряется, пока его не забрали. Поэтому порядок такой: сначала смотрим, нет ли в
папке готового ответа, и только потом тратим лимит.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .contract import Adapter, Profile, Reply
from .entry import Entry, private_dir
from .guard import Guard, LimitPolicy
from .outbox import Outbox
from .process import IDLE_TIMEOUT, TURN_TIMEOUT, capture, interactive, seconds, supervise

log = logging.getLogger(__name__)


class LoginRequired(RuntimeError):
    """Вход в подписку не подтвердился: ход не начинался, и следующий не начнётся тоже,
    пока вход не починят снаружи."""



def workspace(request: Mapping[str, object]) -> Path | None:
    """Каталог, который модель читает в этом ходе: request["workspace"]. Нет — ход без файлов.
    Есть, но это не каталог, — ошибка запроса: ход без нужных файлов ответил бы ни о чём, а
    деньги взял бы."""
    value = request.get("workspace")
    if value is None or value == "":
        return None
    path = Path(str(value)).resolve()
    if not path.is_dir():
        raise ValueError(f"workspace — не каталог: {path}")
    return path


def with_workspace(request: Mapping[str, object]) -> Mapping[str, object]:
    """Запрос с проверенным каталогом: провайдер получает уже полный путь."""
    place = workspace(request)
    return {**request, "workspace": str(place)} if place else request


def span(limit: float) -> str:
    return f"{limit / 60:g} мин" if limit >= 60 else f"{limit:g} с"


@dataclass
class Worker:
    adapter: Adapter
    profile: Profile
    root: Path
    policy: LimitPolicy = field(default_factory=LimitPolicy)
    timeout: float | None = TURN_TIMEOUT   # весь ход, с; None — без предела
    idle: float | None = IDLE_TIMEOUT      # CLI молчит, ни строки в журналах, с; None — не следить

    def __post_init__(self) -> None:
        # Проверяем при сборке: негодный предел выяснился бы только в оплаченном ходе.
        self.timeout = seconds(self.timeout, "Предел хода")
        self.idle = seconds(self.idle, "Предел молчания CLI")
        self.guard = Guard(self.adapter, self.profile, self.policy)
        self.outbox = Outbox(self.root)

    def check(self) -> None:
        """Проверка подписочного входа. Бросает, если входа нет."""
        self.adapter.verify(capture(self.adapter.check(self.profile)))

    def login(self) -> int:
        private_dir(self.profile.home)   # сюда лягут токены входа
        return interactive(self.adapter.login(self.profile))

    def logout(self) -> int:
        return interactive(self.adapter.logout(self.profile))

    def run(self, request: Mapping[str, object], *, key: str,
            stop=None, ensure_login: bool = False) -> dict:
        """Аренду задачи здесь не продлевают: это забота того, кто взял задачу у
        координатора, — на всё время обработки, а не только пока идёт CLI."""
        entry = Entry(self.root, key)
        if not entry.claim():
            # Папкой владеет живой процесс: второй вызов оплатил бы ту же работу.
            return self.result("in_progress", entry, None, None, None,
                               reason="ход уже выполняется другим процессом")
        try:
            done = self.finished(entry)
            if done is not None:
                return done
            return self.attempt(entry, request, stop=stop, ensure_login=ensure_login)
        finally:
            entry.release()

    def attempt(self, entry: Entry, request: Mapping[str, object], *,
                stop=None, ensure_login: bool = False) -> dict:
        """Готового ответа в папке нет — ход делается заново.

        Что бы там ни лежало — ничего или след оборванной попытки, — задачу выдали
        снова, значит, повтор нужен. Сколько раз повторять, считает координатор.
        """
        model = str(request.get("model") or getattr(self.adapter, "model", "") or "")
        try:
            # Негодный запрос — ошибка запроса до проверок входа и замеров: с ним ход всё равно
            # не начнётся, а «нет входа» или «лимит» вместо неё заставили бы повторять его зря.
            request = with_workspace(request)
            refusal, before = self.preflight(entry, model, stop=stop, ensure_login=ensure_login)
            if refusal is not None:
                return refusal
            entry.reset()
            command = self.adapter.ask(entry, request, self.profile)
        except BaseException:
            # Отказ до запуска — включая Ctrl+C в этот момент: ход не стоил ничего,
            # и папка без хода никому не нужна.
            self.discard(entry)
            raise
        launched = False

        def running() -> None:
            nonlocal launched
            # Сначала факт, потом запись о нём: процесс уже запущен, и если запись
            # сорвётся, это обрыв хода, а не отказ до него — журналы нельзя сносить.
            launched = True
            entry.update(state="running", pid=os.getpid())   # с этого места ход стоит денег

        try:
            outcome = supervise(command, stdout=entry.stdout, stderr=entry.stderr,
                                stop=stop, on_start=running,
                                timeout=self.timeout, idle=self.idle)
        except BaseException as exc:
            if not launched:
                # Процесс так и не стартовал — не открылись журналы, не запустилась CLI:
                # это такой же отказ до запуска, как и все выше, и папку за оплаченную
                # попытку выдавать нельзя.
                self.discard(entry)
            else:
                # Ctrl+C посреди хода: папка обязана стать «незавершённой», а не «идёт».
                entry.update(state="incomplete", pid=None, diagnostic=type(exc).__name__)
            raise
        entry.update(state="captured", pid=None, returncode=outcome.returncode,
                     interruption=outcome.interruption)
        reply = self.parse(entry)
        state = "answered" if reply.complete else "incomplete"
        entry.update(state=state, diagnostic=reply.diagnostic)
        reason = None if reply.complete else self.interrupted(outcome.interruption)
        if outcome.interruption == "stopped" or (stop is not None and stop()):
            # Нас попросили остановиться — в том числе сразу после того, как CLI вышла
            # сама: ответ уже в папке, а замер «после» стоит минуты пауз и запусков CLI.
            return self.result(state, entry, reply, before, None, reason=reason)
        after = self.guard.measure("after", session=reply.session_id, model=model)
        return self.result(state, entry, reply, before, after, reason=reason)

    def interrupted(self, interruption: str | None) -> str | None:
        """Почему ход сняли, если сняли мы, — словами. Диагностика CLI скажет лишь, что
        ответ не дописан, а не почему."""
        match interruption:
            case "stopped":
                return "остановлен по запросу"
            case "timeout":
                return f"не уложился в предел хода: {span(self.timeout)}"
            case "idle":
                return f"CLI молчала {span(self.idle)}, ход снят как зависший"
            case _:
                return None

    def preflight(self, entry: Entry, model: str, *, stop=None,
                  ensure_login: bool = False) -> tuple[dict | None, object]:
        """Проверки до хода: вход и лимит. Вернёт (отказ, замер) или (None, замер «до»).

        Остановку смотрим перед каждой пробой, а не только в конце: проба входа и замер
        лимита занимают до минут, и начинать их, когда пора выходить, незачем.
        """
        stopping = stop or (lambda: False)

        def aborted(before=None) -> dict:
            # Остановку попросили до запуска: платный ход не начинаем, папка не нужна.
            self.discard(entry)
            return self.result("aborted", entry, None, before, None,
                               reason="остановка запрошена до запуска")

        if stopping():
            return aborted(), None
        if ensure_login:
            # Готовый ответ выше отдан без входа; проверяем его, только когда ход нужен.
            try:
                self.check()
            except Exception as exc:
                raise LoginRequired(f"вход не подтверждён: {exc}") from exc
            if stopping():
                return aborted(), None
        before = self.guard.measure("before", model=model)
        # Выключенный замер — это решение вызывающего, а не неизвестный лимит.
        reason = self.guard.blocked(before) if self.policy.before else None
        if reason:
            self.discard(entry)   # ход не начинался — папке незачем оставаться
            return self.result("limit_reached", entry, None, before, None, reason=reason), before
        if stopping():
            return aborted(before), before
        return None, before

    def parse(self, entry: Entry) -> Reply:
        """Разбор журналов. Сбой разбора — это незавершённый ход, а не падение воркера:
        ход уже оплачен, и его папка не должна раз за разом ронять всех, кто её читает."""
        try:
            return self.adapter.reply(entry, self.profile)
        except Exception as exc:
            log.warning("Разбор ответа %s не удался: %s", entry.folder.name, type(exc).__name__)
            return Reply("", None, False, f"reply_error: {type(exc).__name__}: {exc}", {},
                         model=entry.meta.get("model"))

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
        """Готовый ответ уже оплачен: отдаём его, не трогая ни лимит, ни вход.

        Смотрим в журнал, а не только в состояние: если процесс умер между ответом CLI
        и записью состояния, ответ всё равно лежит в папке, и платить второй раз незачем.
        """
        if not entry.meta:
            return None   # хода здесь ещё не было
        reply = self.parse(entry)
        if not reply.complete:
            return None
        if entry.meta.get("state") != "answered":
            entry.update(state="answered", pid=None)
        return self.result("resumed", entry, reply, None, None)

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
