"""Команды работают без установленных CLI: ни один тест ничего не тратит."""
import io
import sys
from pathlib import Path

import pytest

from agent_workers import cli


@pytest.fixture(autouse=True)
def connection(tmp_path, monkeypatch):
    for name in ("AGENT_CLAUDE_BINARY", "AGENT_CODEX_BINARY"):
        monkeypatch.setenv(name, __file__)
    monkeypatch.setenv("AGENT_PROVIDER", "claude")
    monkeypatch.setenv("AGENT_HOME", str(tmp_path / "учётка"))
    monkeypatch.chdir(tmp_path)


def folder_of(printed: str) -> Path:
    """Первая строка dry-run называет каталог хода — в нём лежит всё, что уйдёт в CLI."""
    return Path(printed.splitlines()[0].split(": ", 1)[1])


def test_every_command_says_where_it_connects(capsys, tmp_path):
    cli.main(["run", "--dry-run", "привет"])
    reported = capsys.readouterr().err
    assert "подключение: claude" in reported
    assert str(tmp_path / "учётка") in reported


def test_dry_run_shows_the_command_and_spends_nothing(capsys):
    assert cli.main(["run", "--dry-run", "привет"]) == 0
    printed = capsys.readouterr().out
    assert "--output-format stream-json" in printed
    assert "Ничего не запущено и не потрачено." in printed


def test_flags_outrank_the_environment(capsys):
    cli.main(["run", "--provider", "codex", "--model", "gpt-5.6-luna", "--dry-run", "привет"])
    out, err = capsys.readouterr()
    assert "-m gpt-5.6-luna" in out
    assert "подключение: codex" in err


def test_another_account_is_another_folder(capsys, tmp_path):
    cli.main(["run", "--home", str(tmp_path / "второй"), "--dry-run", "привет"])
    assert str(tmp_path / "второй") in capsys.readouterr().err


def test_system_file_reaches_the_request(tmp_path, capsys):
    instruction = tmp_path / "system.md"
    instruction.write_text("Отвечай коротко", encoding="utf-8")
    cli.main(["run", "--system-file", str(instruction), "--dry-run", "привет"])
    written = folder_of(capsys.readouterr().out) / "invocation" / "system.md"
    assert written.read_text(encoding="utf-8") == "Отвечай коротко"


def test_prompt_can_come_from_stdin(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("задание из файла"))
    cli.main(["run", "--dry-run", "-"])
    written = folder_of(capsys.readouterr().out) / "invocation" / "input.txt"
    assert written.read_text(encoding="utf-8") == "задание из файла"


def test_run_refuses_before_spending_when_login_is_missing(capsys):
    # Поддельный «исполняемый файл» не отвечает статусом входа — значит, хода не будет.
    assert cli.main(["run", "привет"]) == 2
    assert "login" in capsys.readouterr().err


def test_status_says_how_to_fix_a_missing_login(capsys):
    assert cli.main(["status"]) == 2
    assert "вход не выполнен" in capsys.readouterr().err


def test_unnamed_connection_is_reported_not_guessed(capsys, monkeypatch):
    monkeypatch.delenv("AGENT_PROVIDER")
    assert cli.main(["status"]) == 2
    assert "AGENT_PROVIDER" in capsys.readouterr().err


def test_unknown_provider_is_rejected_by_the_parser():
    with pytest.raises(SystemExit):
        cli.main(["run", "--provider", "gemini", "привет"])


def test_retry_is_reachable_from_the_command_line():
    options = cli.parser().parse_args(["run", "--retry", "привет"])
    assert options.retry is True and options.prompt == "привет"


def ready_entry(tmp_path, text="ответ"):
    """Готовый результат в лотке, как его оставил бы завершившийся ход."""
    from agent_workers.base import Entry
    entry = Entry(tmp_path / "учётка" / "runs", "ключ-хода")
    entry.write("stdout.jsonl", text)
    entry.update(state="answered", model="claude-opus-5")
    return entry


def test_pending_lists_what_nobody_collected(tmp_path, capsys):
    ready_entry(tmp_path)
    assert cli.main(["pending"]) == 0
    printed = capsys.readouterr().out
    assert "ключ-хода" in printed and "answered" in printed


def test_collect_removes_the_folder(tmp_path, capsys):
    entry = ready_entry(tmp_path)
    assert cli.main(["collect", "--all"]) == 0
    assert not entry.folder.exists()
    assert "забрано: 1" in capsys.readouterr().out


def test_empty_outbox_says_so(capsys):
    assert cli.main(["pending"]) == 0
    assert "лоток пуст" in capsys.readouterr().out


def test_collect_says_no_to_a_dangerous_key(capsys):
    assert cli.main(["collect", ".."]) == 2
    assert "ключ" in capsys.readouterr().err


def test_collect_all_skips_what_is_busy(tmp_path, capsys):
    from agent_workers.base import Entry

    ready_entry(tmp_path)
    busy = Entry(tmp_path / "учётка" / "runs", "занятый")
    busy.claim()
    busy.update(state="answered")
    try:
        assert cli.main(["collect", "--all"]) == 0
    finally:
        busy.release()
    printed = capsys.readouterr().out
    assert "забрано: 1" in printed and "занято: 1" in printed
    assert busy.folder.exists()


def test_dry_run_does_not_touch_a_running_entry(tmp_path, capsys):
    """Файлы задания у идущего хода читает живая CLI — переписывать их нельзя."""
    from agent_workers import build
    from agent_workers.base import Entry
    from agent_workers.config import Settings

    worker = build(Settings.load(tmp_path))
    owner = Entry(worker.root, worker.key_for({"user": "привет", "model": worker.adapter.model}))
    owner.claim()
    owner.write("invocation/input.txt", "задание идущего хода")
    try:
        assert cli.main(["run", "--dry-run", "привет"]) == 5
    finally:
        owner.release()
    assert owner.read("invocation/input.txt") == "задание идущего хода"
    assert "уже идёт" in capsys.readouterr().err


def test_outbox_commands_work_without_the_provider_cli(tmp_path, monkeypatch, capsys):
    """CLI может быть снесена или сломана обновлением — забрать готовое всё равно нужно."""
    ready_entry(tmp_path)
    for name in ("AGENT_CLAUDE_BINARY", "AGENT_CODEX_BINARY"):
        monkeypatch.setenv(name, str(tmp_path / "такого-файла-нет"))

    assert cli.main(["pending"]) == 0
    assert "ключ-хода" in capsys.readouterr().out
    assert cli.main(["collect", "--all"]) == 0
    assert cli.main(["status"]) == 2          # а вот для статуса CLI уже нужна


def test_empty_reply_is_explained_with_the_cli_diagnostic():
    from agent_workers.base import Reply

    failed = {"reason": None, "reply": Reply("", diagnostic="cli_error: Rate limit reached")}
    assert cli.explain(failed) == "cli_error: Rate limit reached"
    assert cli.explain({"reason": "прошлая попытка не завершилась", "reply": None}) == (
        "прошлая попытка не завершилась")
    assert cli.explain({"reason": None, "reply": None}) == "ход не дал ответа"


def test_report_tells_how_to_continue_the_conversation(tmp_path, capsys):
    from agent_workers.base import Entry, Reply

    entry = Entry(tmp_path, "ход")
    result = {"reply": Reply("ок", session_id="сессия-42", complete=True), "entry": entry,
              "tokens": None, "cost": None, "before": None, "after": None, "spent": {},
              "reason": None}
    cli.report(result)
    assert "--session сессия-42" in capsys.readouterr().err


def test_invalid_request_is_a_concise_message_not_a_traceback(monkeypatch, capsys):
    from agent_workers.base import worker as worker_module

    def rejecting(self, request, **kwargs):
        raise ValueError("Неверный идентификатор сессии Codex")

    monkeypatch.setattr(worker_module.Worker, "run", rejecting)
    assert cli.main(["run", "--session", "; rm -rf /", "привет"]) == 2
    err = capsys.readouterr().err
    assert "запрос отклонён" in err and "Traceback" not in err and "login" not in err

