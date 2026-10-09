"""Единственное место, где живёт subprocess: запуск, надзор, добивание."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .contract import Command

# Пределы хода по умолчанию, в секундах. Общий — страховка: ход, который пишет в журналы,
# может идти долго, например читая большой репозиторий. Зависшая CLI молчит, и её снимает
# предел молчания. Он с запасом, потому что без пословного вывода модель может долго молчать,
# пока думает или пишет большой ответ.
TURN_TIMEOUT = 3600.0
IDLE_TIMEOUT = 900.0


def hidden() -> dict:
    # Иначе каждый ход мигает консольным окном поверх работы пользователя.
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def detached() -> dict:
    """Ход запускается своей группой процессов: тогда его можно снять целиком.

    CLI сама порождает потомков — оболочки, инструменты. Убить только её значит
    оставить их жить с открытыми журналами хода, который мы уже считаем законченным.
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_tree(process: subprocess.Popen) -> None:
    """Снять процесс со всеми потомками: сначала вежливо, потом насильно.

    На Linux снимается вся группа, даже если лидер уже вышел сам: фоновый потомок
    иначе пережил бы ход. На Windows сироту после выхода лидера не найти — это
    известное ограничение, боевой запуск идёт в докере на Linux.
    """
    if os.name == "nt":
        if process.poll() is not None:
            return
        # Дерево целиком умеет снимать только taskkill; аналог Job Object без ctypes нет.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                       capture_output=True, **hidden())
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return
    if process.poll() is not None and not group_alive(process.pid):
        return   # лидер вышел и никого после себя не оставил
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    # Ждём не лидера, а всю группу: лидер может выйти сразу, а потомок — не заметить
    # сигнала и остаться с открытыми журналами хода.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None and not group_alive(process.pid):
            return
        time.sleep(0.1)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


class LaunchError(OSError):
    """CLI не запустилась: ход не начинался и ничего не стоил."""


@dataclass(frozen=True)
class Outcome:
    returncode: int | None
    interruption: str | None  # stopped | timeout | idle | interrupted | None


def seconds(value: float | None, what: str) -> float | None:
    """Предел в секундах или None, если предела нет. Ноль и меньше снимали бы каждый ход сразу
    после запуска, то есть уже оплаченным, а nan молча выключал бы предел, — это ошибка."""
    if value is None:
        return None
    if isinstance(value, bool) or not (math.isfinite(value) and value > 0):
        raise ValueError(f"{what} — секунды больше нуля или None: {value!r}")
    return float(value)


def written(*journals) -> int:
    return sum(os.fstat(journal.fileno()).st_size for journal in journals)


def capture(command: Command) -> str:
    """Короткая команда: проверка входа, зонд лимитов. Вывод возвращаем целиком."""
    result = subprocess.run(command.argv, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=command.timeout or 60, env=dict(command.env),
                            cwd=command.cwd, **hidden())
    return result.stdout + result.stderr


def interactive(command: Command) -> int:
    """Консоль отдаём подпроцессу: вход в учётную запись идёт через браузер и вопросы."""
    return subprocess.run(command.argv, env=dict(command.env), cwd=command.cwd).returncode


def launch(command: Command, stdin, out, err) -> subprocess.Popen:
    """Запуск хода своей группой процессов.

    Файла нет, нет прав на запуск, система не даёт создать процесс — это LaunchError,
    а не оборванный ход: процесс не стартовал, и платить было не за что.
    """
    try:
        return subprocess.Popen(command.argv, stdin=stdin, stdout=out, stderr=err,
                                cwd=command.cwd, env=dict(command.env), **detached())
    except OSError as exc:
        raise LaunchError(f"CLI не запустилась: {exc}") from exc


def supervise(command: Command, *, stdout: Path, stderr: Path, stop=None, on_start=None,
              timeout: float | None = TURN_TIMEOUT, idle: float | None = IDLE_TIMEOUT,
              sync_every: float = 20) -> Outcome:
    """Длинный ход. Журналы только дозаписываются, чтобы обрыв не уносил уже полученное.

    on_start зовётся ровно тогда, когда процесс уже запущен: всё, что сломалось раньше, —
    журналы, stdin, сам запуск — случилось до хода, и платить там было не за что.

    timeout — предел всего хода, idle — предел молчания: ни строки ни в один из журналов.
    None снимает предел.
    """
    stop = stop or (lambda: False)
    on_start = on_start or (lambda: None)
    for journal in (stdout, stderr):
        journal.touch(mode=0o600, exist_ok=True)
    stdin = command.stdin.open("rb") if command.stdin else subprocess.DEVNULL
    interruption = None
    try:
        with stdout.open("ab", buffering=0) as out, stderr.open("ab", buffering=0) as err:
            process = launch(command, stdin, out, err)
            try:
                on_start()   # внутри try: сорвётся отметка — процесс всё равно снимем
                interruption = watch(process, out, err, stop=stop, timeout=timeout, idle=idle,
                                     sync_every=sync_every)
            finally:
                if process.poll() is None:
                    interruption = interruption or "interrupted"
                # И после обычного выхода: в группе могли остаться фоновые потомки.
                terminate_tree(process)
                sync(out, err)
    finally:
        if command.stdin:
            stdin.close()
    return Outcome(process.returncode, interruption)


def watch(process: subprocess.Popen, out, err, *, stop, timeout: float | None,
          idle: float | None, sync_every: float) -> str | None:
    """Ждать, пока процесс выйдет сам. Вернёт, почему его пора снять, или None, если вышел."""
    started = last_sync = last_output = time.monotonic()
    size = written(out, err)
    while process.poll() is None:
        now = time.monotonic()
        if (grown := written(out, err)) != size:
            size, last_output = grown, now
        reason = overdue(stop, now - started, timeout, now - last_output, idle)
        if reason:
            return reason
        if now - last_sync >= sync_every:
            sync(out, err)
            last_sync = now
        time.sleep(0.2)
    return None


def overdue(stop, running: float, timeout: float | None, silent: float,
            idle: float | None) -> str | None:
    """Пора ли снимать ход: попросили остановиться, вышел предел хода или молчания."""
    if stop():
        return "stopped"
    if timeout is not None and running >= timeout:
        return "timeout"
    if idle is not None and silent >= idle:
        return "idle"
    return None


def sync(*journals) -> None:
    for journal in journals:
        os.fsync(journal.fileno())
