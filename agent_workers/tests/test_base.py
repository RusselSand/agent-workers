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
from agent_workers.base.entry import Entry, NotRemoved, digest, folder_for, registry

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

    def fingerprint(self):
        return {"model": getattr(self, "model", "")}

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

    def limits(self, profile, *, session=None, model=None):
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
    entry = Entry(tmp_path / "runs", worker.key_for({"user": "привет"}))
    entry.mark_started()
    entry.update(state="running", pid=999_999_999)   # владельца нет: замок никто не держит

    stalled = worker.run({"user": "привет"})
    assert stalled["state"] == "incomplete"
    assert "retry" in stalled["reason"]


def test_retry_starts_a_clean_attempt_and_keeps_the_old_one(tmp_path, profile):
    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET)
    entry = Entry(tmp_path / "runs", worker.key_for({"user": "привет"}))
    entry.mark_started()
    entry.write("stdout.jsonl", "обрывок прошлой попытки")
    entry.update(state="incomplete")

    again = worker.run({"user": "привет"}, retry=True)
    assert again["state"] == "answered" and again["reply"].text == "привет"
    kept = (again["entry"].folder / "attempt-1" / "stdout.jsonl").read_text(encoding="utf-8")
    assert kept == "обрывок прошлой попытки"


def test_live_owner_is_left_alone(tmp_path, profile):
    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET)
    owner = Entry(tmp_path / "runs", worker.key_for({"user": "привет"}))
    assert owner.claim() is True
    try:
        assert worker.run({"user": "привет"})["state"] == "in_progress"
    finally:
        owner.release()


def test_retry_does_not_touch_a_live_attempt(tmp_path, profile):
    """Иначе журналы работающего хода уехали бы в архив, а вызов оплатил бы его заново."""
    adapter = FakeAdapter(tmp_path)
    worker = Worker(adapter, profile, tmp_path / "runs", QUIET)
    owner = Entry(tmp_path / "runs", worker.key_for({"user": "привет"}))
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
        def limits(self, profile, *, session=None, model=None):
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


def test_registry_lock_is_exclusive_between_processes(tmp_path):
    """Замок каталога должен держать чужой процесс, а не только собственный поток."""
    import subprocess
    import sys

    probe = Path(__file__).with_name('probe_lock.py')
    package = Path(__file__).resolve().parents[2]
    lock = tmp_path / '.registry.lock'

    def ask() -> str:
        done = subprocess.run([sys.executable, str(probe), str(lock)],
                              capture_output=True, text=True,
                              env={**os.environ, 'PYTHONPATH': str(package)})
        return done.stdout.strip()

    with registry(tmp_path):
        assert ask() == 'held'
    assert ask() == 'free'


def test_deletion_happens_under_the_registry_lock(tmp_path, profile):
    """Замок самой папки исчезает вместе с ней, поэтому удаление держит замок каталога."""
    from contextlib import contextmanager

    import agent_workers.base.entry as entry_module

    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / 'runs', QUIET)
    entry = Entry(tmp_path / 'runs', 'ход')
    entry.claim()
    entry.update(state='answered')
    entry.release()

    order = []
    keep_registry = entry_module.registry
    keep_rmtree = entry_module.shutil.rmtree

    @contextmanager
    def watched(root):
        order.append('замок взят')
        with keep_registry(root):
            yield
        order.append('замок снят')

    def rmtree(path, *args, **kwargs):
        order.append('удаление')
        keep_rmtree(path, *args, **kwargs)

    entry_module.registry, entry_module.shutil.rmtree = watched, rmtree
    try:
        worker.collect('ход')
    finally:
        entry_module.registry = keep_registry
        entry_module.shutil.rmtree = keep_rmtree
    assert order[-3:] == ['замок взят', 'удаление', 'замок снят']
    assert not entry.folder.exists()


def test_failed_removal_is_reported_not_swallowed(tmp_path, profile):
    """Недоснесённая папка снова всплывёт в лотке — молчать об этом нельзя."""
    import agent_workers.base.entry as entry_module

    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET)
    entry = Entry(tmp_path / "runs", "ход")
    entry.claim()
    entry.update(state="answered")
    entry.release()

    original = entry_module.shutil.rmtree
    entry_module.shutil.rmtree = lambda *a, **k: None    # как будто удалить не вышло
    try:
        with pytest.raises(NotRemoved):
            worker.collect("ход")
    finally:
        entry_module.shutil.rmtree = original
    assert entry.folder.exists()


def test_key_depends_on_the_model_that_will_answer(tmp_path, profile):
    """Иначе смена модели отдала бы старый ответ из лотка как свой."""
    class Named(FakeAdapter):
        model = "первая-модель"

    worker = Worker(Named(tmp_path), profile, tmp_path / "runs", QUIET)
    other = Worker(Named(tmp_path), profile, tmp_path / "runs", QUIET)
    other.adapter.model = "вторая-модель"

    assert worker.key_for({"user": "привет"}) != other.key_for({"user": "привет"})
    assert worker.key_for({"user": "привет"}) == worker.key_for(
        {"user": "привет", "model": "первая-модель"})


def test_answer_of_another_model_is_not_served_as_ours(tmp_path, profile):
    class Named(FakeAdapter):
        model = "первая-модель"

    first = Worker(Named(tmp_path), profile, tmp_path / "runs", QUIET)
    first.run({"user": "привет"})

    second = Worker(Named(tmp_path), profile, tmp_path / "runs", QUIET)
    second.adapter.model = "вторая-модель"
    assert second.run({"user": "привет"})["state"] == "answered"   # выполнили заново


def test_started_turn_takes_a_slot_in_the_outbox(tmp_path, profile):
    """Иначе несколько процессов, глядя на один лоток, стартуют разом и перевалят предел."""
    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET, max_pending=1)
    started = Entry(tmp_path / "runs", "чужой-ход")
    started.claim()
    started.mark_started()          # ход идёт, результата ещё нет
    started.update(state="running")
    started.release()

    assert worker.outbox.occupied() == 1
    assert worker.pending() == []   # в лотке пусто, но место занято
    assert worker.run({"user": "привет"})["state"] == "outbox_full"


def test_retry_forgets_the_numbers_of_the_previous_attempt(tmp_path, profile):
    """Расход и сессия относились к прошлой попытке, новый ход отчитается своими."""
    entry = Entry(tmp_path, "ход")
    entry.claim()
    entry.update(state="answered", started=True, usage={"input_tokens": 13773},
                 session_id="сессия-1", returncode=0)
    entry.restart()
    assert "usage" not in entry.meta and entry.meta["session_id"] is None
    assert entry.meta["attempt"] == 1 and entry.attempted is False
    entry.release()


def test_disabled_measurement_is_not_an_unknown_quota(tmp_path, profile):
    """Выключенный замер — решение вызывающего, а не повод всё запретить."""
    policy = LimitPolicy(before=False, after=False, on_unknown="refuse")
    result = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", policy).run({"user": "ок"})
    assert result["state"] == "answered" and result["before"] is None


def test_window_that_reset_mid_turn_is_not_counted_as_spending(tmp_path, profile):
    """99% до и 1% после — это не «минус 98», это новый отсчёт."""
    guard = Guard(FakeAdapter(tmp_path), profile, QUIET)
    moment = datetime.now(UTC)
    before = Limits("fake", "test", (Window("session", 99.0, resets_at=moment),
                                     Window("week", 10.0, resets_at=moment)),
                    moment, "test", True)
    after = Limits("fake", "test", (Window("session", 1.0, resets_at=moment + timedelta(hours=5)),
                                    Window("week", 11.0, resets_at=moment)),
                   moment, "test", True)
    assert guard.spent(before, after) == {"week": 1.0}


def test_settings_that_change_execution_change_the_key(tmp_path, profile):
    class Tuned(FakeAdapter):
        model = "модель"
        effort = "medium"

        def fingerprint(self):
            return {"model": self.model, "effort": self.effort}

    worker = Worker(Tuned(tmp_path), profile, tmp_path / "runs", QUIET)
    other = Worker(Tuned(tmp_path), profile, tmp_path / "runs", QUIET)
    other.adapter.effort = "high"
    assert worker.key_for({"user": "ок"}) != other.key_for({"user": "ок"})


def test_broken_request_frees_the_slot_it_reserved(tmp_path, profile):
    """Кривые задания не должны потихоньку забивать лоток."""
    class Picky(FakeAdapter):
        def ask(self, entry, request, profile):
            raise ValueError("нет обязательного поля")

    worker = Worker(Picky(tmp_path), profile, tmp_path / "runs", QUIET, max_pending=2)
    for _ in range(3):
        with pytest.raises(ValueError):
            worker.run({"user": "плохое"})
    assert worker.outbox.occupied() == 0
    assert Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET,
                  max_pending=2).run({"user": "хорошее"})["state"] == "answered"


def test_loop_hands_the_stop_signal_to_the_tick(tmp_path):
    """Сигнал во время долгого хода должен дойти до самого хода, а не ждать его конца."""
    import threading

    from agent_workers.base import run_loop

    stop = threading.Event()
    seen = []

    def tick(signal):
        seen.append(signal)
        signal.set()          # как будто сигнал пришёл внутрь хода

    assert run_loop(tick, poll_seconds=0, stop=stop) == 0
    assert seen == [stop] and stop.is_set()


def test_run_folder_is_readable_only_by_its_owner(tmp_path):
    if os.name == "nt":
        pytest.skip("на Windows права выставляются иначе")
    entry = Entry(tmp_path, "ход")
    entry.claim()
    entry.update(state="answered")
    entry.write("invocation/input.txt", "секретное задание")
    try:
        assert entry.folder.stat().st_mode & 0o077 == 0
        assert entry.state_path.stat().st_mode & 0o077 == 0
        assert (entry.folder / "invocation" / "input.txt").stat().st_mode & 0o077 == 0
    finally:
        entry.release()


def test_interrupt_while_building_the_request_frees_the_slot(tmp_path, profile):
    class Interrupted(FakeAdapter):
        def ask(self, entry, request, profile):
            raise KeyboardInterrupt

    worker = Worker(Interrupted(tmp_path), profile, tmp_path / "runs", QUIET, max_pending=1)
    with pytest.raises(KeyboardInterrupt):
        worker.run({"user": "раз"})
    assert worker.outbox.occupied() == 0


def test_interrupted_turn_ends_up_in_the_outbox_not_in_limbo(tmp_path, profile):
    """Ctrl+C посреди хода: папка становится «незавершённой», её видно и можно забрать."""
    import agent_workers.base.worker as worker_module

    keep = worker_module.supervise

    def broken_supervise(*args, **kwargs):
        raise KeyboardInterrupt

    worker_module.supervise = broken_supervise
    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET, max_pending=1)
    try:
        with pytest.raises(KeyboardInterrupt):
            worker.run({"user": "два"})
    finally:
        worker_module.supervise = keep

    stuck = worker.pending()
    assert len(stuck) == 1 and stuck[0].meta["state"] == "incomplete"
    worker.collect(stuck[0])                      # а не вечное занятое место
    assert worker.outbox.occupied() == 0


def test_dead_attempt_becomes_collectable(tmp_path, profile):
    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET)
    entry = Entry(tmp_path / "runs", worker.key_for({"user": "привет"}))
    entry.mark_started()
    entry.update(state="running")
    assert worker.pending() == []
    assert worker.run({"user": "привет"})["state"] == "incomplete"
    assert [e.folder for e in worker.pending()] == [entry.folder]


def test_clock_jitter_in_reset_time_is_not_a_reset(tmp_path, profile):
    """Микросекунды времени сброса плавают от замера к замеру — это не новый отсчёт."""
    guard = Guard(FakeAdapter(tmp_path), profile, QUIET)
    moment = datetime.now(UTC)
    before = Limits("fake", "t", (Window("session", 12.0, resets_at=moment),), moment, "t", True)
    after = Limits("fake", "t", (Window("session", 13.0,
                                        resets_at=moment + timedelta(microseconds=5537)),),
                   moment, "t", True)
    assert guard.spent(before, after) == {"session": 1.0}


def test_measurement_uses_the_model_of_the_request(tmp_path, profile):
    class Watching(FakeAdapter):
        model = "по-умолчанию"
        asked_models: list = []

        def limits(self, profile, *, session=None, model=None):
            self.asked_models.append(model)
            return super().limits(profile, session=session, model=model)

    adapter = Watching(tmp_path, percent=[1.0, 1.0, 1.0])
    Worker(adapter, profile, tmp_path / "runs", QUIET).run({"user": "ок", "model": "другая"})
    assert adapter.asked_models and set(adapter.asked_models) == {"другая"}


def test_no_measurement_after_a_requested_stop(tmp_path, profile):
    """Нас попросили остановиться — не тянем выход паузами и запусками CLI."""
    import agent_workers.base.worker as worker_module
    from agent_workers.base.process import Outcome

    keep = worker_module.supervise
    worker_module.supervise = lambda *a, **k: Outcome(None, "stopped")
    adapter = FakeAdapter(tmp_path, percent=[1.0, 2.0, 3.0])
    try:
        result = Worker(adapter, profile, tmp_path / "runs", QUIET).run({"user": "ок"})
    finally:
        worker_module.supervise = keep
    assert result["after"] is None and adapter.reads == 1


def test_cached_reply_is_served_even_when_login_cannot_be_checked(tmp_path, profile):
    class LoggedOut(FakeAdapter):
        def verify(self, captured):
            raise RuntimeError("выхода из учётной записи")

    adapter = LoggedOut(tmp_path)
    worker = Worker(adapter, profile, tmp_path / "runs", QUIET)
    worker.run({"user": "привет"})                                  # ответ уже в лотке
    served = worker.run({"user": "привет"}, ensure_login=True)
    assert served["state"] == "resumed"                             # вход не понадобился
    with pytest.raises(RuntimeError, match="вход не подтверждён"):
        worker.run({"user": "новый вопрос"}, ensure_login=True)     # а тут нужен


def test_dead_reservation_is_neither_counted_nor_mistaken_for_a_turn(tmp_path, profile):
    """Процесс убили между бронью и запуском CLI: папка пуста, ход не стоил ничего."""
    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET, max_pending=1)
    stale = Entry(tmp_path / "runs", worker.key_for({"user": "привет"}))
    stale.update(state="reserved")            # бронь есть, владельца нет, старта не было

    assert worker.outbox.occupied() == 0      # лоток не забивается чередой падений
    result = worker.run({"user": "привет"})
    assert result["state"] == "answered"      # выполнили, а не объявили оборванным


def test_live_reservation_takes_a_slot(tmp_path, profile):
    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET, max_pending=1)
    owner = Entry(tmp_path / "runs", "чужая-бронь")
    owner.claim()
    owner.update(state="reserved")
    try:
        assert worker.outbox.occupied() == 1
        assert worker.run({"user": "привет"})["state"] == "outbox_full"
    finally:
        owner.release()


def test_continuations_of_one_conversation_take_turns(tmp_path, profile):
    """Два продолжения одной беседы с разными вопросами не идут одновременно."""
    from agent_workers.base.entry import hold, let_go

    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET)
    session = "беседа-1"
    busy = hold(tmp_path / "runs" / ".sessions" / f"{digest(session)}.lock")
    try:
        blocked = worker.run({"user": "второй вопрос", "session": session})
        assert blocked["state"] == "in_progress" and "беседу" in blocked["reason"]
        other = worker.run({"user": "вопрос", "session": "беседа-2"})
        assert other["state"] == "answered"          # чужая беседа не мешает
    finally:
        let_go(busy)
    assert worker.run({"user": "второй вопрос", "session": session})["state"] == "answered"


def test_relative_account_folder_becomes_absolute(tmp_path, monkeypatch):
    from agent_workers.base import Profile
    from agent_workers.config import Settings

    monkeypatch.chdir(tmp_path)
    assert Profile("x", Path(".agent")).home == (tmp_path / ".agent").resolve()
    (tmp_path / ".env").write_text("AGENT_PROVIDER=claude" + chr(10) + "AGENT_HOME=.agent",
                                   encoding="utf-8")
    settings = Settings.load(tmp_path)
    assert settings.home.is_absolute() and settings.runs.is_absolute()


def test_relative_executable_becomes_absolute(tmp_path, monkeypatch):
    from agent_workers.providers import common

    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "claude").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    found = common.find_executable("./bin/claude", "AGENT_CLAUDE_BINARY", "claude")
    assert Path(found).is_absolute() and Path(found) == (tmp_path / "bin" / "claude").resolve()


def test_stopping_a_turn_kills_its_grandchildren(tmp_path):
    """Иначе внук CLI переживёт ход и продолжит писать в папку, которую уже забирают."""
    if os.name == "nt":
        pytest.skip("на Windows проверяется вручную через taskkill /T")
    import time

    from agent_workers.base.process import supervise

    pid_file = tmp_path / "grandchild.pid"
    command = Command((sys.executable, str(Path(__file__).with_name("spawning_child.py")),
                       str(pid_file)), dict(os.environ), tmp_path)
    outcome = supervise(command, stdout=tmp_path / "out", stderr=tmp_path / "err", timeout=1.0)
    assert outcome.interruption == "timeout"
    grandchild = int(pid_file.read_text(encoding="utf-8"))

    def gone(pid: int) -> bool:
        # В контейнере без init убитый внук остаётся зомби: PID ещё есть, процесса нет.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        status = Path(f"/proc/{pid}/status")
        return status.is_file() and "State:" + chr(9) + "Z" in status.read_text()

    for _ in range(50):
        if gone(grandchild):
            break
        time.sleep(0.1)
    else:
        pytest.fail("внук пережил снятие хода")


def test_same_prompt_after_the_conversation_moved_on_is_a_new_turn(tmp_path, profile):
    """«Продолжай» после того, как беседа продвинулась, — другой вопрос, не тот же ответ."""
    worker = Worker(FakeAdapter(tmp_path, percent=[1.0] * 10), profile, tmp_path / "runs", QUIET)
    first = worker.run({"user": "продолжай", "session": "беседа"})
    assert first["state"] == "answered"

    again = worker.run({"user": "продолжай", "session": "беседа"})  # беседа уже продвинулась
    assert again["state"] == "answered"                             # выполнили заново
    assert again["entry"].folder != first["entry"].folder

    # Свой ключ задачи — единственный способ попросить именно тот, старый результат.
    keyed = worker.run({"user": "вопрос", "session": "беседа"}, key="задача-7")
    assert worker.run({"user": "вопрос", "session": "беседа"}, key="задача-7")["state"] == "resumed"
    assert keyed["state"] == "answered"


def test_generation_survives_a_crash_so_a_retry_still_resumes(tmp_path, profile):
    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET)
    key_before = worker.key_for({"user": "вопрос", "session": "беседа"})
    worker.run({"user": "вопрос", "session": "беседа"})
    # Тот же вопрос той же беседы, пока она не продвинулась дальше, — та же папка.
    assert worker.key_for({"user": "вопрос", "session": "беседа"}) != key_before
    assert worker.generation("беседа") == 1


def test_ready_answer_is_served_while_the_conversation_is_busy(tmp_path, profile):
    """Готовый ответ беседу не трогает — его отдают, не дожидаясь её очереди."""
    from agent_workers.base.entry import hold, let_go

    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET)
    worker.run({"user": "первый", "session": "беседа"}, key="задача-1")
    busy = hold(worker.session_file("беседа", "lock"))
    try:
        served = worker.run({"user": "первый", "session": "беседа"}, key="задача-1")
        assert served["state"] == "resumed"
        redo = worker.run({"user": "первый", "session": "беседа"}, key="задача-1", retry=True)
        assert redo["state"] == "in_progress"          # а повтор — это новый ход, ждёт
    finally:
        let_go(busy)


def test_unlimited_credits_keep_the_worker_going(tmp_path, profile):
    guard = Guard(FakeAdapter(tmp_path), profile, QUIET)
    spent = Limits("fake", "t", (Window("primary", 100.0),), datetime.now(UTC), "t", True,
                   credits=None, credits_unlimited=True)
    assert guard.blocked(spent) is None
    assert guard.blocked(limits(100.0)) == "Окно выбрано на 100%"


def test_stubborn_grandchild_is_killed_after_the_leader_exits(tmp_path):
    """Лидер вышел по SIGTERM сразу, внук сигнал проигнорировал — его снимает SIGKILL."""
    if os.name == "nt":
        pytest.skip("группы процессов POSIX")
    import time

    from agent_workers.base.process import supervise

    pid_file = tmp_path / "grandchild.pid"
    command = Command((sys.executable, str(Path(__file__).with_name("stubborn_child.py")),
                       str(pid_file)), dict(os.environ), tmp_path)
    supervise(command, stdout=tmp_path / "out", stderr=tmp_path / "err", timeout=1.0)
    grandchild = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(80):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        status = Path(f"/proc/{grandchild}/status")
        if status.is_file() and "State:" + chr(9) + "Z" in status.read_text():
            break
        time.sleep(0.1)
    else:
        pytest.fail("упрямый внук пережил снятие хода")


def test_crash_between_answer_and_advance_is_healed_on_next_read(tmp_path, profile):
    """Ответ записан, номер беседы — нет: следующее чтение доделает, а не заморозит."""
    worker = Worker(FakeAdapter(tmp_path, percent=[1.0] * 10), profile, tmp_path / "runs", QUIET)
    worker.run({"user": "продолжай", "session": "беседа"})
    assert worker.generation("беседа") == 1
    worker.session_file("беседа", "gen").write_text("0", encoding="utf-8")   # как будто упали

    healed = worker.run({"user": "продолжай", "session": "беседа"})
    assert healed["state"] == "resumed"                # тот же ключ — тот же ответ
    assert worker.generation("беседа") == 1            # и номер восстановлен из папки
    assert worker.run({"user": "продолжай", "session": "беседа"})["state"] == "answered"


def test_no_stray_folder_when_the_conversation_is_busy(tmp_path, profile):
    from agent_workers.base.entry import hold, let_go

    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET)
    busy = hold(worker.session_file("беседа", "lock"))
    try:
        blocked = worker.run({"user": "новый вопрос", "session": "беседа"})
    finally:
        let_go(busy)
    assert blocked["state"] == "in_progress"
    assert not blocked["entry"].folder.exists()        # папку завели зря — убрали


def test_retry_keeps_the_old_result_when_preflight_fails(tmp_path, profile):
    """Отказ до запуска не должен стирать результат, который ещё можно доставить."""
    class LoggedOut(FakeAdapter):
        def verify(self, captured):
            raise RuntimeError("выхода из учётной записи")

    adapter = LoggedOut(tmp_path, percent=[1.0, 1.0, 99.0])
    worker = Worker(adapter, profile, tmp_path / "runs", QUIET)
    first = worker.run({"user": "вопрос"}, key="задача")
    assert first["state"] == "answered"

    with pytest.raises(RuntimeError):                  # вход не подтверждён
        worker.run({"user": "вопрос"}, key="задача", retry=True, ensure_login=True)
    refused = worker.run({"user": "вопрос"}, key="задача", retry=True)   # лимит 99%
    assert refused["state"] == "limit_reached"

    kept = worker.run({"user": "вопрос"}, key="задача")
    assert kept["state"] == "resumed" and kept["reply"].text == "вопрос"
    assert not (first["entry"].folder / "attempt-1").exists()   # архив не заводился


def test_shutdown_requested_before_launch_does_not_start_a_turn(tmp_path, profile):
    adapter = FakeAdapter(tmp_path)
    worker = Worker(adapter, profile, tmp_path / "runs", QUIET)
    result = worker.run({"user": "вопрос"}, stop=lambda: True)
    assert result["state"] == "aborted"
    assert adapter.asked == []                          # CLI не запускалась
    assert worker.outbox.occupied() == 0                # бронь снята


def test_broken_reply_parsing_is_an_incomplete_result_not_a_crash(tmp_path, profile):
    class Unparseable(FakeAdapter):
        def reply(self, entry, profile):
            raise ValueError("формат вывода изменился")

    worker = Worker(Unparseable(tmp_path), profile, tmp_path / "runs", QUIET)
    result = worker.run({"user": "вопрос"})
    assert result["state"] == "incomplete"
    assert "reply_error" in result["reply"].diagnostic
    again = worker.run({"user": "вопрос"})              # папка не роняет читающих
    assert again["state"] == "incomplete" and "retry" in again["reason"]


def test_failed_login_check_leaves_no_hidden_folder_for_a_new_request(tmp_path, profile):
    class LoggedOut(FakeAdapter):
        def verify(self, captured):
            raise RuntimeError("выхода из учётной записи")

    worker = Worker(LoggedOut(tmp_path), profile, tmp_path / "runs", QUIET)
    with pytest.raises(RuntimeError):
        worker.run({"user": "вопрос"}, ensure_login=True)
    assert not any((tmp_path / "runs").glob("[!.]*"))      # ни одной папки хода


def test_first_time_retry_still_reserves_a_slot(tmp_path, profile):
    """retry по ключу без прошлой попытки — обычный первый ход, с бронью и уборкой."""
    from agent_workers.base.outbox import Outbox

    class Peeking(FakeAdapter):
        seen: list = []

        def limits(self, profile, *, session=None, model=None):
            self.seen.append(Outbox(tmp_path / "runs").occupied())
            return super().limits(profile, session=session, model=model)

    adapter = Peeking(tmp_path, percent=[1.0, 1.0])
    worker = Worker(adapter, profile, tmp_path / "runs", QUIET)
    result = worker.run({"user": "вопрос"}, key="новая", retry=True)
    assert result["state"] == "answered"
    assert adapter.seen[0] == 1                       # во время замера место уже занято
    assert not (result["entry"].folder / "attempt-1").exists()   # архивировать было нечего


def test_generation_never_moves_backwards(tmp_path, profile):
    worker = Worker(FakeAdapter(tmp_path), profile, tmp_path / "runs", QUIET)
    worker.advance("беседа", 2)
    worker.advance("беседа", 1)                       # запоздалое восстановление
    assert worker.generation("беседа") == 2
    assert worker.session_file("беседа", "gen.lock").exists()   # обновление шло под замком

