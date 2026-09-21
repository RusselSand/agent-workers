"""Единственное место, где живёт subprocess: запуск, надзор, добивание."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .contract import Command


def hidden() -> dict:
    # Иначе каждый ход мигает консольным окном поверх работы пользователя.
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


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
    stdin = command.stdin.open("rb") if command.stdin else subprocess.DEVNULL
    interruption = None
    try:
        with stdout.open("ab", buffering=0) as out, stderr.open("ab", buffering=0) as err:
            process = subprocess.Popen(command.argv, stdin=stdin, stdout=out, stderr=err,
                                       cwd=command.cwd, env=dict(command.env), **hidden())
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
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    interruption = interruption or "interrupted"
                os.fsync(out.fileno())
                os.fsync(err.fileno())
    finally:
        if command.stdin:
            stdin.close()
    return Outcome(process.returncode, interruption)
