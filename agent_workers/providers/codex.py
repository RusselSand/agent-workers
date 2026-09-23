"""Codex CLI под подпиской ChatGPT: сборка запроса, чтение ответа, расход подписки.

Расход читается по протоколу app-server: initialize -> account/read ->
account/rateLimits/read. Ни thread/start, ни turn/start не посылаются, поэтому
модель не вызывается и подписка не тратится. Ответ содержит все корзины лимитов.

Запасной путь — снимок из роллаута сессии: те же цифры приезжают в событии
token_count. Он может быть устаревшим, поэтому помечается неточным.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

from ..base.channel import Channel
from ..base.contract import Command, Cost, Limits, Profile, Rates, Reply, Usage, Window
from . import common
from .common import count, decimal, moment, number

log = logging.getLogger(__name__)

ENV_HOME = "CODEX_HOME"

APP_SERVER = ("app-server", "--listen", "stdio://")
SUBSCRIPTION = ("chatgpt", "chatgptAuthTokens")

SOURCE = "openai, developers.openai.com/api/docs/pricing (снято 2026-09-21)"


def price_of(input_price: str, cached: str, output_price: str) -> Rates:
    """Цена за миллион токенов. Запись в кеш здесь не тарифицируется — отсюда ноль."""
    return Rates(Decimal(input_price), Decimal(output_price), Decimal(cached),
                 Decimal(0), "USD", SOURCE)


PRICES = {
    # У sol цена акционная, объявлена как минимум до 21 ноября 2026 — потом проверить.
    "gpt-5.6-sol": price_of("4", "0.40", "20"),
    "gpt-5.6-terra": price_of("2", "0.20", "12"),
    "gpt-5.6-luna": price_of("0.20", "0.02", "1.20"),
}


def length(minutes) -> timedelta | None:
    return timedelta(minutes=minutes) if number(minutes) and minutes > 0 else None


def bucket_windows(buckets: Mapping, *, used: str, duration: str, resets: str):
    for key, bucket in sorted(buckets.items()):
        for part in ("primary", "secondary"):
            value = bucket.get(part)
            if isinstance(value, dict) and number(value.get(used)):
                yield Window(f"{key}:{part}", float(value[used]),
                             length(value.get(duration)), moment(value.get(resets)))


def credits_of(raw: Mapping) -> Decimal | None:
    return decimal((raw.get("credits") or {}).get("balance"))


def unlimited(raw: Mapping) -> bool:
    return bool((raw.get("credits") or {}).get("unlimited"))


def applicable(buckets: Mapping, model: str) -> dict:
    """Корзины бывают привязаны к модели: чужие ограничения нас не касаются.

    Если какая-то корзина названа под нашу модель, считаем по ней. Иначе берём общие,
    без привязки. Без этого общая корзина на 100% запретила бы ход модели, у которой
    своя корзина пуста. Нет ни своей, ни общей — корзин для нас нет: лимит неизвестен,
    и дальше решает политика, а не исчерпанное окно чужой модели.
    """
    named = {key: bucket for key, bucket in buckets.items()
             if bucket.get("normalModelSlug") == model}
    if named:
        return named
    return {key: bucket for key, bucket in buckets.items()
            if not bucket.get("normalModelSlug")}


def buckets_of(result: Mapping) -> dict:
    """Корзины лимитов из ответа app-server; старый ответ несёт одну, без словаря."""
    buckets = result.get("rateLimitsByLimitId")
    if isinstance(buckets, dict) and buckets:
        return buckets
    single = result.get("rateLimits")
    return {str(single.get("limitId") or "codex"): single} if isinstance(single, dict) else {}


def rpc_windows(result: Mapping, model: str = "") -> tuple[Window, ...]:
    return tuple(bucket_windows(applicable(buckets_of(result), model), used="usedPercent",
                                duration="windowDurationMins", resets="resetsAt"))


def rpc_credits(result: Mapping, model: str = "") -> tuple[Decimal | None, bool]:
    """Кредиты тех же корзин, по которым считаются окна, — остаток и «без ограничений».

    Кредиты сообщаются по корзинам: остаток общей не платит за корзину модели, у которой
    своих нет. Корзина без кредитов — значит, их нет: тратить без уверенности нельзя.
    """
    selected = list(applicable(buckets_of(result), model).values())
    balances = [credits_of(bucket) for bucket in selected]
    endless = bool(selected) and all(unlimited(bucket) for bucket in selected)
    if not balances or None in balances:
        return None, endless
    return min(balances), endless


def rollouts(home: Path):
    """Роллауты новых сессий сверху: имя содержит дату, так что сортировки хватает."""
    return sorted((home / "sessions").rglob("rollout-*.jsonl"), reverse=True)


def snapshot_of(text: str, model: str | None = None) -> tuple[dict | None, datetime | None]:
    """Последний непустой снимок лимитов и время, которым его пометил сам Codex.

    Время обязательно: снимок может быть вчерашним, и выдавать его за сейчас нельзя —
    на возрасте замера держится решение, начинать ли ход.

    model — брать только снимки, снятые во время хода этой модели: у моделей бывают
    свои корзины лимита, и снимок чужой корзины про наш ход ничего не говорит. Модель
    хода роллаут пишет в turn_context, и в одной сессии она может меняться.
    """
    found, when, current = None, None, None
    for line in text.splitlines():
        if '"rate_limits"' not in line and '"turn_context"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        payload = record.get("payload") or {}
        if record.get("type") == "turn_context":
            current = payload.get("model")
        elif (payload.get("type") == "token_count" and payload.get("rate_limits")
              and (model is None or current == model)):
            found, when = payload["rate_limits"], moment(record.get("timestamp"))
    return found, when


def rate_limits(text: str) -> dict | None:
    return snapshot_of(text)[0]


def tokens_of(usage: Mapping) -> Usage:
    """input_tokens здесь — весь запрос, включая прочитанное из кеша.

    Проверено ходом с попаданием в кеш: 13948 входных при 13056 кешированных, тогда как
    первый ход той же беседы стоил 13596 свежих. То есть кеш — часть входа, и вычесть
    его надо, иначе он оплачивается дважды. У Claude ровно наоборот: там input уже
    остаток, поэтому вычитание живёт здесь, а не в базе.
    """
    cached = count(usage.get("cached_input_tokens"))
    return Usage(max(0, count(usage.get("input_tokens")) - cached),
                 cached,
                 count(usage.get("cache_write_input_tokens")),
                 count(usage.get("output_tokens")),
                 count(usage.get("reasoning_output_tokens")))


def last_usage(text: str) -> dict | None:
    """Расход последнего хода. Рядом лежит total_token_usage — итог всей беседы."""
    found = None
    for line in text.splitlines():
        if '"token_count"' not in line:
            continue
        try:
            payload = json.loads(line).get("payload") or {}
        except ValueError:
            continue
        usage = (payload.get("info") or {}).get("last_token_usage")
        if payload.get("type") == "token_count" and isinstance(usage, dict):
            found = usage
    return found


def read_events(text: str) -> tuple[str | None, dict, bool, str]:
    """Сессия, расход, дошёл ли ход до конца и текст ответа — из журнала `exec --json`."""
    session, usage, complete, message = None, {}, False, ""
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        kind = item.get("type")
        if kind == "thread.started":
            session = item.get("thread_id") or session
        elif kind == "turn.completed":
            complete, usage = True, item.get("usage", {})
        elif kind == "item.completed" and item.get("item", {}).get("type") == "agent_message":
            message = item["item"].get("text", "") or message
    return session, usage, complete, message


def verdict(complete: bool, text: str) -> str | None:
    """Почему ход — не ответ: не дошёл до конца или дошёл, но без текста.
    Молчать нельзя: иначе координатор получит неудачу без причины."""
    if not complete:
        return "no_terminal_event"
    return None if text else "empty_result"


def rollout_windows(raw: Mapping) -> tuple[Window, ...]:
    buckets = {str(raw.get("limit_id") or "codex"): raw}
    return tuple(bucket_windows(buckets, used="used_percent",
                                duration="window_minutes", resets="resets_at"))


@dataclass
class CodexAdapter:
    executable: str | None = None
    model: str = "gpt-5.6-sol"
    effort: str = "medium"
    sandbox: str = "read-only"
    name: ClassVar[str] = "codex"

    def __post_init__(self) -> None:
        self.executable = common.find_executable(self.executable, "AGENT_CODEX_BINARY",
                                                 "codex.exe", "codex")

    def environment(self, profile: Profile) -> Mapping[str, str]:
        return common.environment(profile, ENV_HOME)

    def check(self, profile: Profile) -> Command:
        return Command((self.executable, "login", "status"), self.environment(profile),
                       profile.home, timeout=30)

    def login(self, profile: Profile) -> Command:
        return Command((self.executable, "login"), self.environment(profile), profile.home)

    def logout(self, profile: Profile) -> Command:
        return Command((self.executable, "logout"), self.environment(profile), profile.home)

    def verify(self, captured: str) -> None:
        if "Logged in using ChatGPT" not in captured:
            raise RuntimeError("Нужен вход Codex по подписке ChatGPT; API-ключи не используются")

    def ask(self, entry, request: Mapping[str, object], profile: Profile) -> Command:
        system = str(request.get("system") or "")
        user = str(request["user"])
        # У Codex нет отдельного системного канала: инструкция идёт первой в том же тексте.
        prompt = (system + "\n\n" + user).strip() if system else user
        stdin = entry.write("invocation/prompt.txt", prompt)
        model = str(request.get("model") or self.model)
        entry.update(model=model)   # в ответе Codex модели нет, помним её с момента запроса
        argv = (self.executable, "-a", "never", "exec", "-s", self.sandbox,
                "--ignore-user-config", "--skip-git-repo-check",
                "-m", model, "--json",
                "-o", str(entry.folder / "summary.txt"),
                "-c", f'model_reasoning_effort="{self.effort}"', "-")
        return Command(argv, self.environment(profile), entry.folder, stdin)

    def reply(self, entry, profile: Profile) -> Reply:
        session, usage, complete, message = read_events(entry.read("stdout.jsonl"))
        text = entry.read("summary.txt") or message
        done = complete and bool(text)
        usage = self.turn_usage(profile, session) or usage
        return Reply(text, session, done, verdict(complete, text), usage,
                     tokens_of(usage), entry.meta.get("model") or self.model)

    def turn_usage(self, profile: Profile, session: str | None) -> dict | None:
        """Расход хода из роллаута сессии. Это справка к ответу: роллаут пропал или не
        читается — берём расход из потока, а не теряем уже полученный и оплаченный ответ."""
        if not session:
            return None
        try:
            path = self.rollout(profile, session=session)
            return last_usage(path.read_text(encoding="utf-8", errors="ignore")) if path else None
        except OSError as exc:
            log.warning("Роллаут сессии %s не прочитать, расход берём из потока: %s",
                        session, type(exc).__name__)
            return None

    def price(self, reply: Reply) -> Cost | None:
        """Своей цены Codex не сообщает — считаем по таблице."""
        return common.table_price(reply, PRICES)

    def limits(self, profile: Profile, *, session: str | None = None,
               model: str | None = None) -> Limits | None:
        # Модель берём ту, что пойдёт в ход: у неё может быть своя корзина лимита.
        model = model or self.model
        try:
            result = self.rate_limits(profile)
        except Exception:
            # Протокол app-server помечен экспериментальным: его отказ — не авария.
            return self.snapshot(profile, session=session, model=model)
        windows = rpc_windows(result, model)
        if not windows:
            return self.snapshot(profile, session=session, model=model)
        balance, endless = rpc_credits(result, model)
        summary = result.get("rateLimits") or {}
        return Limits(self.name, profile.name, windows, datetime.now(UTC), "app-server", True,
                      plan=summary.get("planType"), credits=balance, credits_unlimited=endless)

    def rate_limits(self, profile: Profile) -> dict:
        """Три запроса подряд и ни одного хода модели: этот процесс не умеет её звать."""
        command = Command((self.executable, *APP_SERVER), self.environment(profile),
                          profile.home, timeout=30)
        with Channel(command) as channel:
            channel.send({"method": "initialize", "id": 1,
                          "params": {"clientInfo": {"name": "agent_workers", "version": "1"}}})
            self.answer(channel, 1)
            channel.send({"method": "initialized"})
            channel.send({"method": "account/read", "id": 2, "params": {"refreshToken": False}})
            account = (self.answer(channel, 2).get("account") or {})
            if account.get("type") not in SUBSCRIPTION:
                raise RuntimeError("Нужен вход Codex по подписке ChatGPT")
            channel.send({"method": "account/rateLimits/read", "id": 3})
            return self.answer(channel, 3)

    @staticmethod
    def answer(channel: Channel, request_id: int) -> dict:
        event = channel.wait(lambda item, wanted=request_id: item.get("id") == wanted)
        if "error" in event:
            raise RuntimeError("Запрос к app-server отклонён")
        return event.get("result") or {}

    def snapshot(self, profile: Profile, *, session: str | None = None,
                 model: str | None = None) -> Limits | None:
        """Запасной путь: те же цифры из роллаута, но, возможно, вчерашние.

        Своя сессия — снимок наш по построению. Без неё берём самый свежий роллаут, где
        шёл ход нашей модели: снимок чужой корзины решал бы за наш ход. Такого нет —
        лимит неизвестен, и решает политика.
        """
        wanted = None if session else model
        path = self.rollout(profile, session=session, model=wanted)
        if path is None:
            return None
        raw, when = snapshot_of(path.read_text(encoding="utf-8", errors="ignore"), wanted)
        windows = rollout_windows(raw) if raw else ()
        if not windows:
            return None
        # Ни своего времени, ни отметки в записи — берём время файла, но не «сейчас».
        measured = when or datetime.fromtimestamp(path.stat().st_mtime, UTC)
        return Limits(self.name, profile.name, windows, measured, "rollout",
                      session is not None, plan=raw.get("plan_type"), credits=credits_of(raw),
                      credits_unlimited=unlimited(raw))

    def rollout(self, profile: Profile, *, session: str | None = None,
                model: str | None = None) -> Path | None:
        if session:
            match = [path for path in rollouts(profile.home) if path.stem.endswith(session)]
            return match[0] if match else None
        for path in rollouts(profile.home)[:20]:
            if snapshot_of(path.read_text(encoding="utf-8", errors="ignore"), model)[0]:
                return path
        return None
