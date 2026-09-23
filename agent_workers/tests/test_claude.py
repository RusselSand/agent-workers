import json
from datetime import datetime, timedelta

import pytest

from agent_workers.base import Entry
from agent_workers.providers.claude import (
    ClaudeAdapter,
    concerns,
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
    assert windows[0].used_percent == 10
    assert windows[0].duration == timedelta(hours=5)
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
    assert windows[0].resets_at is None
    assert windows[0].resets_hint.startswith("Sep 21")


def test_display_name_of_our_model_is_our_window(sample):
    """«Claude Opus 5» из ответа — это claude-opus-5 из настроек: его окно нас касается."""
    payload = json.loads(sample("claude-get-usage.json"))
    scoped = next(row for row in payload["rate_limits"]["limits"] if row.get("scope"))
    scoped["scope"]["model"] = {"display_name": "Claude Opus 5"}
    names = [w.name for w in control_windows(payload["rate_limits"], "claude-opus-5")]
    assert "weekly_scoped:claude_opus_5" in names

    printed = "Current week (Claude Opus 5): 100% used · resets Sep 26, 7pm (Europe/Madrid)"
    week = usage_windows(printed, "claude-opus-5")
    assert [w.used_percent for w in week] == [100.0]
    assert week[0].resets_hint == "Sep 26, 7pm (Europe/Madrid)"


def test_model_names_match_by_whole_words():
    assert concerns("Opus 4.8", "claude-opus-4-8")
    assert concerns("Fable", "claude-fable-5-1")
    assert not concerns("Opus 4", "claude-opus-45")         # не часть другого числа
    assert not concerns("Sonnet", "claude-opus-5")
    assert concerns("???", "claude-opus-5")                  # непонятное — считаем нашим


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
    claude = adapter()
    with pytest.raises(RuntimeError):
        claude.verify('{"loggedIn": true, "authMethod": "apiKey"}')


def test_turn_leaves_no_session_behind(profile, tmp_path):
    """Ход — вопрос и ответ: беседу никто не продолжит, каталогу учётки незачем расти."""
    entry = Entry(tmp_path, "run")
    command = adapter().ask(entry, {"user": "привет", "session": "старая"}, profile)
    assert "--no-session-persistence" in command.argv
    assert "--resume" not in command.argv


def test_windows_of_other_models_do_not_limit_us(sample):
    payload = json.loads(sample("claude-get-usage.json"))["rate_limits"]
    assert [w.name for w in control_windows(payload, "claude-opus-5")] == ["session", "weekly_all"]
    assert [w.name for w in control_windows(payload, "claude-fable-5-1")] == [
        "session", "weekly_all", "weekly_scoped:fable"]
    assert len(control_windows(payload)) == 3      # модель не названа — не фильтруем


def test_error_result_keeps_its_text_and_paid_tokens(profile, tmp_path):
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", json.dumps({
        "type": "result", "is_error": True, "result": "Rate limit reached for claude-opus-5",
        "session_id": "s", "usage": {"input_tokens": 500, "output_tokens": 0},
        "total_cost_usd": 0.0025}))
    reply = adapter().reply(entry, profile)
    assert reply.complete is False
    assert "Rate limit reached" in reply.diagnostic
    assert reply.tokens.input == 500
    assert reply.usage["total_cost_usd"] == 0.0025


def test_successful_turn_without_text_is_not_an_answer(profile, tmp_path):
    """Иначе координатор получил бы пустой ответ как выполненную задачу."""
    empty = {"type": "result", "is_error": False, "result": "", "session_id": "s",
             "usage": {"input_tokens": 5, "output_tokens": 0}}
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", json.dumps(empty))
    reply = adapter().reply(entry, profile)
    assert reply.complete is False
    assert reply.diagnostic == "empty_result"

    # Текст пришёл сообщениями, а итог его не повторил — ответ берём из потока.
    streamed = {"type": "assistant", "message": {"content": [{"type": "text", "text": "ок"}]}}
    entry.write("stdout.jsonl", json.dumps(streamed) + "\n" + json.dumps(empty))
    reply = adapter().reply(entry, profile)
    assert (reply.text, reply.complete) == ("ок", True)


def test_missing_system_prompt_is_empty_not_the_word_none(profile, tmp_path):
    entry = Entry(tmp_path, "run")
    adapter().ask(entry, {"user": "привет", "system": None}, profile)
    assert entry.read("invocation/system.md") == ""


def test_partial_message_stream_is_not_requested(profile, tmp_path):
    entry = Entry(tmp_path, "run")
    argv = adapter().ask(entry, {"user": "привет"}, profile).argv
    assert "--include-partial-messages" not in argv


def test_fallback_usage_probe_does_not_leave_sessions_behind(profile):
    argv = adapter().usage_command(profile).argv
    assert "--no-session-persistence" in argv
    assert "/usage" in argv

