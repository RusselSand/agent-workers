"""Воркер собирается из настроек и живёт с одним подключением."""
import pytest

from agent_workers import build
from agent_workers.config import Settings

from .stubs import cli_stub


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    # Тесты не должны требовать установленных CLI: подсовываем существующий файл.
    for name in ("AGENT_CLAUDE_BINARY", "AGENT_CODEX_BINARY"):
        monkeypatch.setenv(name, cli_stub())


def settings_at(tmp_path, text=""):
    (tmp_path / ".env").write_text(text, encoding="utf-8")
    return Settings.load(tmp_path)


def test_worker_is_built_around_the_named_folder(tmp_path):
    home = tmp_path / "учётка"
    worker = build(settings_at(tmp_path, f"AGENT_PROVIDER=codex\nAGENT_HOME={home.as_posix()}"))
    assert worker.adapter.name == "codex"
    assert worker.profile.home == home
    assert worker.profile.name == "учётка"      # имя профиля — имя каталога, без выдумок
    assert worker.root == home / "runs"


def test_another_account_is_another_folder(tmp_path):
    first = build(settings_at(tmp_path, f"AGENT_PROVIDER=codex\nAGENT_HOME={tmp_path}/первый"))
    second = build(settings_at(tmp_path, f"AGENT_PROVIDER=codex\nAGENT_HOME={tmp_path}/второй"))
    assert first.profile.home != second.profile.home
    assert first.root != second.root


def test_model_and_policy_come_from_the_settings(tmp_path):
    worker = build(settings_at(tmp_path, "AGENT_PROVIDER=codex\nAGENT_MODEL=gpt-5.6-luna"
                                         "\nAGENT_REFUSE_ABOVE=50"))
    assert worker.adapter.model == "gpt-5.6-luna"
    assert worker.policy.refuse_above == 50.0


def test_cli_path_comes_from_the_env_file_too(tmp_path, monkeypatch):
    """AGENT_CODEX_BINARY в .env работает, а относительный путь — от каталога .env."""
    from pathlib import Path

    for name in ("AGENT_CLAUDE_BINARY", "AGENT_CODEX_BINARY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "codex").write_text("", encoding="utf-8")
    (tmp_path / "bin" / "codex").chmod(0o755)           # заглушка, но запускаемая
    (tmp_path / ".env").write_text(f"AGENT_PROVIDER=codex\n"
                                   f"AGENT_HOME={(tmp_path / 'учётка').as_posix()}\n"
                                   f"AGENT_CODEX_BINARY=bin/codex\n", encoding="utf-8")
    nested = tmp_path / "где-то" / "глубже"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    worker = build(Settings.load())
    assert Path(worker.adapter.executable) == (tmp_path / "bin" / "codex").resolve()


@pytest.mark.skipif(__import__("os").name == "nt", reason="права на выполнение — POSIX")
def test_cli_without_execute_permission_is_refused_at_build(tmp_path, monkeypatch):
    """Файл есть, запустить нельзя: сказать при сборке, а не упасть на входе."""
    placeholder = tmp_path / "codex"
    placeholder.write_text("", encoding="utf-8")
    placeholder.chmod(0o644)
    monkeypatch.setenv("AGENT_CODEX_BINARY", str(placeholder))
    settings = settings_at(tmp_path, "AGENT_PROVIDER=codex")
    with pytest.raises(RuntimeError, match="права на выполнение"):
        build(settings)


def test_unknown_provider_is_named_not_guessed(tmp_path):
    settings = settings_at(tmp_path, "AGENT_PROVIDER=gemini")
    with pytest.raises(ValueError, match="gemini"):
        build(settings)
