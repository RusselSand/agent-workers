"""Поддельный адаптер для тестов пакета и кода, который на нём построен.

FakeAdapter отвечает эхом через настоящий дочерний процесс Python и отдаёт заранее
заданные замеры расхода: Worker проходит весь ход, но ни одна CLI не запускается и
подписка не тратится. pytest здесь не нужен.
"""
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .base import Command, Cost, LimitPolicy, Limits, Reply, Usage, Window

QUIET = LimitPolicy(settle_reads=1, settle_delay=0)   # замер «после» один раз и без паузы


def limits(percent, *, exact=True, measured_at=None):
    return Limits("fake", "test", (Window("window", percent),),
                  measured_at or datetime.now(UTC), "test", exact)


@dataclass
class FakeAdapter:
    """Отвечает эхом через отдельный процесс и отдаёт заранее заданные замеры."""

    root: Path
    name = "fake"
    percent: list = field(default_factory=lambda: [10.0, 12.5])
    asked: list = field(default_factory=list)
    reads: int = 0

    def environment(self, profile):
        return dict(os.environ)

    def check(self, profile):
        return Command((sys.executable, "-c", "print('ok')"), dict(os.environ), self.root)

    def verify(self, captured):
        if "ok" not in captured:
            raise RuntimeError("нет входа")

    def ask(self, entry, request, profile):
        self.asked.append(request)
        # Пишем байтами: консольная кодировка Windows не переварила бы кириллицу.
        script = f"import sys; sys.stdout.buffer.write({request['user']!r}.encode('utf-8'))"
        return Command((sys.executable, "-c", script), dict(os.environ), entry.folder)

    def reply(self, entry, profile):
        text = entry.read("stdout.jsonl")
        return Reply(text, "session-1", bool(text), None, {"tokens": len(text)},
                     Usage(input=len(text), output=1), "fake-model")

    def price(self, reply):
        return Cost(Decimal("0.01"), "USD", "table")

    def limits(self, profile, *, session=None, model=None, fresh_within=None):
        self.reads += 1
        return limits(self.percent.pop(0) if self.percent else 0.0)
