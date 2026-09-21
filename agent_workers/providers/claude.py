"""Claude Code под подпиской: сборка запроса, чтение ответа, расход подписки.

Расход берётся управляющим запросом get_usage: ходов модели он не делает и стоит
ноль. Ответ структурный, со временем сброса. Если клиент этот протокол не понимает,
остаётся запасной путь — человекочитаемый /usage, но точных дат там нет.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

from ..base.channel import Channel
from ..base.contract import Command, Cost, Limits, Profile, Rates, Reply, Usage, Window
from ..base.pricing import estimate
from ..base.process import capture

ENV_HOME = "CLAUDE_CONFIG_DIR"

# Ключи проекта в подпроцесс не уезжают: пропускаем только то, без чего CLI не живёт.
ALLOWED = {"APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME",
           "TEMP", "TMP", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATH", "PATHEXT",
           "PROGRAMFILES", "PROGRAMW6432", "PROGRAMFILES(X86)", "WINDIR", "LANG",
           "PYTHONUTF8", "TERM", "TZ", "CLAUDE_CODE_OAUTH_TOKEN", "DISABLE_AUTOUPDATER"}

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


def moment(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "_", value.lower()).strip("_")[:60]


def row_name(row: Mapping) -> str:
    """session · weekly_all · окно, привязанное к модели -> weekly_scoped:fable"""
    kind = str(row.get("kind") or "window")
    model = ((row.get("scope") or {}).get("model") or {}).get("display_name")
    return kind + ":" + slug(model) if model else kind


def control_windows(rate_limits: Mapping) -> tuple[Window, ...]:
    """Новая форма — самоописательный список limits[]; старая — окна отдельными ключами.

    Список предпочтительнее: новое окно приезжает в нём строкой, а не ключом, который
    здесь пришлось бы заводить руками.
    """
    rows = rate_limits.get("limits")
    if isinstance(rows, list) and rows:
        return tuple(Window(row_name(row), float(row["percent"]),
                            GROUPS.get(str(row.get("group"))), moment(row.get("resets_at")))
                     for row in rows if number(row.get("percent")))
    windows = []
    for key, length in LEGACY.items():
        value = rate_limits.get(key)
        if isinstance(value, dict) and number(value.get("utilization")):
            windows.append(Window(key, float(value["utilization"]), length,
                                  moment(value.get("resets_at"))))
    for value in rate_limits.get("model_scoped") or []:
        if number(value.get("utilization")):
            windows.append(Window("model:" + slug(str(value.get("display_name", "?"))),
                                  float(value["utilization"]), timedelta(days=7),
                                  moment(value.get("resets_at"))))
    return tuple(windows)


def window_name(label: str) -> str:
    """session · week (all models) · week (Fable) -> session · week · week:fable"""
    label = label.strip().lower()
    inner = re.fullmatch(r"week \((.+)\)", label)
    if not inner:
        return label
    return "week" if inner[1] == "all models" else "week:" + inner[1]


def usage_windows(text: str) -> tuple[Window, ...]:
    return tuple(Window(window_name(label), float(percent), resets_hint=(resets or None))
                 for label, percent, resets in USAGE.findall(text))


def count(value) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


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


@dataclass
class ClaudeAdapter:
    executable: str | None = None
    model: str = "claude-opus-5"
    name: ClassVar[str] = "claude"

    def __post_init__(self) -> None:
        self.executable = (self.executable or os.environ.get("AGENT_CLAUDE_BINARY")
                           or shutil.which("claude.exe") or shutil.which("claude"))
        if not self.executable or not Path(self.executable).is_file():
            raise RuntimeError("Claude Code не найден")

    def environment(self, profile: Profile) -> Mapping[str, str]:
        profile.home.mkdir(parents=True, exist_ok=True)
        base = {k: v for k, v in os.environ.items() if k.upper() in ALLOWED}
        return {**base, ENV_HOME: str(profile.home)}

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
        system = entry.write("invocation/system.md", str(request.get("system", "")))
        stdin = entry.write("invocation/input.txt", str(request["user"]))
        session = request.get("session")
        model = str(request.get("model") or self.model)
        entry.update(model=model)
        argv = [self.executable, "-p", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages", "--model", model,
                "--tools", "", "--strict-mcp-config", "--mcp-config", MCP_OFF,
                "--system-prompt-file", str(system)]
        # Без продолжения сессии не храним её вовсе: меньше следов чужой работы на диске.
        argv += ["--resume", str(session)] if session else ["--no-session-persistence"]
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
        if terminal and not terminal.get("is_error") and isinstance(terminal.get("result"), str):
            usage = {**(terminal.get("usage") or {}),
                     "total_cost_usd": terminal.get("total_cost_usd"),
                     "modelUsage": terminal.get("modelUsage") or {}}
            return Reply(terminal["result"], session, True, None, usage, tokens_of(usage),
                         canonical(usage) or entry.meta.get("model"))
        diagnostic = "cli_error" if terminal else "no_terminal_result"
        return Reply("".join(streamed), session, False, diagnostic, {},
                     model=entry.meta.get("model"))

    def price(self, reply: Reply) -> Cost | None:
        """CLI считает стоимость сама — свою таблицу пускаем в ход, только если не посчитала."""
        reported = (reply.usage or {}).get("total_cost_usd")
        if isinstance(reported, int | float) and not isinstance(reported, bool):
            return Cost(Decimal(str(reported)), "USD", "cli")
        rates = PRICES.get(str(reply.model or ""))
        return estimate(reply.tokens, rates) if rates and reply.tokens else None

    def limits(self, profile: Profile, *, session: str | None = None) -> Limits | None:
        try:
            payload = self.usage(profile)
        except Exception:
            # Управляющий протокол помечен как экспериментальный: его отказ — не авария.
            return self.printed_usage(profile)
        windows = control_windows(payload.get("rate_limits") or {})
        if not payload.get("rate_limits_available") or not windows:
            return self.printed_usage(profile)
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

    def printed_usage(self, profile: Profile) -> Limits | None:
        """Запасной путь: то же, что показывает /usage человеку."""
        windows = usage_windows(last_json(capture(self.usage_command(profile))).get("result", ""))
        if not windows:
            return None
        # Время сброса приходит текстом без года — точную дату отсюда не соберёшь.
        return Limits(self.name, profile.name, windows, datetime.now(UTC), "usage", True)

    def usage_command(self, profile: Profile) -> Command:
        return Command((self.executable, "-p", "--output-format", "json", "/usage"),
                       self.environment(profile), profile.home, timeout=120)
