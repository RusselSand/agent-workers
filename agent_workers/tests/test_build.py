"""Воркер собирается из настроек и живёт с одним подключением."""
import pytest

from agent_workers import build
from agent_workers.config import Settings


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    # Тесты не должны требовать установленных CLI: подсовываем существующий файл.
    for name in ("AGENT_CLAUDE_BINARY", "AGENT_CODEX_BINARY"):
        monkeypatch.setenv(name, __file__)


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


def test_unknown_provider_is_named_not_guessed(tmp_path):
    with pytest.raises(ValueError, match="gemini"):
        build(settings_at(tmp_path, "AGENT_PROVIDER=gemini"))
