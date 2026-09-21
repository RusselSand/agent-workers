import json
from datetime import datetime, timedelta

import pytest

from agent_workers.base import Entry
from agent_workers.providers.claude import (
    ClaudeAdapter,
    control_windows,
    usage_windows,
    window_name,
)


def adapter():
    return ClaudeAdapter(executable=__file__)  # файл существует: до запуска дело не доходит


def test_control_answer_becomes_windows(sample):
    payload = json.loads(sample("claude-get-usage.json"))
    windows = control_windows(payload["rate_limits"])
    assert [w.name for w in windows] == ["session", "weekly_all", "weekly_scoped:fable"]
    assert windows[0].used_percent == 10 and windows[0].window == timedelta(hours=5)
    # В отличие от текстового /usage, здесь приходит точное время сброса.
    assert windows[1].resets_at == datetime.fromisoformat("2026-09-26T17:00:00.275587+00:00")


def test_old_answer_without_the_list_still_parses(sample):
    payload = json.loads(sample("claude-get-usage-legacy.json"))
    windows = control_windows(payload["rate_limits"])
    assert [w.name for w in windows] == ["five_hour", "seven_day", "model:fable"]
    assert [w.used_percent for w in windows] == [10, 23, 23]


def test_unknown_bucket_does_not_need_a_new_key(sample):
    """Новое окно приезжает строкой в limits[] — разбор не надо дописывать."""
    payload = json.loads(sample("claude-get-usage.json"))
    payload["rate_limits"]["limits"].append(
        {"kind": "monthly_all", "group": "monthly", "percent": 7, "resets_at": None})
    assert control_windows(payload["rate_limits"])[-1].name == "monthly_all"


def test_impossible_percentage_is_refused(sample):
    payload = json.loads(sample("claude-get-usage.json"))
    payload["rate_limits"]["limits"][0]["percent"] = 250
    with pytest.raises(ValueError):
        control_windows(payload["rate_limits"])


def test_printed_usage_is_the_fallback(sample):
    windows = usage_windows(json.loads(sample("claude-usage.json"))["result"])
    assert [w.name for w in windows] == ["session", "week", "week:fable"]
    # У текстового ответа нет года — точную дату отсюда не собрать, остаётся подсказка.
    assert windows[0].resets_at is None and windows[0].resets_hint.startswith("Sep 21")


def test_window_names_are_normalised():
    assert window_name("session") == "session"
    assert window_name("week (all models)") == "week"
    assert window_name("week (Fable)") == "week:fable"


def test_terminal_result_is_the_answer(sample, profile, tmp_path):
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", sample("claude-stream.jsonl"))
    reply = adapter().reply(entry, profile)
    assert (reply.text, reply.complete) == ("ok", True)
    assert reply.session_id == "3576b5f7-0620-400a-a40e-d8d496123516"
    assert reply.usage["output_tokens"] == 42


def test_interrupted_capture_keeps_partial_text(sample, profile, tmp_path):
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", sample("claude-stream-interrupted.jsonl"))
    reply = adapter().reply(entry, profile)
    assert reply.complete is False
    assert reply.diagnostic == "no_terminal_result"
    assert reply.text == "частичный от"  # обрезанный хвост не съедает полученное


def test_multiline_auth_status_is_accepted(sample):
    # `claude auth status` печатает JSON с отступами и может предварить его строкой шума.
    adapter().verify(sample("claude-auth-status.txt"))


def test_login_without_subscription_is_refused():
    with pytest.raises(RuntimeError):
        adapter().verify('{"loggedIn": true, "authMethod": "apiKey"}')
