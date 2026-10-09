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


def test_relative_paths_in_env_are_relative_to_the_env_file(tmp_path, monkeypatch):
    """.env нашёлся выше по дереву: его пути — от него, из какого каталога ни запусти.
    Иначе другой подкаталог — другой вход и другой лоток с невидимыми ответами."""
    for name in ("AGENT_HOME", "AGENT_RUNS"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("AGENT_PROVIDER=claude\nAGENT_HOME=.agent\n"
                                   "AGENT_RUNS=outbox\n", encoding="utf-8")
    nested = tmp_path / "backend" / "agent_workers"
    nested.mkdir(parents=True)
    for start in (tmp_path, nested):
        monkeypatch.chdir(start)
        settings = Settings.load()
        assert settings.home == (tmp_path / ".agent").resolve()
        assert settings.runs == (tmp_path / "outbox").resolve()


def test_relative_paths_from_the_environment_follow_the_cwd(tmp_path, monkeypatch):
    nested = tmp_path / "где-то"
    nested.mkdir()
    monkeypatch.chdir(nested)
    monkeypatch.setenv("AGENT_HOME", "учётка")
    assert Settings().home == (nested / "учётка").resolve()


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


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "101", "abc"])
def test_refusal_threshold_outside_0_100_is_a_configuration_error(tmp_path, value):
    """nan и отрицательные запретили бы всё, inf и больше 100 молча сняли бы отказ."""
    settings = settings_at(tmp_path, f"AGENT_PROVIDER=claude\nAGENT_REFUSE_ABOVE={value}")
    with pytest.raises(ValueError, match="AGENT_REFUSE_ABOVE"):
        _ = settings.policy


@pytest.mark.parametrize("value", ["flase", "2", "может быть"])
def test_typo_in_spend_credits_is_a_configuration_error(tmp_path, value):
    """Опечатка не должна молча разрешать тратить кредиты."""
    settings = settings_at(tmp_path, f"AGENT_PROVIDER=claude\nAGENT_SPEND_CREDITS={value}")
    with pytest.raises(ValueError, match="AGENT_SPEND_CREDITS"):
        _ = settings.policy


def test_spend_credits_understands_yes_and_no(tmp_path):
    for value, expected in (("нет", False), ("0", False), ("yes", True), ("", True)):
        settings = settings_at(tmp_path, f"AGENT_PROVIDER=claude\nAGENT_SPEND_CREDITS={value}")
        assert settings.policy.spend_credits is expected


def test_refusal_threshold_bounds_are_allowed(tmp_path):
    for value in ("0", "100"):
        settings = settings_at(tmp_path, f"AGENT_PROVIDER=claude\nAGENT_REFUSE_ABOVE={value}")
        assert settings.policy.refuse_above == float(value)


def test_policy_built_in_code_is_checked_too():
    from agent_workers.base import LimitPolicy

    with pytest.raises(ValueError, match="от 0 до 100"):
        LimitPolicy(refuse_above=float("nan"))


def test_empty_value_means_off_not_default(tmp_path):
    """`AGENT_REFUSE_ABOVE=` в файле снимает порог, а не возвращает умолчание."""
    settings = settings_at(tmp_path, "AGENT_PROVIDER=claude" "\n" "AGENT_REFUSE_ABOVE=")
    assert settings.policy.refuse_above is None
    assert settings_at(tmp_path, "AGENT_PROVIDER=claude").policy.refuse_above == 95.0


def test_turn_limits_come_from_the_file_and_empty_removes_one(tmp_path):
    settings = settings_at(tmp_path, "AGENT_PROVIDER=claude\nAGENT_TIMEOUT=7200\nAGENT_IDLE_TIMEOUT=")
    assert settings.timeout == 7200.0
    assert settings.idle is None
    default = settings_at(tmp_path, "AGENT_PROVIDER=claude")
    assert (default.timeout, default.idle) == (3600.0, 900.0)


@pytest.mark.parametrize("value", ["0", "-5", "nan", "inf", "час"])
@pytest.mark.parametrize(("key", "limit"), [("AGENT_TIMEOUT", "timeout"),
                                            ("AGENT_IDLE_TIMEOUT", "idle")])
def test_turn_limit_that_is_not_positive_seconds_is_a_configuration_error(tmp_path, key, limit,
                                                                          value):
    """Ноль снимал бы каждый ход сразу после запуска, уже оплаченным; nan молча снял бы предел."""
    settings = settings_at(tmp_path, f"AGENT_PROVIDER=claude\n{key}={value}")
    with pytest.raises(ValueError, match=key):
        getattr(settings, limit)


def test_comment_after_a_quoted_value_is_cut_off():
    """AGENT_MODEL="opus" # закреплено — кавычки снимаются, комментарий не прилипает."""
    values = parse('AGENT_MODEL="claude-opus-5" # закреплено')
    assert values["AGENT_MODEL"] == "claude-opus-5"
    assert parse("AGENT_HOME='D:/путь с пробелом' # тут")["AGENT_HOME"] == "D:/путь с пробелом"

