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

from .stubs import cli_stub


def adapter():
    return CodexAdapter(executable=cli_stub())


def test_quota_is_read_from_the_bucket_of_our_model(sample):
    """Общая корзина на 100% не должна запрещать ход модели, у которой своя пуста."""
    result = json.loads(sample("codex-app-server.json"))

    luna = rpc_windows(result, "gpt-5.6-luna")     # у неё своя корзина
    assert [(w.name, w.used_percent) for w in luna] == [("base_model_inference:primary", 0.0)]

    sol = rpc_windows(result, "gpt-5.6-sol")       # своей нет — считаем по общей
    assert [(w.name, w.used_percent) for w in sol] == [("codex:primary", 100.0)]
    assert sol[0].duration == timedelta(days=7)
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
    monkeypatch.setattr(CodexAdapter, "snapshot", lambda self, profile, **_: None)
    limits = adapter().limits(profile, model="gpt-5.6-новая")
    assert limits is None                                    # лимит неизвестен
    assert Guard(adapter(), profile, LimitPolicy()).blocked(limits) is None   # решает политика


def rollout_of(folder, name, model, bucket, percent):
    """Роллаут одной сессии: ход модели и снимок лимитов её корзины после него."""
    turn = {"timestamp": "2026-09-23T10:00:00Z", "type": "turn_context",
            "payload": {"model": model}}
    count = {"timestamp": "2026-09-23T10:00:05Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": None, "rate_limits": {
                 "limit_id": bucket, "primary": {"used_percent": percent, "window_minutes": 10080,
                                                 "resets_at": 1790588781},
                 "secondary": None, "credits": None, "plan_type": "prolite"}}}
    (folder / name).write_text(json.dumps(turn) + "\n" + json.dumps(count) + "\n",
                               encoding="utf-8")


def test_fallback_snapshot_comes_from_a_turn_of_our_model(profile, monkeypatch):
    """app-server недоступен: снимок чужой модели не решает за наш ход."""
    from agent_workers.base import Guard, LimitPolicy

    folder = profile.home / "sessions" / "2026" / "09" / "23"
    folder.mkdir(parents=True)
    rollout_of(folder, "rollout-2026-09-23T10-00-00-luna.jsonl",       # свежий, Luna выбрана
               "gpt-5.6-luna", "base_model_inference", 100)
    rollout_of(folder, "rollout-2026-09-22T10-00-00-sol.jsonl",        # постарше, у Sol запас
               "gpt-5.6-sol", "codex", 10)

    def unavailable(self, profile):
        raise RuntimeError("app-server не отвечает")

    monkeypatch.setattr(CodexAdapter, "rate_limits", unavailable)
    guard = Guard(adapter(), profile, LimitPolicy(stale_after=timedelta(days=365 * 10)))

    sol = adapter().limits(profile, model="gpt-5.6-sol")
    assert [(w.name, w.used_percent) for w in sol.windows] == [("codex:primary", 10.0)]
    assert guard.blocked(sol) is None                  # исчерпанная Luna Sol не держит

    luna = adapter().limits(profile, model="gpt-5.6-luna")
    assert guard.blocked(luna) == "Окно выбрано на 100%"
    assert adapter().limits(profile, model="gpt-5.6-terra") is None   # своего снимка нет


def test_fallback_looks_past_many_rollouts_of_other_models(profile, monkeypatch):
    """Сессий других моделей может быть сколько угодно: ищем, пока снимок ещё свежий."""
    import time

    folder = profile.home / "sessions" / "2026" / "09" / "23"
    folder.mkdir(parents=True)
    now = time.time()
    rollout_of(folder, "rollout-2026-09-23T09-00-00-sol.jsonl", "gpt-5.6-sol", "codex", 100)
    os.utime(folder / "rollout-2026-09-23T09-00-00-sol.jsonl", (now - 600, now - 600))
    for n in range(25):                                   # свежее, но чужая модель
        name = f"rollout-2026-09-23T10-{n:02d}-00-luna.jsonl"
        rollout_of(folder, name, "gpt-5.6-luna", "base_model_inference", 0)
        os.utime(folder / name, (now - n, now - n))

    def unavailable(self, profile):
        raise RuntimeError("app-server не отвечает")

    monkeypatch.setattr(CodexAdapter, "rate_limits", unavailable)
    sol = adapter().limits(profile, model="gpt-5.6-sol")      # не по счёту: 25 чужих не помеха
    assert [(w.name, w.used_percent) for w in sol.windows] == [("codex:primary", 100.0)]
    fresh = adapter().limits(profile, model="gpt-5.6-sol", fresh_within=timedelta(hours=1))
    assert fresh.worst == 100.0
    # Снимок старше, чем политика готова верить, не ищем вовсе — лимит неизвестен.
    assert adapter().limits(profile, model="gpt-5.6-sol",
                            fresh_within=timedelta(minutes=5)) is None


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


def test_unreadable_rollout_does_not_cost_the_answer(sample, profile, tmp_path, monkeypatch):
    """Роллаут нашёлся, но исчез до чтения: ответ остаётся, расход — из потока."""
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", sample("codex-exec.jsonl"))
    monkeypatch.setattr(CodexAdapter, "rollout",
                        lambda self, profile, session=None: tmp_path / "исчезнувший.jsonl")
    reply = adapter().reply(entry, profile)
    assert reply.complete is True
    assert reply.text == "ok"
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


def test_failed_turn_keeps_the_cli_error(profile, tmp_path):
    """turn.failed: координатор должен получить причину, а не безымянный обрыв."""
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", '{"type": "thread.started", "thread_id": "t"}\n'
                                '{"type": "turn.started"}\n'
                                '{"type": "turn.failed", "error": {"message": '
                                '"model gpt-5.6-nova is not available"}}')
    reply = adapter().reply(entry, profile)
    assert reply.complete is False
    assert reply.diagnostic == "cli_error: model gpt-5.6-nova is not available"

    entry.write("stdout.jsonl", '{"type": "error", "message": "stream disconnected"}')
    assert adapter().reply(entry, profile).diagnostic == "cli_error: stream disconnected"


def test_error_on_the_way_does_not_spoil_a_completed_answer(sample, profile, tmp_path):
    """Временная ошибка, а потом ход всё-таки завершился: это ответ, платить заново незачем."""
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", '{"type": "error", "message": "reconnecting"}\n'
                + sample("codex-exec.jsonl"))
    reply = adapter().reply(entry, profile)
    assert reply.complete is True
    assert reply.diagnostic is None


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
    assert ours.exact is True
    assert ours.source == "rollout"
    # Без сессии снимок находится тоже, но считается неточным.
    assert adapter().snapshot(profile).exact is False
    assert adapter().snapshot(profile, session="0" * 36) is None


def test_own_session_is_found_without_listing_the_whole_history(sample, profile,
                                                                monkeypatch):
    """Каждый ход — новая сессия: искать свою по всей истории значит дорожать с каждым ходом."""
    import agent_workers.providers.codex as codex_module

    session = "01a0c345-2cd1-70e2-8daa-7193e928f5d9"
    folder = profile.home / "sessions" / "2026" / "09" / "21"
    folder.mkdir(parents=True)
    (folder / f"rollout-2026-09-21T11-21-25-{session}.jsonl").write_text(
        sample("codex-rollout.jsonl"), encoding="utf-8")

    def everything(home):
        raise AssertionError("обошли всю историю сессий")

    monkeypatch.setattr(codex_module, "rollouts", everything)
    assert adapter().snapshot(profile, session=session).exact is True


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
    assert "AGENT_SECRET" not in env
    assert env["CODEX_HOME"] == str(profile.home)


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



def test_a_workspace_is_the_root_of_the_turn_and_read_only(tmp_path, profile):
    """Даже если в настройках песочница пишущая: чужой каталог только читают."""
    repo = tmp_path / "repo"
    repo.mkdir()
    writing = CodexAdapter(executable=cli_stub(), sandbox="workspace-write")
    command = writing.ask(Entry(tmp_path, "run"), {"user": "x", "workspace": str(repo)}, profile)
    argv = command.argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("-C") + 1] == str(repo.resolve())
    assert command.cwd == repo.resolve()


def test_without_a_workspace_the_sandbox_is_as_configured(tmp_path, profile):
    writing = CodexAdapter(executable=cli_stub(), sandbox="workspace-write")
    entry = Entry(tmp_path, "run")
    command = writing.ask(entry, {"user": "x"}, profile)
    assert command.argv[command.argv.index("-s") + 1] == "workspace-write"
    assert "-C" not in command.argv
    assert command.cwd == entry.folder
