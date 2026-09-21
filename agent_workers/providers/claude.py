"""Claude Code под подпиской: сборка запроса, чтение ответа, расход подписки.

Расход берётся управляющим запросом get_usage: ходов модели он не делает и стоит
ноль. Ответ структурный, со временем сброса. Если клиент этот протокол не понимает,
остаётся запасной путь — человекочитаемый /usage, но точных дат там нет.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

from ..base.channel import Channel
from ..base.contract import Command, Cost, Limits, Profile, Rates, Reply, Usage, Window
from ..base.process import capture
from . import common
from .common import count, moment, number

ENV_HOME = "CLAUDE_CONFIG_DIR"
EXTRA_ENV = {"CLAUDE_CODE_OAUTH_TOKEN", "DISABLE_AUTOUPDATER"}

SUBSCRIPTIONS = ("pro", "max", "team", "enterprise")

CONTROL = ("-p", "--input-format", "stream-json", "--output-format", "stream-json",
           "--verbose", "--tools", "", "--no-session-persistence")

MCP_OFF = "{\"mcpServers\":{}}"

# Длительность окна в ответе не приходит, а для оценки скорости расхода она нужна.
GROUPS = {"session": timedelta(hours=5), "weekly": timedelta(days=7)}
LEGACY = {"five_hour": timedelta(hours=5), "seven_day": timedelta(days=7),
          "seven_day_opus": timedelta(days=7), "seven_day_sonnet": timedelta(days=7)}

# «Current week (all models): 22% used · resets Sep 26, 7pm (Europe/Madrid)»
USAGE = re.compile(r"^Current ([^:]+):\s*(\d+(?:\.\d+)?)% used(?:\s*·\s*resets (.+?))?\s*$", re.M)

DATED = re.compile(r"-\d{8}$")
SOURCE = "anthropic, справочник claude-api (кэш 2026-06-24)"


def price_of(input_price: str, output_price: str, cached: str | None = None) -> Rates:
    """Цена за миллион токенов. Чтение из кеша — 0.1× от входа, запись — 1.25× (5 минут)."""
    base = Decimal(input_price)
    return Rates(base, Decimal(output_price),
                 Decimal(cached) if cached else base / 10,
                 base * Decimal("1.25"), "USD", SOURCE)


PRICES = {
    "claude-opus-5": price_of("5", "25"),
    "claude-opus-4-8": price_of("5", "25"),
    "claude-opus-4-7": price_of("5", "25"),
    "claude-sonnet-5": price_of("2", "10"),
    "claude-haiku-4-5": price_of("1", "5"),
    "claude-fable-5-1": price_of("10", "50", "0.25"),  # у этой модели кеш дешевле обычных 0.1×
}


def last_json(text: str) -> dict:
    """Ответ приходит и одной строкой (print-режим), и с отступами (auth status),
    иногда после предупреждения обновлятора. Разбираем все три случая."""
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except ValueError:
        pass
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    start = stripped.find("{")
    if start >= 0:
        try:
            return json.JSONDecoder().raw_decode(stripped[start:])[0]
        except ValueError:
            pass
    raise ValueError("В выводе Claude нет JSON")


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", value.lower()).strip("_")[:60]


def scoped_model(row: Mapping) -> str | None:
    """Имя модели, к которой привязано окно, или None для окна на весь аккаунт."""
    model = (row.get("scope") or {}).get("model") or {}
    return model.get("display_name") or model.get("id") or None


def concerns(scoped: str | None, model: str) -> bool:
    """Окно на весь аккаунт касается всех; окно модели — только её самой.

    Управляющий протокол называет модель коротко («Fable», «Sonnet»), а в настройках
    она идёт полным именем («claude-fable-5-1»), поэтому сравниваем по вхождению.
    """
    if not scoped or not model:
        return True
    return slug(scoped) in slug(model)


def row_name(row: Mapping) -> str:
    """session · weekly_all · окно, привязанное к модели -> weekly_scoped:fable"""
    kind = str(row.get("kind") or "window")
    model = scoped_model(row)
    return kind + ":" + slug(model) if model else kind


def control_windows(rate_limits: Mapping, model: str = "") -> tuple[Window, ...]:
    """Новая форма — самоописательный список limits[]; старая — окна отдельными ключами.

    Окна чужих моделей отбрасываем: их процент ограничивает не нас, а Guard берёт
    максимум по всему, что ему отдали.
    """
    rows = rate_limits.get("limits")
    if isinstance(rows, list) and rows:
        return tuple(Window(row_name(row), float(row["percent"]),
                            GROUPS.get(str(row.get("group"))), moment(row.get("resets_at")))
                     for row in rows
                     if number(row.get("percent")) and concerns(scoped_model(row), model))
    windows = []
    for key, length in LEGACY.items():
        value = rate_limits.get(key)
        family = key.removeprefix("seven_day_") if key.startswith("seven_day_") else None
        if isinstance(value, dict) and number(value.get("utilization")) \
                and concerns(family, model):
            windows.append(Window(key, float(value["utilization"]), length,
                                  moment(value.get("resets_at"))))
    for value in rate_limits.get("model_scoped") or []:
        name = str(value.get("display_name", "?"))
        if number(value.get("utilization")) and concerns(name, model):
            windows.append(Window("model:" + slug(name), float(value["utilization"]),
                                  timedelta(days=7), moment(value.get("resets_at"))))
    return tuple(windows)


def window_name(label: str) -> str:
    """session · week (all models) · week (Fable) -> session · week · week:fable"""
    label = label.strip().lower()
    inner = re.fullmatch(r"week \((.+)\)", label)
    if not inner:
        return label
    return "week" if inner[1] == "all models" else "week:" + inner[1]


def usage_windows(text: str, model: str = "") -> tuple[Window, ...]:
    windows = []
    for label, percent, resets in USAGE.findall(text):
        name = window_name(label)
        scoped = name.partition(":")[2] or None
        if concerns(scoped, model):
            windows.append(Window(name, float(percent), resets_hint=(resets or None)))
    return tuple(windows)


def tokens_of(usage: Mapping) -> Usage:
    """input здесь — уже неподкешированный остаток, так его отдаёт сама CLI."""
    details = usage.get("output_tokens_details") or {}
    return Usage(count(usage.get("input_tokens")),
                 count(usage.get("cache_read_input_tokens")),
                 count(usage.get("cache_creation_input_tokens")),
                 count(usage.get("output_tokens")),
                 count(details.get("thinking_tokens")))


def canonical(usage: Mapping) -> str | None:
    """modelUsage знает и точное имя снапшота, и каноническое — второе в таблице цен."""
    for name, value in (usage.get("modelUsage") or {}).items():
        if isinstance(value, dict) and value.get("canonicalModel"):
            return str(value["canonicalModel"])
        return DATED.sub("", str(name))
    return None


def terminal_usage(terminal: Mapping) -> dict:
    return {**(terminal.get("usage") or {}),
            "total_cost_usd": terminal.get("total_cost_usd"),
            "modelUsage": terminal.get("modelUsage") or {}}


@dataclass
class ClaudeAdapter:
    executable: str | None = None
    model: str = "claude-opus-5"
    name: ClassVar[str] = "claude"

    def __post_init__(self) -> None:
        self.executable = common.find_executable(self.executable, "AGENT_CLAUDE_BINARY",
                                                 "claude.exe", "claude")

    def fingerprint(self) -> Mapping[str, str]:
        return {"model": self.model}

    def environment(self, profile: Profile) -> Mapping[str, str]:
        return common.environment(profile, ENV_HOME, EXTRA_ENV)

    def check(self, profile: Profile) -> Command:
        return Command((self.executable, "auth", "status"), self.environment(profile),
                       profile.home, timeout=30)

    def login(self, profile: Profile) -> Command:
        return Command((self.executable, "auth", "login"), self.environment(profile), profile.home)

    def logout(self, profile: Profile) -> Command:
        return Command((self.executable, "auth", "logout"), self.environment(profile), profile.home)

    def verify(self, captured: str) -> None:
        try:
            auth = last_json(captured)
        except ValueError:
            raise RuntimeError("Не удалось прочитать статус входа Claude") from None
        subscription = (auth.get("authMethod") == "claude.ai"
                        and auth.get("subscriptionType") in SUBSCRIPTIONS)
        token = (bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
                 and auth.get("authMethod") in ("oauth_token", "claude.ai"))
        if not auth.get("loggedIn") or not (subscription or token):
            raise RuntimeError("Нужен вход Claude по подписке; оплата по API-ключу отключена")

    def ask(self, entry, request: Mapping[str, object], profile: Profile) -> Command:
        system = entry.write("invocation/system.md", str(request.get("system") or ""))
        stdin = entry.write("invocation/input.txt", str(request["user"]))
        session = request.get("session")
        model = str(request.get("model") or self.model)
        entry.update(model=model)
        # Без --include-partial-messages: пословные фрагменты раздували бы журнал,
        # а разбор всё равно читает только целые сообщения и итог.
        argv = [self.executable, "-p", "--output-format", "stream-json", "--verbose",
                "--model", model, "--tools", "", "--strict-mcp-config", "--mcp-config", MCP_OFF,
                "--system-prompt-file", str(system)]
        # Сессию сохраняем всегда: её идентификатор уходит вызывающему, и он вправе
        # продолжить беседу. С --no-session-persistence такое продолжение невозможно.
        if session:
            argv += ["--resume", str(session)]
        return Command(tuple(argv), self.environment(profile), entry.folder, stdin)

    def reply(self, entry, profile: Profile) -> Reply:
        streamed, terminal, session = [], None, None
        for line in entry.read("stdout.jsonl").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue  # Обрезанный хвост остаётся в журнале как есть.
            if not isinstance(item, dict) or item.get("parent_tool_use_id"):
                continue
            session = item.get("session_id") or session
            if item.get("type") == "result":
                terminal = item
            elif item.get("type") == "assistant":
                streamed += [part.get("text", "") for part in item["message"].get("content", [])
                             if isinstance(part, dict) and part.get("type") == "text"]
        model = entry.meta.get("model")
        if terminal is None:
            return Reply("".join(streamed), session, False, "no_terminal_result", {}, model=model)
        usage = terminal_usage(terminal)
        model = canonical(usage) or model
        text = terminal.get("result") if isinstance(terminal.get("result"), str) else ""
        if terminal.get("is_error"):
            # Ошибка тоже оплачена: токены и её текст должны дойти до вызывающего.
            return Reply("".join(streamed), session, False, "cli_error: " + (text or "без текста"),
                         usage, tokens_of(usage), model)
        return Reply(text, session, True, None, usage, tokens_of(usage), model)

    def price(self, reply: Reply) -> Cost | None:
        """CLI считает стоимость сама — свою таблицу пускаем в ход, только если не посчитала."""
        reported = (reply.usage or {}).get("total_cost_usd")
        if number(reported):
            return Cost(Decimal(str(reported)), "USD", "cli")
        return common.table_price(reply, PRICES)

    def limits(self, profile: Profile, *, session: str | None = None,
               model: str | None = None) -> Limits | None:
        model = model or self.model
        try:
            payload = self.usage(profile)
        except Exception:
            # Управляющий протокол помечен как экспериментальный: его отказ — не авария.
            return self.printed_usage(profile, model)
        windows = control_windows(payload.get("rate_limits") or {}, model)
        if not payload.get("rate_limits_available") or not windows:
            return self.printed_usage(profile, model)
        return Limits(self.name, profile.name, windows, datetime.now(UTC), "control", True,
                      plan=payload.get("subscription_type"))

    def usage(self, profile: Profile) -> dict:
        """Управляющий запрос get_usage: ни одного хода модели."""
        command = Command((self.executable, *CONTROL), self.environment(profile),
                          profile.home, timeout=30)
        payload: dict = {}
        with Channel(command) as channel:
            for request_id, subtype in (("init", "initialize"), ("usage", "get_usage")):
                channel.send({"type": "control_request", "request_id": request_id,
                              "request": {"subtype": subtype}})
                event = channel.wait(lambda item, wanted=request_id:
                                     item.get("type") == "control_response"
                                     and (item.get("response") or {}).get("request_id") == wanted)
                response = event.get("response") or {}
                if response.get("subtype") != "success":
                    raise RuntimeError("Этот клиент не отдаёт расход по управляющему протоколу")
                payload = response.get("response") or {}
        return payload

    def printed_usage(self, profile: Profile, model: str = "") -> Limits | None:
        """Запасной путь: то же, что показывает /usage человеку."""
        text = last_json(capture(self.usage_command(profile))).get("result", "")
        windows = usage_windows(text, model)
        if not windows:
            return None
        # Время сброса приходит текстом без года — точную дату отсюда не соберёшь.
        return Limits(self.name, profile.name, windows, datetime.now(UTC), "usage", True)

    def usage_command(self, profile: Profile) -> Command:
        return Command((self.executable, "-p", "--output-format", "json", "/usage"),
                       self.environment(profile), profile.home, timeout=120)
