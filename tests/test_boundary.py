"""База не должна знать о провайдерах: иначе шов перестаёт быть швом."""
from pathlib import Path

import agent_workers

BASE = Path(agent_workers.__file__).resolve().parent / "base"


def test_base_does_not_mention_providers():
    for path in BASE.rglob("*.py"):
        source = path.read_text(encoding="utf-8").lower()
        for forbidden in ("codex", "claude", "chatgpt", "providers"):
            assert forbidden not in source, f"{path.name}: упоминание {forbidden!r}"


def test_base_imports_nothing_from_providers():
    for path in BASE.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "import" not in source or "agent_workers.providers" not in source, path.name
