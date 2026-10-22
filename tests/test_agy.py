import json

import pytest

from agent_workers.base import Entry
from agent_workers.providers.agy import (
    EFFORTS,
    READ_ONLY,
    AgyAdapter,
    agents_dir,
    data_dir,
)

from .stubs import cli_stub

CONVERSATION = "e2ab43e6-1ae0-4892-9a37-77f1e9deae26"


def adapter(**options):
    return AgyAdapter(executable=cli_stub(), **options)  # до запуска дело не доходит


def agent_text(profile, argv):
    name = argv[argv.index("--agent") + 1]
    return (agents_dir(profile.home) / f"{name}.md").read_text(encoding="utf-8")


def test_system_becomes_the_whole_prompt_of_our_own_agent(profile, tmp_path):
    """Встроенный промпт и инструменты agy снимаются только своим агентом."""
    entry = Entry(tmp_path, "run")
    argv = adapter().ask(entry, {"user": "привет", "system": "Ты фильтр."}, profile).argv
    text = agent_text(profile, argv)
    header, body = text.split("\n---\n", 1)
    assert body == "Ты фильтр."
    assert "excludeDefaultComponents: true" in header
    assert "inheritCustomizations: false" in header
    assert "tools: []" in header


def test_user_text_goes_through_stdin_not_the_command_line(profile, tmp_path):
    """Длинный текст в -p упёрся бы в длину командной строки."""
    entry = Entry(tmp_path, "run")
    command = adapter().ask(entry, {"user": "строка один\nстрока два"}, profile)
    argv = command.argv
    assert argv[argv.index("-p") + 1] == ""
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--disable-slash-commands" in argv
    message = json.loads(command.stdin.read_text(encoding="utf-8"))
    assert message == {"event": "user", "message": {"content": "строка один\nстрока два"}}
    assert command.cwd == entry.folder


def test_one_system_is_one_agent_file(profile, tmp_path):
    """Имя агента — от содержимого: ходы с тем же system не плодят файлов."""
    first = adapter().ask(Entry(tmp_path, "a"), {"user": "x", "system": "один"}, profile).argv
    again = adapter().ask(Entry(tmp_path, "b"), {"user": "y", "system": "один"}, profile).argv
    other = adapter().ask(Entry(tmp_path, "c"), {"user": "x", "system": "другой"}, profile).argv
    name = first[first.index("--agent") + 1]
    assert again[again.index("--agent") + 1] == name
    assert other[other.index("--agent") + 1] != name
    assert sorted(path.name for path in agents_dir(profile.home).iterdir()) == sorted(
        {f"{name}.md", other[other.index("--agent") + 1] + ".md"})


def test_a_workspace_turn_can_only_read(profile, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    command = adapter().ask(Entry(tmp_path, "run"), {"user": "x", "workspace": str(repo)}, profile)
    assert f"tools: [{', '.join(READ_ONLY)}]" in agent_text(profile, command.argv)
    assert command.cwd == repo.resolve()


def test_the_profile_is_the_home_of_the_cli(profile):
    env = adapter().environment(profile)
    assert env["HOME"] == str(profile.home)
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


def test_model_and_effort_go_only_when_set(profile, tmp_path):
    plain = adapter().ask(Entry(tmp_path, "a"), {"user": "x"}, profile).argv
    assert "--model" not in plain
    assert "--effort" not in plain
    tuned = adapter(model="gemini-3.1-pro-high", effort="high").ask(
        Entry(tmp_path, "b"), {"user": "x"}, profile).argv
    assert tuned[tuned.index("--model") + 1] == "gemini-3.1-pro-high"
    assert tuned[tuned.index("--effort") + 1] == "high"
    asked = adapter().ask(Entry(tmp_path, "c"), {"user": "x", "model": "gemini-3.8-flash-low"}, profile).argv
    assert asked[asked.index("--model") + 1] == "gemini-3.8-flash-low"


def test_unknown_effort_level_is_refused_when_the_adapter_is_built():
    with pytest.raises(ValueError, match="effort"):
        adapter(effort="extreme")
    assert "max" in EFFORTS


def test_completed_turn_is_an_answer(sample, profile, tmp_path):
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", sample("agy-stream.jsonl"))
    reply = adapter().reply(entry, profile)
    assert reply.complete is True
    assert reply.text == "готовый ответ\n"
    assert reply.session_id == CONVERSATION
    # Прочитанное из кеша — часть входа: вычитаем, иначе оно оплачивалось бы дважды.
    # Размышления уже входят в output_tokens: прибавлять их второй раз нельзя.
    tokens = reply.tokens
    assert (tokens.input, tokens.cached_input, tokens.output, tokens.reasoning) == (20, 100, 46, 40)


def test_interrupted_turn_keeps_partial_text(sample, profile, tmp_path):
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", "\n".join(sample("agy-stream.jsonl").splitlines()[:3]))
    reply = adapter().reply(entry, profile)
    assert reply.complete is False
    assert reply.diagnostic == "no_terminal_result"
    assert reply.text == "готовый "


def test_failed_turn_keeps_the_cli_error(profile, tmp_path):
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", json.dumps({"event": "result", "result": {
        "conversation_id": CONVERSATION, "status": "ERROR", "error": "quota exhausted",
        "usage": {"input_tokens": 5}}}))
    reply = adapter().reply(entry, profile)
    assert reply.complete is False
    assert reply.diagnostic == "cli_error: quota exhausted"
    assert reply.tokens.input == 5


def test_the_conversation_is_removed_after_the_turn(sample, profile, tmp_path):
    """agy сохраняет беседу всегда; продолжать её некому, а каталог учётки рос бы."""
    data = data_dir(profile.home)
    files = [data / "conversations" / f"{CONVERSATION}.db",
             data / "presence" / f"{CONVERSATION}.lock",
             data / "annotations" / f"{CONVERSATION}.pbtxt",
             data / "brain" / CONVERSATION / "scratch" / "note"]
    keep = data / "conversation_summaries.db"
    for path in [*files, keep]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", sample("agy-stream.jsonl"))
    adapter().reply(entry, profile)
    assert not any(path.exists() for path in files)
    assert not (data / "brain" / CONVERSATION).exists()
    assert keep.exists()


def test_a_strange_conversation_id_removes_nothing(profile, tmp_path):
    """Идентификатор приходит из вывода CLI и становится путём: только UUID."""
    victim = data_dir(profile.home) / "brain" / "victim"
    victim.mkdir(parents=True)
    entry = Entry(tmp_path, "run")
    entry.write("stdout.jsonl", json.dumps({"event": "result", "result": {
        "conversation_id": "../brain/victim", "status": "SUCCESS", "response": "ok"}}))
    adapter().reply(entry, profile)
    assert victim.exists()


def test_signed_out_cli_is_not_a_login():
    claude_like = "Fetching available models...\nError: Please sign in to view available models."
    with pytest.raises(RuntimeError):
        adapter().verify(claude_like)
    adapter().verify("Fetching available models...\ngemini-3.1-pro-high\tGemini 3.1 Pro (High)\n")


def test_quota_and_price_are_unknown_for_now(profile):
    assert adapter().limits(profile) is None
    assert adapter().price(None) is None
