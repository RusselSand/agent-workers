"""База проверяется на поддельном адаптере: ни одной настоящей CLI здесь нет."""
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_workers.base import (
    Command,
    Cost,
    Guard,
    LimitPolicy,
    Limits,
    Reply,
    Usage,
    Window,
    Worker,
)
from agent_workers.base.entry import Entry, digest

QUIET = LimitPolicy(settle_reads=1, settle_delay=0)


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
        from decimal import Decimal
        return Cost(Decimal("0.01"), "USD", "table")

    def limits(self, profile, *, session=None):
        self.reads += 1
        return limits(self.percent.pop(0) if self.percent else 0.0)


def test_digest_is_stable_for_the_same_request():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})


def test_entry_start_marker_is_taken_once(tmp_path):
    entry = Entry(tmp_path, "run")
    assert entry.reserve_start() is True
    assert Entry(tmp_path, "run").reserve_start() is False


def test_worker_measures_before_and_after_and_reports_the_price(tmp_path, profile):
    adapter = FakeAdapter(tmp_path)
    result = Worker(adapter, profile, tmp_path / "runs", QUIET).run({"user": "привет"})
    assert result["state"] == "answered"
    assert result["reply"].text == "привет"
    assert result["before"].worst == 10.0 and result["after"].worst == 12.5
    assert result["spent"] == {"window": 2.5}
    # Рядом с ценой в процентах окна — во что тот же ход обошёлся бы по API.
    assert result["tokens"].output == 1 and str(result["cost"].amount) == "0.01"


def test_worker_refuses_before_spending_anything(tmp_path, profile):
    adapter = FakeAdapter(tmp_path, percent=[97.0])
    result = Worker(adapter, profile, tmp_path / "runs", QUIET).run({"user": "привет"})
    assert result["state"] == "limit_reached"
    assert "97" in result["reason"]
    assert adapter.asked == []            # запрос не собирался
    assert result["after"] is None


def test_second_pass_over_the_same_request_does_not_run_again(tmp_path, profile):
    adapter = FakeAdapter(tmp_path, percent=[1.0, 2.0, 3.0, 4.0])
    worker = Worker(adapter, profile, tmp_path / "runs", QUIET)
    worker.run({"user": "привет"})
    again = worker.run({"user": "привет"})
    assert again["state"] == "resumed"
    assert len(adapter.asked) == 2        # команду собрали, но процесс не запускали
    assert again["reply"].text == "привет"


def test_measurement_after_the_turn_waits_for_accounting(tmp_path, profile):
    """Счётчик догоняет вызов не сразу: последнее из нескольких чтений и есть замер."""
    adapter = FakeAdapter(tmp_path, percent=[10.0, 10.0, 14.0])
    slept = []
    guard = Guard(adapter, profile, LimitPolicy(settle_reads=3, settle_delay=2.0),
                  sleep=slept.append)
    assert guard.measure("after").worst == 14.0
    assert (adapter.reads, slept) == (3, [2.0, 2.0, 2.0])


def test_measurement_before_the_turn_is_read_once(tmp_path, profile):
    adapter = FakeAdapter(tmp_path)
    guard = Guard(adapter, profile, LimitPolicy(settle_reads=3), sleep=lambda _: None)
    guard.measure("before")
    assert adapter.reads == 1


def test_exhausted_window_with_credits_left_is_not_a_stop(tmp_path, profile):
    """У Codex окно может быть выбрано целиком, а работа продолжаться за кредиты."""
    from decimal import Decimal

    guard = Guard(FakeAdapter(tmp_path), profile, QUIET)
    spent = Limits("fake", "test", (Window("primary", 100.0),), datetime.now(UTC), "test", True,
                   credits=Decimal("184.52"))
    assert guard.blocked(spent) is None
    assert guard.blocked(limits(100.0)) == "Окно выбрано на 100%"

    strict = Guard(FakeAdapter(tmp_path), profile, LimitPolicy(spend_credits=False))
    assert strict.blocked(spent) == "Окно выбрано на 100%"


def test_stale_snapshot_counts_as_unknown(tmp_path, profile):
    policy = LimitPolicy(on_unknown="refuse", stale_after=timedelta(minutes=1))
    guard = Guard(FakeAdapter(tmp_path), profile, policy)
    old = limits(10.0, exact=False, measured_at=datetime.now(UTC) - timedelta(hours=1))
    assert guard.blocked(old) == "Лимит неизвестен"
    assert guard.blocked(limits(10.0, exact=True)) is None


def test_failed_measurement_does_not_break_the_turn(tmp_path, profile):
    class Broken(FakeAdapter):
        def limits(self, profile, *, session=None):
            raise OSError("зонд недоступен")

    result = Worker(Broken(tmp_path), profile, tmp_path / "runs", QUIET).run({"user": "привет"})
    assert result["state"] == "answered" and result["before"] is None
