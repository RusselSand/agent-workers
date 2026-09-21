"""Настройки одного подключения: что назвали, с тем и работаем."""
from pathlib import Path

import pytest

from agent_workers.config import Settings, find, parse

SAMPLE = """\ufeff# подключение воркера
export AGENT_PROVIDER=claude\r
AGENT_HOME = D:/PROJECTS/.agent/рабочий
AGENT_MODEL="claude-opus-5"
AGENT_REFUSE_ABOVE=80  # осторожнее обычного

СЛОМАННАЯ СТРОКА БЕЗ РАВНО
"""


def settings_at(tmp_path, text=""):
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    return Settings.load(tmp_path)


def test_env_file_quirks_are_survivable():
    values = parse(SAMPLE)
    assert values["AGENT_PROVIDER"] == "claude"                    # BOM, export и CRLF
    assert values["AGENT_HOME"] == "D:/PROJECTS/.agent/рабочий"    # пробелы вокруг =
    assert values["AGENT_MODEL"] == "claude-opus-5"                # кавычки сняты
    assert values["AGENT_REFUSE_ABOVE"] == "80"                    # комментарий отрезан
    assert "СЛОМАННАЯ" not in str(values)


def test_file_is_found_from_a_nested_folder(tmp_path):
    (tmp_path / ".env").write_text("AGENT_PROVIDER=codex", encoding="utf-8")
    deep = tmp_path / "backend" / "agent_workers"
    deep.mkdir(parents=True)
    assert find(deep) == tmp_path / ".env"
    assert Settings.load(deep).provider == "codex"


def test_flag_beats_environment_beats_file(tmp_path, monkeypatch):
    settings = settings_at(tmp_path, "AGENT_PROVIDER=claude")
    assert settings.provider == "claude"
    monkeypatch.setenv("AGENT_PROVIDER", "codex")
    assert settings.provider == "codex"
    assert settings.override(provider="claude").provider == "claude"


def test_empty_override_changes_nothing(tmp_path):
    settings = settings_at(tmp_path, "AGENT_PROVIDER=codex")
    assert settings.override(provider=None, home="").provider == "codex"


def test_unnamed_connection_is_an_error_not_a_guess(tmp_path):
    settings = settings_at(tmp_path)
    with pytest.raises(ValueError, match="AGENT_PROVIDER"):
        assert settings.provider


def test_runs_live_next_to_the_account_by_default(tmp_path):
    settings = settings_at(tmp_path, f"AGENT_PROVIDER=claude\nAGENT_HOME={tmp_path.as_posix()}/acc")
    assert settings.runs == tmp_path / "acc" / "runs"


def test_account_folder_stays_out_of_the_repository(tmp_path):
    """В каталоге лежат токены входа — ему нельзя оказаться в репозитории."""
    settings = settings_at(tmp_path, "AGENT_PROVIDER=claude")
    assert settings.home == Path.home() / ".agent-worker" / "claude"
    assert Path.cwd() not in settings.home.parents


def test_policy_comes_from_the_same_file(tmp_path):
    settings = settings_at(tmp_path, "AGENT_PROVIDER=claude\nAGENT_REFUSE_ABOVE=80"
                                     "\nAGENT_SPEND_CREDITS=0")
    assert settings.policy.refuse_above == 80.0
    assert settings.policy.spend_credits is False
    assert settings_at(tmp_path, "AGENT_PROVIDER=claude").policy.refuse_above == 95.0
