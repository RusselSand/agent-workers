"""База проверяется на поддельном адаптере: ни одной настоящей CLI здесь нет."""
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
from agent_workers.base.entry import Entry, digest, folder_for

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


def test_folder_is_owned_by_one_process_at_a_time(tmp_path):
    entry = Entry(tmp_path, "run")
    assert entry.claim() is True
    assert Entry(tmp_path, "run").claim() is False   # замок держит первый
    entry.release()
    assert Entry(tmp_path, "run").claim() is True    # отпустил — можно брать


def test_key_cannot_point_outside_the_folder(tmp_path):
    """Ключ приходит снаружи, а папку сносят рекурсивно: путь в ключе недопустим."""
    for key in ("..", ".", "", "../соседняя", "/abs", "C:/windows", "вложенный/путь"):
        with pytest.raises(ValueError):
            folder_for(tmp_path, key)
    assert folder_for(tmp_path, "обычный-ключ") == (tmp_path / "обычный-ключ").resolve()


def test_collect_refuses_a_key_that_is_a_path(tmp_path, profile):
    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET)
    victim = tmp_path / "runs"
    victim.mkdir(parents=True)
    (victim / "чужое.txt").write_text("не трогать", encoding="utf-8")
    for key in ("..", "../runs", "/tmp"):
        with pytest.raises(ValueError):
            worker.collect(key)
    assert (victim / "чужое.txt").exists()


def test_collect_refuses_an_unknown_key(tmp_path, profile):
    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET)
    with pytest.raises(ValueError, match="нет"):
        worker.collect("никогда-не-существовавший")


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


def test_ready_answer_is_served_for_free(tmp_path, profile):
    """Готовый ответ уже оплачен: ни замера, ни сборки запроса, ни запуска."""
    adapter = FakeAdapter(tmp_path, percent=[1.0, 2.0])
    worker = Worker(adapter, profile, tmp_path / "runs", QUIET)
    worker.run({"user": "привет"})
    reads, asked = adapter.reads, len(adapter.asked)

    again = worker.run({"user": "привет"})
    assert again["state"] == "resumed" and again["reply"].text == "привет"
    assert (adapter.reads, len(adapter.asked)) == (reads, asked)
    assert again["before"] is None and again["spent"] == {}


def test_exhausted_window_does_not_hide_a_ready_answer(tmp_path, profile):
    """Иначе выбранный лимит стирал бы уже полученный ответ отказом."""
    adapter = FakeAdapter(tmp_path, percent=[1.0, 2.0])
    worker = Worker(adapter, profile, tmp_path / "runs", QUIET)
    worker.run({"user": "привет"})
    entry = Worker(adapter, profile, tmp_path / "runs", QUIET).run({"user": "привет"})["entry"]

    strict = Worker(FakeAdapter(tmp_path, percent=[99.0]), profile, tmp_path / "runs", QUIET)
    served = strict.run({"user": "привет"})
    assert served["state"] == "resumed"
    assert entry.meta["state"] == "answered"     # отказ не затирает состояние папки


def test_dead_attempt_is_not_reported_as_running(tmp_path, profile):
    """След оборванной попытки не должен выдавать себя за идущую работу."""
    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET)
    entry = Entry(tmp_path / "runs", digest({"adapter": "fake", "request": {"user": "привет"}}))
    entry.mark_started()
    entry.update(state="running", pid=999_999_999)   # владельца нет: замок никто не держит

    stalled = worker.run({"user": "привет"})
    assert stalled["state"] == "incomplete"
    assert "retry" in stalled["reason"]


def test_retry_starts_a_clean_attempt_and_keeps_the_old_one(tmp_path, profile):
    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET)
    entry = Entry(tmp_path / "runs", digest({"adapter": "fake", "request": {"user": "привет"}}))
    entry.mark_started()
    entry.write("stdout.jsonl", "обрывок прошлой попытки")
    entry.update(state="incomplete")

    again = worker.run({"user": "привет"}, retry=True)
    assert again["state"] == "answered" and again["reply"].text == "привет"
    kept = (again["entry"].folder / "attempt-1" / "stdout.jsonl").read_text(encoding="utf-8")
    assert kept == "обрывок прошлой попытки"


def test_live_owner_is_left_alone(tmp_path, profile):
    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET)
    owner = Entry(tmp_path / "runs", digest({"adapter": "fake", "request": {"user": "привет"}}))
    assert owner.claim() is True
    try:
        assert worker.run({"user": "привет"})["state"] == "in_progress"
    finally:
        owner.release()


def test_retry_does_not_touch_a_live_attempt(tmp_path, profile):
    """Иначе журналы работающего хода уехали бы в архив, а вызов оплатил бы его заново."""
    adapter = FakeAdapter(tmp_path)
    worker = Worker(adapter, profile, tmp_path / "runs", QUIET)
    owner = Entry(tmp_path / "runs", digest({"adapter": "fake", "request": {"user": "привет"}}))
    owner.claim()
    owner.mark_started()
    owner.write("stdout.jsonl", "идущая работа")
    try:
        refused = worker.run({"user": "привет"}, retry=True)
    finally:
        owner.release()
    assert refused["state"] == "in_progress"
    assert adapter.asked == []                                   # второго вызова не было
    assert owner.read("stdout.jsonl") == "идущая работа"          # журнал на месте
    assert not (owner.folder / "attempt-1").exists()              # архива не появилось


def test_result_waits_in_the_outbox_until_it_is_collected(tmp_path, profile):
    """Папка живёт не по таймеру, а пока результат не заберут."""
    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET)
    result = worker.run({"user": "привет"})

    waiting = worker.pending()
    assert [entry.folder for entry in waiting] == [result["entry"].folder]

    worker.collect(waiting[0])
    assert worker.pending() == []
    assert not result["entry"].folder.exists()


def test_collected_answer_is_no_longer_served_for_free(tmp_path, profile):
    adapter = FakeAdapter(tmp_path, percent=[1.0, 2.0, 3.0, 4.0])
    worker = Worker(adapter, profile, tmp_path / "runs", QUIET)
    worker.collect(worker.run({"user": "привет"})["entry"])
    assert worker.run({"user": "привет"})["state"] == "answered"   # выполнили заново, честно


def test_full_outbox_stops_new_turns_instead_of_filling_the_disk(tmp_path, profile):
    adapter = FakeAdapter(tmp_path, percent=[1.0, 2.0])
    worker = Worker(adapter, profile, tmp_path / "runs", QUIET, max_pending=1)
    worker.run({"user": "первый"})
    reads, asked = adapter.reads, len(adapter.asked)

    stopped = worker.run({"user": "второй"})
    assert stopped["state"] == "outbox_full"
    assert (adapter.reads, len(adapter.asked)) == (reads, asked)   # ничего не потрачено
    assert len(worker.pending()) == 1                              # мусора не прибавилось


def test_refused_turn_leaves_no_folder_behind(tmp_path, profile):
    worker = Worker(FakeAdapter(tmp_path, percent=[99.0]), profile, tmp_path / "runs", QUIET)
    refused = worker.run({"user": "привет"})
    assert refused["state"] == "limit_reached"
    assert not refused["entry"].folder.exists()
    assert worker.pending() == []


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


def test_running_entry_cannot_be_collected(tmp_path, profile):
    """Снести папку идущего хода — значит выдернуть у него журналы и замок из-под ног."""
    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET)
    owner = Entry(tmp_path / "runs", "живой-ход")
    owner.claim()
    owner.update(state="running", started=True)
    try:
        with pytest.raises(RuntimeError, match="выполняется"):
            worker.collect("живой-ход")
    finally:
        owner.release()
    assert owner.folder.exists()
    worker.collect("живой-ход")          # владелец ушёл — теперь можно
    assert not owner.folder.exists()


def test_new_entry_writes_nothing_before_it_is_owned(tmp_path):
    """Иначе двое, пришедшие одновременно, затрут состояние друг друга."""
    entry = Entry(tmp_path, "ход")
    assert not entry.state_path.exists()
    assert entry.meta["state"] == "prepared" and entry.attempted is False


def test_latecomer_does_not_overwrite_the_live_state(tmp_path):
    owner = Entry(tmp_path, "ход")
    owner.claim()
    owner.update(state="running", started=True)

    latecomer = Entry(tmp_path, "ход")          # второй процесс на том же ключе
    assert latecomer.claim() is False           # владения не получил
    assert latecomer.meta["state"] == "running"  # и ничего не переписал
    owner.release()
