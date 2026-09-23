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
from agent_workers.base.entry import Entry, NotRemoved, folder_for

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

    def limits(self, profile, *, session=None, model=None):
        self.reads += 1
        return limits(self.percent.pop(0) if self.percent else 0.0)


def worker_at(tmp_path, profile, adapter=None, policy=QUIET):
    return Worker(adapter or FakeAdapter(tmp_path), profile, tmp_path / "runs", policy)


def test_folder_is_owned_by_one_process_at_a_time(tmp_path):
    entry = Entry(tmp_path, "run")
    assert entry.claim() is True
    assert Entry(tmp_path, "run").claim() is False   # замок держит первый
    entry.release()
    assert Entry(tmp_path, "run").claim() is True    # отпустил — можно брать


def test_folder_lock_is_exclusive_between_processes(tmp_path):
    """Замок должен держать чужой процесс, а не только собственный поток."""
    import subprocess

    probe = Path(__file__).with_name("probe_lock.py")
    package = Path(__file__).resolve().parents[2]
    owner = Entry(tmp_path, "ход")

    def ask() -> str:
        done = subprocess.run([sys.executable, str(probe), str(owner.folder / "owner.lock")],
                              capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": str(package)})
        return done.stdout.strip()

    owner.claim()
    try:
        assert ask() == "held"
    finally:
        owner.release()
    assert ask() == "free"


def test_key_cannot_point_outside_the_folder(tmp_path):
    """Ключ приходит снаружи, а папку сносят рекурсивно: путь в ключе недопустим."""
    for key in ("..", ".", "", "../соседняя", "/abs", "C:/windows", "вложенный/путь"):
        with pytest.raises(ValueError):
            folder_for(tmp_path, key)
    assert folder_for(tmp_path, "обычный-ключ") == (tmp_path / "обычный-ключ").resolve()


def test_key_is_required(tmp_path, profile):
    """Ключ — идентификатор задачи. Выводить его из текста запроса воркер не берётся."""
    with pytest.raises(TypeError):
        worker_at(tmp_path, profile).run({"user": "привет"})


def test_collect_refuses_a_key_that_is_a_path(tmp_path, profile):
    worker = worker_at(tmp_path, profile)
    victim = tmp_path / "runs"
    victim.mkdir(parents=True)
    (victim / "чужое.txt").write_text("не трогать", encoding="utf-8")
    for key in ("..", "../runs", "/tmp"):
        with pytest.raises(ValueError):
            worker.collect(key)
    assert (victim / "чужое.txt").exists()


def test_collect_refuses_an_unknown_key(tmp_path, profile):
    with pytest.raises(ValueError, match="нет"):
        worker_at(tmp_path, profile).collect("никогда-не-существовавший")


def test_worker_measures_before_and_after_and_reports_the_price(tmp_path, profile):
    result = worker_at(tmp_path, profile).run({"user": "привет"}, key="задача")
    assert result["state"] == "answered"
    assert result["reply"].text == "привет"
    assert result["before"].worst == 10.0 and result["after"].worst == 12.5
    assert result["spent"] == {"window": 2.5}
    # Рядом с ценой в процентах окна — во что тот же ход обошёлся бы по API.
    assert result["tokens"].output == 1 and str(result["cost"].amount) == "0.01"


def test_worker_refuses_before_spending_anything(tmp_path, profile):
    adapter = FakeAdapter(tmp_path, percent=[97.0])
    result = worker_at(tmp_path, profile, adapter).run({"user": "привет"}, key="задача")
    assert result["state"] == "limit_reached"
    assert "97" in result["reason"]
    assert adapter.asked == []            # запрос не собирался
    assert result["after"] is None


def test_ready_answer_is_served_for_free(tmp_path, profile):
    """Готовый ответ уже оплачен: ни замера, ни сборки запроса, ни запуска."""
    adapter = FakeAdapter(tmp_path, percent=[1.0, 2.0])
    worker = worker_at(tmp_path, profile, adapter)
    worker.run({"user": "привет"}, key="задача")
    reads, asked = adapter.reads, len(adapter.asked)

    again = worker.run({"user": "привет"}, key="задача")
    assert again["state"] == "resumed" and again["reply"].text == "привет"
    assert (adapter.reads, len(adapter.asked)) == (reads, asked)
    assert again["before"] is None and again["spent"] == {}


def test_same_text_under_another_key_is_a_new_turn(tmp_path, profile):
    """Одинаковый текст — ещё не та же задача: совпадений по тексту больше нет."""
    adapter = FakeAdapter(tmp_path, percent=[1.0] * 4)
    worker = worker_at(tmp_path, profile, adapter)
    first = worker.run({"user": "привет"}, key="задача-1")
    second = worker.run({"user": "привет"}, key="задача-2")
    assert (first["state"], second["state"]) == ("answered", "answered")
    assert first["entry"].folder != second["entry"].folder
    assert len(adapter.asked) == 2


def test_exhausted_window_does_not_hide_a_ready_answer(tmp_path, profile):
    """Иначе выбранный лимит стирал бы уже полученный ответ отказом."""
    entry = worker_at(tmp_path, profile).run({"user": "привет"}, key="задача")["entry"]

    strict = worker_at(tmp_path, profile, FakeAdapter(tmp_path, percent=[99.0]))
    served = strict.run({"user": "привет"}, key="задача")
    assert served["state"] == "resumed"
    assert entry.meta["state"] == "answered"     # отказ не затирает состояние папки


def test_cached_reply_is_served_even_when_login_cannot_be_checked(tmp_path, profile):
    class LoggedOut(FakeAdapter):
        def verify(self, captured):
            raise RuntimeError("выхода из учётной записи")

    worker = worker_at(tmp_path, profile, LoggedOut(tmp_path))
    worker.run({"user": "привет"}, key="задача")                        # ответ уже в лотке
    served = worker.run({"user": "привет"}, key="задача", ensure_login=True)
    assert served["state"] == "resumed"                                 # вход не понадобился
    with pytest.raises(RuntimeError, match="вход не подтверждён"):
        worker.run({"user": "новый вопрос"}, key="другая", ensure_login=True)   # а тут нужен


def test_answer_written_before_a_crash_is_not_paid_twice(tmp_path, profile):
    """CLI ответила, а процесс умер до записи состояния: ответ лежит в журнале."""
    adapter = FakeAdapter(tmp_path)
    worker = worker_at(tmp_path, profile, adapter)
    entry = Entry(tmp_path / "runs", "задача")
    entry.write("stdout.jsonl", "привет")
    entry.update(state="running", pid=999_999_999)     # владельца нет: замок никто не держит

    served = worker.run({"user": "привет"}, key="задача")
    assert served["state"] == "resumed" and served["reply"].text == "привет"
    assert adapter.asked == []                          # второй раз не платили
    assert entry.meta["state"] == "answered"


def test_abandoned_attempt_is_run_again(tmp_path, profile):
    """Задачу выдали снова — значит, повтор нужен. Сколько повторять, считает координатор."""
    adapter = FakeAdapter(tmp_path)
    entry = Entry(tmp_path / "runs", "задача")
    entry.write("stdout.jsonl", "")
    entry.update(state="running", pid=999_999_999)

    again = worker_at(tmp_path, profile, adapter).run({"user": "привет"}, key="задача")
    assert again["state"] == "answered" and again["reply"].text == "привет"
    assert len(adapter.asked) == 1


def test_new_attempt_starts_from_a_clean_folder(tmp_path, profile):
    """Журналы прошлой попытки не должны смешаться с новыми."""
    entry = Entry(tmp_path / "runs", "задача")
    entry.write("stdout.jsonl", "")
    entry.write("summary.txt", "итог прошлой попытки")
    entry.update(state="incomplete", diagnostic="timeout")

    again = worker_at(tmp_path, profile).run({"user": "привет"}, key="задача")
    assert again["state"] == "answered"
    assert not (entry.folder / "summary.txt").exists()
    assert entry.meta.get("diagnostic") is None


def test_live_owner_is_left_alone(tmp_path, profile):
    """Иначе второй вызов оплатил бы ту же работу, а журналы писали бы двое."""
    adapter = FakeAdapter(tmp_path)
    owner = Entry(tmp_path / "runs", "задача")
    owner.claim()
    owner.write("stdout.jsonl", "идущая работа")
    try:
        refused = worker_at(tmp_path, profile, adapter).run({"user": "привет"}, key="задача")
    finally:
        owner.release()
    assert refused["state"] == "in_progress"
    assert adapter.asked == []                                   # второго вызова не было
    assert owner.read("stdout.jsonl") == "идущая работа"          # журнал на месте


def test_result_waits_in_the_outbox_until_it_is_collected(tmp_path, profile):
    """Папка живёт не по таймеру, а пока результат не заберут."""
    worker = worker_at(tmp_path, profile)
    result = worker.run({"user": "привет"}, key="задача")

    waiting = worker.pending()
    assert [entry.folder for entry in waiting] == [result["entry"].folder]
    assert waiting[0].outcome == "answered"

    worker.collect(waiting[0])
    assert worker.pending() == []
    assert not result["entry"].folder.exists()


def test_collected_answer_is_no_longer_served_for_free(tmp_path, profile):
    worker = worker_at(tmp_path, profile, FakeAdapter(tmp_path, percent=[1.0] * 4))
    worker.collect(worker.run({"user": "привет"}, key="задача")["entry"])
    assert worker.run({"user": "привет"}, key="задача")["state"] == "answered"   # заново


def test_refused_turn_leaves_no_folder_behind(tmp_path, profile):
    worker = worker_at(tmp_path, profile, FakeAdapter(tmp_path, percent=[99.0]))
    refused = worker.run({"user": "привет"}, key="задача")
    assert refused["state"] == "limit_reached"
    assert not refused["entry"].folder.exists()
    assert worker.pending() == []


def test_failed_login_check_leaves_no_folder_behind(tmp_path, profile):
    class LoggedOut(FakeAdapter):
        def verify(self, captured):
            raise RuntimeError("выхода из учётной записи")

    worker = worker_at(tmp_path, profile, LoggedOut(tmp_path))
    with pytest.raises(RuntimeError):
        worker.run({"user": "вопрос"}, key="задача", ensure_login=True)
    assert not any((tmp_path / "runs").iterdir())


def test_broken_request_leaves_no_folder_behind(tmp_path, profile):
    class Picky(FakeAdapter):
        def ask(self, entry, request, profile):
            raise ValueError("нет обязательного поля")

    worker = worker_at(tmp_path, profile, Picky(tmp_path))
    with pytest.raises(ValueError):
        worker.run({"user": "плохое"}, key="задача")
    assert worker.pending() == [] and not any((tmp_path / "runs").iterdir())


def test_interrupt_while_building_the_request_leaves_no_folder_behind(tmp_path, profile):
    class Interrupted(FakeAdapter):
        def ask(self, entry, request, profile):
            raise KeyboardInterrupt

    worker = worker_at(tmp_path, profile, Interrupted(tmp_path))
    with pytest.raises(KeyboardInterrupt):
        worker.run({"user": "раз"}, key="задача")
    assert worker.pending() == []


def test_shutdown_requested_before_launch_does_not_start_a_turn(tmp_path, profile):
    adapter = FakeAdapter(tmp_path)
    result = worker_at(tmp_path, profile, adapter).run({"user": "вопрос"}, key="задача",
                                                       stop=lambda: True)
    assert result["state"] == "aborted"
    assert adapter.asked == []                          # CLI не запускалась
    assert not result["entry"].folder.exists()


def test_interrupted_turn_ends_up_in_the_outbox_not_in_limbo(tmp_path, profile):
    """Ctrl+C посреди хода: папка становится «незавершённой», её видно и можно забрать."""
    import agent_workers.base.worker as worker_module

    keep = worker_module.supervise

    def broken_supervise(*args, **kwargs):
        raise KeyboardInterrupt

    worker_module.supervise = broken_supervise
    worker = worker_at(tmp_path, profile)
    try:
        with pytest.raises(KeyboardInterrupt):
            worker.run({"user": "два"}, key="задача")
    finally:
        worker_module.supervise = keep

    stuck = worker.pending()
    assert len(stuck) == 1 and stuck[0].meta["state"] == "incomplete"
    worker.collect(stuck[0])
    assert worker.pending() == []


def test_abandoned_folder_is_visible_and_collectable(tmp_path, profile):
    """Папка без владельца — итог, каким бы он ни был. Невидимых папок не бывает."""
    worker = worker_at(tmp_path, profile)
    crashed = Entry(tmp_path / "runs", "упал-посреди-хода")
    crashed.write("stdout.jsonl", "")
    crashed.update(state="running")
    half = Entry(tmp_path / "runs", "упал-до-состояния")
    half.claim()                                   # замок заводится первым делом
    half.write("invocation/input.txt", "задание")
    half.release()

    waiting = {entry.folder.name: entry.outcome for entry in worker.pending()}
    assert waiting == {"упал-посреди-хода": "incomplete", "упал-до-состояния": "incomplete"}
    for entry in worker.pending():
        worker.collect(entry)
    assert not any((tmp_path / "runs").iterdir())


def test_foreign_directories_are_never_taken_for_entries(tmp_path, profile):
    """Каталог ходов по ошибке совпал с каталогом учётки: её содержимое — не ходы."""
    worker = worker_at(tmp_path, profile)
    sessions = tmp_path / "runs" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "токен.json").write_text("секрет", encoding="utf-8")

    assert worker.pending() == []
    with pytest.raises(ValueError, match="нет"):
        worker.collect("sessions")
    assert (sessions / "токен.json").exists()


def test_broken_reply_parsing_is_an_incomplete_result_not_a_crash(tmp_path, profile):
    class Unparseable(FakeAdapter):
        def reply(self, entry, profile):
            raise ValueError("формат вывода изменился")

    worker = worker_at(tmp_path, profile, Unparseable(tmp_path, percent=[1.0] * 4))
    result = worker.run({"user": "вопрос"}, key="задача")
    assert result["state"] == "incomplete"
    assert "reply_error" in result["reply"].diagnostic
    again = worker.run({"user": "вопрос"}, key="задача")   # папка не роняет читающих
    assert again["state"] == "incomplete"


def test_running_entry_cannot_be_collected(tmp_path, profile):
    """Снести папку идущего хода — значит выдернуть у него журналы и замок из-под ног."""
    worker = worker_at(tmp_path, profile)
    owner = Entry(tmp_path / "runs", "живой-ход")
    owner.claim()
    owner.update(state="running")
    try:
        assert worker.pending() == []                 # идущий ход — не итог
        with pytest.raises(RuntimeError, match="выполняется"):
            worker.collect("живой-ход")
    finally:
        owner.release()
    assert owner.folder.exists()
    worker.collect("живой-ход")          # владелец ушёл — теперь можно
    assert not owner.folder.exists()


def test_new_entry_writes_nothing_before_it_runs(tmp_path):
    entry = Entry(tmp_path, "ход")
    assert not entry.state_path.exists()
    assert entry.meta == {}


def test_latecomer_does_not_overwrite_the_live_state(tmp_path):
    owner = Entry(tmp_path, "ход")
    owner.claim()
    owner.update(state="running")

    latecomer = Entry(tmp_path, "ход")          # второй процесс на том же ключе
    assert latecomer.claim() is False           # владения не получил
    assert latecomer.meta["state"] == "running"  # и ничего не переписал
    owner.release()


def test_failed_removal_is_reported_not_swallowed(tmp_path, profile):
    """Недоснесённая папка снова всплывёт в лотке — молчать об этом нельзя."""
    import agent_workers.base.entry as entry_module

    worker = worker_at(tmp_path, profile)
    entry = Entry(tmp_path / "runs", "ход")
    entry.update(state="answered")

    original = entry_module.shutil.rmtree
    entry_module.shutil.rmtree = lambda *a, **k: None    # как будто удалить не вышло
    try:
        with pytest.raises(NotRemoved):
            worker.collect("ход")
    finally:
        entry_module.shutil.rmtree = original
    assert entry.folder.exists()


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


def test_unlimited_credits_keep_the_worker_going(tmp_path, profile):
    guard = Guard(FakeAdapter(tmp_path), profile, QUIET)
    spent = Limits("fake", "t", (Window("primary", 100.0),), datetime.now(UTC), "t", True,
                   credits=None, credits_unlimited=True)
    assert guard.blocked(spent) is None
    assert guard.blocked(limits(100.0)) == "Окно выбрано на 100%"


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

    result = worker_at(tmp_path, profile, Broken(tmp_path)).run({"user": "привет"}, key="задача")
    assert result["state"] == "answered" and result["before"] is None


def test_disabled_measurement_is_not_an_unknown_quota(tmp_path, profile):
    """Выключенный замер — решение вызывающего, а не повод всё запретить."""
    policy = LimitPolicy(before=False, after=False, on_unknown="refuse")
    result = worker_at(tmp_path, profile, policy=policy).run({"user": "ок"}, key="задача")
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
    worker_at(tmp_path, profile, adapter).run({"user": "ок", "model": "другая"}, key="задача")
    assert adapter.asked_models and set(adapter.asked_models) == {"другая"}


def test_no_measurement_after_a_requested_stop(tmp_path, profile):
    """Нас попросили остановиться — не тянем выход паузами и запусками CLI."""
    import agent_workers.base.worker as worker_module
    from agent_workers.base.process import Outcome

    keep = worker_module.supervise
    worker_module.supervise = lambda *a, **k: Outcome(None, "stopped")
    adapter = FakeAdapter(tmp_path, percent=[1.0, 2.0, 3.0])
    try:
        result = worker_at(tmp_path, profile, adapter).run({"user": "ок"}, key="задача")
    finally:
        worker_module.supervise = keep
    assert result["after"] is None and adapter.reads == 1


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


def gone(pid: int, *, tries: int = 80) -> bool:
    """Процесс исчез. В контейнере без init убитый внук остаётся зомби: PID ещё есть,
    а процесса нет — это тоже «исчез»."""
    import time

    for _ in range(tries):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        status = Path(f"/proc/{pid}/status")
        if status.is_file() and "State:" + chr(9) + "Z" in status.read_text():
            return True
        time.sleep(0.1)
    return False


def supervised(tmp_path, script: str, **options):
    """Запустить подпроцесс из tests/ под надзором; вернуть исход и PID его внука."""
    from agent_workers.base.process import supervise

    pid_file = tmp_path / "grandchild.pid"
    command = Command((sys.executable, str(Path(__file__).with_name(script)), str(pid_file)),
                      dict(os.environ), tmp_path)
    outcome = supervise(command, stdout=tmp_path / "out", stderr=tmp_path / "err", **options)
    return outcome, int(pid_file.read_text(encoding="utf-8"))


POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="группы процессов POSIX; боевой запуск — в докере на Linux")


@POSIX_ONLY
def test_stopping_a_turn_kills_its_grandchildren(tmp_path):
    """Иначе внук CLI переживёт ход и продолжит писать в папку, которую уже забирают."""
    outcome, grandchild = supervised(tmp_path, "spawning_child.py", timeout=1.0)
    assert outcome.interruption == "timeout"
    assert gone(grandchild), "внук пережил снятие хода"


@POSIX_ONLY
def test_stubborn_grandchild_is_killed_after_the_leader_exits(tmp_path):
    """Лидер вышел по SIGTERM сразу, внук сигнал проигнорировал — его снимает SIGKILL."""
    _, grandchild = supervised(tmp_path, "stubborn_child.py", timeout=1.0)
    assert gone(grandchild), "упрямый внук пережил снятие хода"


@POSIX_ONLY
def test_background_grandchild_is_reaped_after_a_normal_exit(tmp_path):
    """CLI вышла сама, а её фоновый потомок остался: после хода в группе никого нет."""
    outcome, grandchild = supervised(tmp_path, "leaving_child.py", timeout=30)
    assert outcome.interruption is None
    assert outcome.returncode == 0
    assert gone(grandchild), "фоновый внук пережил ход"   # сам он спит минуту
