import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agent_workers.base import Entry, Profile
from agent_workers.providers.codex import (
    CodexAdapter,
    credits_of,
    rate_limits,
    rollout_windows,
    rpc_windows,
)
from agent_workers.providers.codex import (
    tokens_of as codex_tokens,
)


def adapter():
    return CodexAdapter(executable=__file__)


def test_quota_is_read_from_the_bucket_of_our_model(sample):
    """Общая корзина на 100% не должна запрещать ход модели, у которой своя пуста."""
    result = json.loads(sample("codex-app-server.json"))

    luna = rpc_windows(result, "gpt-5.6-luna")     # у неё своя корзина
    assert [(w.name, w.used_percent) for w in luna] == [("base_model_inference:primary", 0.0)]

    sol = rpc_windows(result, "gpt-5.6-sol")       # своей нет — считаем по общей
    assert [(w.name, w.used_percent) for w in sol] == [("codex:primary", 100.0)]
    assert sol[0].window == timedelta(days=7)
    assert sol[0].resets_at == datetime.fromtimestamp(1790258347, UTC)
    assert credits_of(result["rateLimits"]) == Decimal("184.5226000000")


def test_credits_come_from_the_same_bucket_as_the_windows(sample, profile, monkeypatch):
    """Остаток общей корзины не платит за корзину модели, у которой кредитов нет."""
    from agent_workers.base import Guard, LimitPolicy

    result = json.loads(sample("codex-app-server.json"))
    # Корзина Luna выбрана целиком, у общей корзины — остаток кредитов.
    result["rateLimitsByLimitId"]["base_model_inference"]["primary"]["usedPercent"] = 100
    monkeypatch.setattr(CodexAdapter, "rate_limits", lambda self, profile: result)
    guard = Guard(adapter(), profile, LimitPolicy())

    luna = adapter().limits(profile, model="gpt-5.6-luna")
    assert luna.credits is None
    assert not luna.spendable
    assert guard.blocked(luna) == "Окно выбрано на 100%"       # кредиты чужой корзины не в счёт

    sol = adapter().limits(profile, model="gpt-5.6-sol")
    assert sol.credits == Decimal("184.5226000000")
    assert guard.blocked(sol) is None                         # у общей корзины кредиты свои


def test_buckets_of_other_models_never_limit_us(sample, profile, monkeypatch):
    """Ни своей корзины, ни общей: чужой исчерпанный лимит не повод отказать."""
    from agent_workers.base import Guard, LimitPolicy

    result = json.loads(sample("codex-app-server.json"))
    del result["rateLimitsByLimitId"]["codex"]                       # общей корзины нет
    result["rateLimitsByLimitId"]["base_model_inference"]["primary"]["usedPercent"] = 100
    assert rpc_windows(result, "gpt-5.6-новая") == ()

    monkeypatch.setattr(CodexAdapter, "rate_limits", lambda self, profile: result)
    monkeypatch.setattr(CodexAdapter, "snapshot", lambda self, profile, session=None: None)
    limits = adapter().limits(profile, model="gpt-5.6-новая")
    assert limits is None                                    # лимит неизвестен
    assert Guard(adapter(), profile, LimitPolicy()).blocked(limits) is None   # решает политика


def test_single_bucket_answer_is_supported(sample):
    result = json.loads(sample("codex-app-server.json"))
    result.pop("rateLimitsByLimitId")
    assert [w.name for w in rpc_windows(result, "gpt-5.6-sol")] == ["codex:primary"]


def test_rollout_snapshot_is_the_fallback(sample):
    raw = rate_limits(sample("codex-rollout.jsonl"))
    windows = rollout_windows(raw)
    assert (windows[0].name, windows[0].used_percent) == ("codex:primary", 100.0)
    assert windows[0].resets_at == datetime.fromtimestamp(1790258346, UTC)
    assert credits_of(raw) == Decimal("186.4078000000")
    assert raw["plan_type"] == "prolite"


def test_null_snapshot_is_not_a_measurement(sample):
    # Первая строка образца несёт rate_limits: null — её нельзя принимать за замер.
    assert rate_limits('{"payload": {"type": "token_count", "rate_limits": null}}') is None


def test_completed_turn_is_the_answer(sample, profile, tmp_path):
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", sample("codex-exec.jsonl"))
    reply = adapter().reply(entry, profile)
    assert (reply.text, reply.complete) == ("ok", True)
    assert reply.session_id == "01a0c345-2cd1-70e2-8daa-7193e928f5d9"
    assert reply.usage["input_tokens"] == 18827


def test_summary_file_outranks_streamed_message(sample, profile, tmp_path):
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", sample("codex-exec.jsonl"))
    entry.write("summary.txt", "итоговый ответ")
    assert adapter().reply(entry, profile).text == "итоговый ответ"


def test_completed_turn_without_text_says_so(profile, tmp_path):
    """Ход дошёл до конца, но ответа нет: неудача должна быть названа, а не безымянна."""
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", '{"type": "thread.started", "thread_id": "t"}\n'
                                '{"type": "turn.completed", "usage": {"input_tokens": 5}}')
    reply = adapter().reply(entry, profile)
    assert reply.complete is False
    assert reply.diagnostic == "empty_result"


def test_every_turn_starts_a_new_session(tmp_path, profile):
    """Ход — вопрос и ответ: продолжать чужую беседу не из чего и незачем."""
    command = adapter().ask(Entry(tmp_path, "run"), {"user": "x", "session": "старая"}, profile)
    assert "resume" not in command.argv


def test_snapshot_reads_our_own_session(sample, profile):
    session = "01a0c345-2cd1-70e2-8daa-7193e928f5d9"
    folder = profile.home / "sessions" / "2026" / "09" / "21"
    folder.mkdir(parents=True)
    (folder / f"rollout-2026-09-21T11-21-25-{session}.jsonl").write_text(
        sample("codex-rollout.jsonl"), encoding="utf-8")
    ours = adapter().snapshot(profile, session=session)
    assert ours.exact is True and ours.source == "rollout"
    # Без сессии снимок находится тоже, но считается неточным.
    assert adapter().snapshot(profile).exact is False
    assert adapter().snapshot(profile, session="0" * 36) is None


def resumed_profile(sample, profile):
    session = "01a0c3ba-6ab4-7590-8ac9-33e8fa9dacce"
    folder = profile.home / "sessions" / "2026" / "09" / "21"
    folder.mkdir(parents=True)
    (folder / f"rollout-2026-09-21T13-29-29-{session}.jsonl").write_text(
        sample("codex-rollout-resumed.jsonl"), encoding="utf-8")
    return session


def test_resumed_turn_is_not_charged_for_the_whole_conversation(sample, profile, tmp_path):
    """turn.completed в продолженной сессии отдаёт итог всей беседы, а не расход хода."""
    resumed_profile(sample, profile)
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", sample("codex-exec-resumed.jsonl"))
    # В потоке лежит накопленный итог беседы (27369), в роллауте — расход хода.
    reply = adapter().reply(entry, profile)
    assert reply.tokens.input == 13773
    assert reply.tokens.output == 5


def test_without_the_rollout_we_fall_back_to_the_stream(sample, profile, tmp_path):
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", sample("codex-exec-resumed.jsonl"))
    assert adapter().reply(entry, profile).tokens.input == 27369


def test_cached_part_is_not_charged_twice():
    """Цифры настоящие: ход с попаданием в кеш внутри продолженной беседы."""
    tokens = codex_tokens({"input_tokens": 13948, "cached_input_tokens": 13056,
                           "output_tokens": 5})
    assert (tokens.input, tokens.cached_input) == (892, 13056)
    assert tokens.total == 13953   # весь запрос плюс ответ, без двойного счёта


def test_snapshot_keeps_the_time_it_was_actually_taken(sample, profile):
    """Вчерашний снимок, помеченный сегодняшним временем, разрешил бы ход по старым цифрам."""
    session = resumed_profile(sample, profile)
    limits = adapter().snapshot(profile, session=session)
    assert limits.measured_at == datetime.fromisoformat("2026-09-21T13:31:12+00:00")
    assert limits.measured_at != datetime.now(UTC)


def test_stale_snapshot_is_treated_as_unknown(sample, profile):
    from agent_workers.base import Guard, LimitPolicy

    folder = profile.home / "sessions" / "2026" / "09" / "21"
    folder.mkdir(parents=True)
    (folder / "rollout-2026-09-21T11-21-25-old.jsonl").write_text(
        sample("codex-rollout.jsonl"), encoding="utf-8")
    limits = adapter().snapshot(profile)          # без сессии — значит неточный
    guard = Guard(adapter(), profile, LimitPolicy(on_unknown="refuse"))
    assert limits.exact is False
    assert guard.blocked(limits) == "Лимит неизвестен"


def test_proxy_and_certificates_reach_the_subprocess(monkeypatch, profile):
    from agent_workers.providers import common

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.local:3128")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/etc/ssl/corp.pem")
    monkeypatch.setenv("AGENT_SECRET", "не должно утечь")
    env = common.environment(profile, "CODEX_HOME")
    assert env["HTTPS_PROXY"] == "http://proxy.local:3128"
    assert env["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/corp.pem"
    assert "AGENT_SECRET" not in env and env["CODEX_HOME"] == str(profile.home)


def test_account_folder_is_created_private(tmp_path):
    """Там токены входа: соседу по машине туда незачем."""
    from agent_workers.providers import common

    if os.name == "nt":
        pytest.skip("на Windows права выставляются иначе")
    profile = Profile("x", tmp_path / "учётка")
    common.environment(profile, "CODEX_HOME")
    assert profile.home.stat().st_mode & 0o077 == 0


def test_unlimited_credits_without_a_balance_are_still_credits(sample, profile):
    from agent_workers.providers.codex import unlimited

    result = json.loads(sample("codex-app-server.json"))
    result["rateLimits"]["credits"] = {"hasCredits": True, "unlimited": True, "balance": None}
    assert credits_of(result["rateLimits"]) is None
    assert unlimited(result["rateLimits"]) is True

