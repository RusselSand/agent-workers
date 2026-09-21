"""Диалог по stdio — механика базы, поэтому проверяется на обычном python-процессе."""
import os
import sys

import pytest

from agent_workers.base import Channel, Command
from agent_workers.base.channel import ChannelError

ECHO = """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    print(json.dumps({"id": message["id"], "echo": message.get("text", "")}), flush=True)
"""

SILENT = "import sys; sys.stdin.read()"


def command(tmp_path, source):
    script = tmp_path / "child.py"
    script.write_text(source, encoding="utf-8")
    return Command((sys.executable, str(script)), dict(os.environ), tmp_path, timeout=5)


def test_answer_is_matched_by_request_id(tmp_path):
    with Channel(command(tmp_path, ECHO)) as channel:
        channel.send({"id": 1, "text": "первый"})
        channel.send({"id": 2, "text": "второй"})
        answer = channel.wait(lambda item: item.get("id") == 2)
        assert answer["echo"] == "второй"


def test_silence_ends_with_an_error_not_a_hang(tmp_path):
    with Channel(command(tmp_path, SILENT)) as channel:
        channel.send({"id": 1})
        with pytest.raises(ChannelError):
            channel.wait(lambda item: True, timeout=0.3)


def test_early_exit_is_reported(tmp_path):
    with Channel(command(tmp_path, "pass")) as channel:
        with pytest.raises(ChannelError):
            channel.wait(lambda item: True, timeout=5)


def test_process_is_closed_on_leaving_the_block(tmp_path):
    with Channel(command(tmp_path, ECHO)) as channel:
        process = channel.process
    assert process.poll() is not None
