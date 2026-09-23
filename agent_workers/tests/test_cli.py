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


def test_unreadable_system_file_is_a_message_not_a_traceback(tmp_path, capsys):
    assert cli.main(["run", "--system-file", str(tmp_path / "нет-такого.md"), "привет"]) == 2
    assert "файл инструкции" in capsys.readouterr().err

    broken = tmp_path / "не-utf8.md"
    broken.write_bytes(b"\xff\xfe\x00")
    assert cli.main(["run", "--system-file", str(broken), "--dry-run", "привет"]) == 2
    assert "файл инструкции" in capsys.readouterr().err


def test_prompt_can_come_from_stdin(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("задание из файла"))
    cli.main(["run", "--dry-run", "-"])
    written = folder_of(capsys.readouterr().out) / "invocation" / "input.txt"
    assert written.read_text(encoding="utf-8") == "задание из файла"


def test_dry_run_leaves_nothing_in_the_runs_folder(tmp_path, capsys):
    """Пробный ход собирается во временном каталоге: в лотке от него ничего не остаётся."""
    cli.main(["run", "--dry-run", "привет"])
    folder = folder_of(capsys.readouterr().out)
    runs = tmp_path / "учётка" / "runs"
    assert runs not in folder.parents
    assert not runs.exists() or not any(runs.iterdir())


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


def finished_run(tmp_path, monkeypatch, state, text="ответ"):
    """Подменяет ход: папка в лотке и результат, какой вернул бы воркер."""
    from agent_workers.base import Entry, Reply
    from agent_workers.base import worker as worker_module

    made = []

    def fake_run(self, request, *, key, **kwargs):
        entry = Entry(self.root, key)
        entry.write("stdout.jsonl", text)
        entry.update(state=state)
        made.append(entry)
        return {"state": state, "entry": entry, "reason": None,
                "reply": Reply(text, complete=state == "answered"),
                "tokens": None, "cost": None, "before": None, "after": None, "spent": {}}

    monkeypatch.setattr(worker_module.Worker, "run", fake_run)
    return made


def test_answer_is_printed_and_its_folder_removed(tmp_path, monkeypatch, capsys):
    """Ответ напечатан — значит доставлен: лоток ручными ходами не забивается."""
    made = finished_run(tmp_path, monkeypatch, "answered")
    assert cli.main(["run", "привет"]) == 0
    assert capsys.readouterr().out.strip() == "ответ"
    assert not made[0].folder.exists()


def test_every_run_is_a_new_turn(tmp_path, monkeypatch):
    made = finished_run(tmp_path, monkeypatch, "answered")
    cli.main(["run", "привет"])
    cli.main(["run", "привет"])
    assert made[0].folder.name != made[1].folder.name


def test_broken_turn_keeps_its_folder_for_diagnosis(tmp_path, monkeypatch, capsys):
    made = finished_run(tmp_path, monkeypatch, "incomplete", text="")
    assert cli.main(["run", "привет"]) == 4
    assert made[0].folder.exists()
    assert f"collect {made[0].folder.name}" in capsys.readouterr().err


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


def test_collect_all_spares_the_account_when_runs_point_at_it(tmp_path, monkeypatch, capsys):
    """AGENT_RUNS по ошибке равен AGENT_HOME: токены и сессии CLI не должны пропасть."""
    from agent_workers.base import Entry

    home = tmp_path / "учётка"
    monkeypatch.setenv("AGENT_RUNS", str(home))
    (home / "sessions").mkdir(parents=True)
    (home / ".credentials.json").write_text("токен", encoding="utf-8")
    (home / "sessions" / "rollout.jsonl").write_text("{}", encoding="utf-8")
    entry = Entry(home, "ключ-хода")
    entry.update(state="answered")

    assert cli.main(["collect", "--all"]) == 0
    assert "забрано: 1" in capsys.readouterr().out
    assert not entry.folder.exists()
    assert (home / "sessions" / "rollout.jsonl").exists()
    assert (home / ".credentials.json").exists()


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
    assert cli.explain({"reason": "ход уже выполняется", "reply": None}) == (
        "ход уже выполняется")
    assert cli.explain({"reason": None, "reply": None}) == "ход не дал ответа"


def test_invalid_request_is_a_concise_message_not_a_traceback(monkeypatch, capsys):
    from agent_workers.base import worker as worker_module

    def rejecting(self, request, **kwargs):
        raise ValueError("Нет обязательного поля")

    monkeypatch.setattr(worker_module.Worker, "run", rejecting)
    assert cli.main(["run", "привет"]) == 2
    err = capsys.readouterr().err
    assert "запрос отклонён" in err and "Traceback" not in err and "login" not in err

