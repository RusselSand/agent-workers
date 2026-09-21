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
