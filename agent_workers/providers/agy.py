"""Antigravity CLI (agy) под подпиской Google: сборка запроса, чтение ответа.

Gemini CLI подписку Google AI Pro/Ultra больше не обслуживает (с 18.06.2026), подписочный
клиент теперь — agy. Ход должен быть как запрос в API: наш system и наш user. Встроенный промпт
agy (≈15 тысяч символов) и его инструменты снимаются только своим агентом: файл в
~/.gemini/config/agents, где excludeDefaultComponents делает текст файла всем системным
промптом, inheritCustomizations: false не пускает правила, GEMINI.md, AGENTS.md, скиллы и MCP,
а tools — список инструментов. Остаётся то, что agy не выключает: инструмент manage_task
(≈400 токенов), обёртка сообщения со временем и сменой модели (≈120 токенов) и вызов
маленькой модели за заголовком беседы — в usage он не попадает. Так на пути с API-ключом
(запись запроса, agy 1.4.2). По подписке agy excludeDefaultComponents не выполняет и добавляет
к system свои разделы — user_information, messaging, artifacts, guidelines, communication_style:
ход «ок» стоил ≈2,2 тысячи токенов входа (видно по сохранённой беседе, 20.10.2026).

Своего каталога настроек у agy нет — профиль отделяется подменой HOME (на Windows ещё и
USERPROFILE). Токены входа agy держит в хранилище паролей ОС, если оно есть: на Windows и на
Linux с D-Bus оно одно на пользователя ОС, и два профиля agy там делят один вход. Отдельные
входы — только где хранилища нет (контейнер): тогда токены лежат файлом в HOME.

Расхода подписки без хода agy по подписке пока не отдаёт в известном нам виде: /usage по
подписке ещё не видели, поэтому limits — None, и решает политика.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import ClassVar

from ..base.contract import Command, Cost, Limits, Profile, Reply, Usage
from ..base.entry import PRIVATE_FILE, make_private, private_dir
from . import common
from .common import count

log = logging.getLogger(__name__)

ENV_HOME = "HOME"

# Обновлятор agy стартует фоном при каждом запуске: в ходе ему делать нечего.
QUIET = {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"}

# Уровни, которые понимает --effort. Без флага уровень выбирает сама CLI.
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Инструменты хода с каталогом: только чтение и поиск. Файлы вне каталога agy читать не даёт
# (проверено: view_file вне рабочего каталога — permission check failed).
READ_ONLY = ("view_file", "grep_search", "find_by_name", "list_dir")

# Идентификатор беседы приходит из вывода CLI и становится частью пути при чистке: только UUID.
CONVERSATION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

SIGNED_OUT = "sign in"


def data_dir(home: Path) -> Path:
    return home / ".gemini" / "antigravity-cli"


def agents_dir(home: Path) -> Path:
    return home / ".gemini" / "config" / "agents"


def agent_of(system: str, tools: tuple[str, ...]) -> tuple[str, str]:
    """Имя агента и текст его файла. Имя — от содержимого: один system — один файл, сколько бы
    ходов его ни использовали, а упавший ход не оставляет после себя ничего лишнего."""
    digest = hashlib.sha256(("\0".join(tools) + "\0\0" + system).encode("utf-8")).hexdigest()
    name = "aw-" + digest[:16]
    header = ("---", f"name: {name}", "description: agent-workers turn",
              f"tools: [{', '.join(tools)}]", "mainAgent: true", "subagent: false",
              "excludeDefaultComponents: true", "inheritCustomizations: false", "---")
    return name, "\n".join(header) + "\n" + system


def put_agent(home: Path, name: str, text: str) -> Path:
    """Файл агента пишется, только если его ещё нет такого: через временный файл и замену,
    чтобы соседний ход не прочитал его наполовину."""
    folder = private_dir(agents_dir(home))
    path = folder / f"{name}.md"
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return path
    draft = folder / f".{name}.{os.getpid()}.tmp"
    draft.write_text(text, encoding="utf-8")
    make_private(draft, PRIVATE_FILE)
    os.replace(draft, path)
    return path


def read_stream(text: str) -> tuple[list[str], dict | None, str | None]:
    """Текст ответа по частям, итоговое событие и беседа — из журнала stream-json."""
    streamed, terminal, conversation = [], None, None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue  # Обрезанный хвост остаётся в журнале как есть.
        if not isinstance(item, dict):
            continue
        event = item.get("event")
        body = item.get(event) if isinstance(item.get(event), dict) else {}
        conversation = item.get("conversation_id") or body.get("conversation_id") or conversation
        if event == "result":
            terminal = body
        elif event == "step_update" and body.get("step_type") == "agent_response":
            streamed.append(str(body.get("text_delta") or ""))
    return streamed, terminal, conversation


def failure_of(result: Mapping) -> str:
    error = result.get("error")
    text = error.get("message") if isinstance(error, dict) else error
    return str(text or result.get("status") or "без текста")


def tokens_of(usage: Mapping) -> Usage:
    """input_tokens — весь запрос вместе с прочитанным из кеша, как promptTokenCount у Gemini API
    (на подписке не проверено: кеш в замерах был нулевой). Размышления уже входят в output_tokens:
    на подписке «ок» дал output 127 при thinking 126."""
    cached = count(usage.get("cache_read_tokens"))
    return Usage(max(0, count(usage.get("input_tokens")) - cached), cached, 0,
                 count(usage.get("output_tokens")), count(usage.get("thinking_tokens")))


@dataclass
class AgyAdapter:
    executable: str | None = None
    model: str | None = None     # без модели её выбирает сама CLI
    effort: str | None = None
    name: ClassVar[str] = "agy"

    def __post_init__(self) -> None:
        self.model = self.model or None
        self.effort = self.effort or None
        if self.effort is not None and self.effort not in EFFORTS:
            raise ValueError(f"Уровень effort у agy — {', '.join(EFFORTS)}; сейчас: {self.effort!r}")
        self.executable = common.find_executable(self.executable, "AGENT_AGY_BINARY", "agy.exe", "agy")

    def environment(self, profile: Profile) -> Mapping[str, str]:
        env = common.environment(profile, ENV_HOME)
        if os.name == "nt":
            # Домашний каталог Go на Windows берёт из USERPROFILE, а не из HOME.
            env["USERPROFILE"] = str(profile.home)
        return {**env, **QUIET}

    def check(self, profile: Profile) -> Command:
        # Список моделей без входа сразу падает с «Please sign in»; хода модели он не делает.
        return Command((self.executable, "models"), self.environment(profile), profile.home, timeout=30)

    def login(self, profile: Profile) -> Command:
        # Отдельной команды входа нет: agy без аргументов сам ведёт на вход через браузер.
        return Command((self.executable,), self.environment(profile), profile.home)

    def logout(self, profile: Profile) -> Command:
        # Выход — команда /logout внутри agy: в print-режиме её нет.
        return Command((self.executable,), self.environment(profile), profile.home)

    def verify(self, captured: str) -> None:
        if SIGNED_OUT in captured.lower() or "\t" not in captured:
            raise RuntimeError("Нужен вход agy по подписке Google: python -m agent_workers login")

    def ask(self, entry, request: Mapping[str, object], profile: Profile) -> Command:
        system = str(request.get("system") or "")
        entry.write("invocation/system.md", system)
        # Длинный текст в -p упирается в длину командной строки, а простой stdin agy не читает.
        # Режим stream-json читает задание со stdin, одно сообщение — один ход; -p "" нужен,
        # чтобы включить print-режим, сам текст из флага в этом режиме не используется.
        message = {"event": "user", "message": {"content": str(request["user"])}}
        stdin = entry.write("invocation/input.ndjson", json.dumps(message, ensure_ascii=False) + "\n")
        model = str(request.get("model") or self.model or "") or None
        entry.update(model=model)
        # С каталогом ход идёт в нём, и модель может его читать — только читать: других
        # инструментов у агента нет. Без каталога инструментов нет вовсе.
        place = common.workspace(request)
        name, text = agent_of(system, READ_ONLY if place else ())
        put_agent(profile.home, name, text)
        argv = (self.executable, "-p", "", "--agent", name, "--disable-slash-commands",
                "--output-format", "stream-json", "--input-format", "stream-json",
                *(("--model", model) if model else ()),
                *(("--effort", self.effort) if self.effort else ()))
        return Command(argv, self.environment(profile), place or entry.folder, stdin)

    def reply(self, entry, profile: Profile) -> Reply:
        streamed, terminal, conversation = read_stream(entry.read("stdout.jsonl"))
        self.forget(profile, conversation)
        model = entry.meta.get("model")
        if terminal is None:
            return Reply("".join(streamed), conversation, False, "no_terminal_result", {}, model=model)
        usage = dict(terminal.get("usage") or {})
        text = terminal.get("response") if isinstance(terminal.get("response"), str) else ""
        if terminal.get("status") != "SUCCESS":
            # Ошибка тоже оплачена: токены и её текст должны дойти до вызывающего.
            return Reply(text or "".join(streamed), conversation, False,
                         "cli_error: " + failure_of(terminal), usage, tokens_of(usage), model)
        text = text or "".join(streamed)
        if not text:
            return Reply("", conversation, False, "empty_result", usage, tokens_of(usage), model)
        return Reply(text, conversation, True, None, usage, tokens_of(usage), model)

    def forget(self, profile: Profile, conversation: str | None) -> None:
        """Беседу agy сохраняет всегда. Ход — вопрос и ответ, продолжать его никто не будет:
        без чистки каталог учётной записи рос бы с каждым ходом. Общую базу заголовков и логи
        не трогаем — в них нет отдельного файла на беседу."""
        if not conversation or not CONVERSATION.fullmatch(conversation):
            return
        data = data_dir(profile.home)
        for path in (data / "conversations" / f"{conversation}.db",
                     data / "presence" / f"{conversation}.lock",
                     data / "annotations" / f"{conversation}.pbtxt"):
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                log.warning("Файл беседы %s не удалить: %s", path.name, type(exc).__name__)
        shutil.rmtree(data / "brain" / conversation, ignore_errors=True)

    def price(self, reply: Reply) -> Cost | None:
        """Таблицы цен Gemini пока нет: оценки по API не даём, а не выдумываем."""
        return None

    def limits(self, profile: Profile, *, session: str | None = None,
               model: str | None = None,
               fresh_within: timedelta | None = None) -> Limits | None:
        """Ответ /usage по подписке ещё не видели — лимит неизвестен, решает политика."""
        return None
