"""Единственное место, где живёт subprocess: запуск, надзор, добивание."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .contract import Command


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


@dataclass(frozen=True)
class Outcome:
    returncode: int | None
    interruption: str | None  # stopped | timeout | interrupted | None


def capture(command: Command) -> str:
    """Короткая команда: проверка входа, зонд лимитов. Вывод возвращаем целиком."""
    result = subprocess.run(command.argv, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=command.timeout or 60, env=dict(command.env),
                            cwd=command.cwd, **hidden())
    return result.stdout + result.stderr


def interactive(command: Command) -> int:
    """Консоль отдаём подпроцессу: вход в учётную запись идёт через браузер и вопросы."""
    return subprocess.run(command.argv, env=dict(command.env), cwd=command.cwd).returncode


def supervise(command: Command, *, stdout: Path, stderr: Path, pulse=None, stop=None,
              timeout: float = 1200, sync_every: float = 20) -> Outcome:
    """Длинный ход. Журналы только дозаписываются, чтобы обрыв не уносил уже полученное."""
    pulse = pulse or (lambda: None)
    stop = stop or (lambda: False)
    for journal in (stdout, stderr):
        journal.touch(mode=0o600, exist_ok=True)
    stdin = command.stdin.open("rb") if command.stdin else subprocess.DEVNULL
    interruption = None
    try:
        with stdout.open("ab", buffering=0) as out, stderr.open("ab", buffering=0) as err:
            process = subprocess.Popen(command.argv, stdin=stdin, stdout=out, stderr=err,
                                       cwd=command.cwd, env=dict(command.env), **detached())
            started = last_sync = time.monotonic()
            try:
                while process.poll() is None:
                    now = time.monotonic()
                    if stop() or now - started >= timeout:
                        interruption = "stopped" if stop() else "timeout"
                        break
                    if now - last_sync >= sync_every:
                        os.fsync(out.fileno())
                        os.fsync(err.fileno())
                        pulse()
                        last_sync = now
                    time.sleep(0.2)
            finally:
                if process.poll() is None:
                    interruption = interruption or "interrupted"
                # И после обычного выхода: в группе могли остаться фоновые потомки.
                terminate_tree(process)
                os.fsync(out.fileno())
                os.fsync(err.fileno())
    finally:
        if command.stdin:
            stdin.close()
    return Outcome(process.returncode, interruption)
