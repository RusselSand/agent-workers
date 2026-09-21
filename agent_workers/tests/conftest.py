from pathlib import Path

import pytest

SAMPLES = Path(__file__).parent / "samples"


@pytest.fixture
def sample():
    return lambda name: (SAMPLES / name).read_text(encoding="utf-8")


@pytest.fixture
def profile(tmp_path):
    from agent_workers.base import Profile
    return Profile("test", tmp_path / "home")
