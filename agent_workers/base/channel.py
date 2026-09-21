"""Построчный диалог с подпроцессом: сообщение туда, JSON-строки обратно.

Нужен там, где ответ получается не одним запуском, а обменом репликами по stdio.
Как и process.py, это механика: что именно посылать, знает только провайдер.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Mapping
from queue import Empty, Queue
from threading import Thread

from .contract import Command
from .process import hidden


class ChannelError(RuntimeError):
    pass


class Channel:
    """Одна сессия обмена. Закрывается вместе с процессом — в том числе при ошибке."""

    def __init__(self, command: Command, *, timeout: float = 25) -> None:
        self.timeout = command.timeout or timeout
        self.process = subprocess.Popen(
            command.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            cwd=command.cwd, env=dict(command.env), **hidden())
        self.events: Queue = Queue()
        Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.process.stdout:
            try:
                self.events.put(json.loads(line))
            except ValueError:
                continue  # Диагностика и прогресс не в JSON нас не касаются.
        self.events.put(None)

    def send(self, message: Mapping) -> None:
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def wait(self, match, *, timeout: float | None = None) -> dict:
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            # Болтливый процесс шлёт уведомления без конца: срок истекает и при них.
            if time.monotonic() >= deadline:
                raise ChannelError("Подпроцесс не ответил вовремя")
            try:
                event = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            except Empty:
                raise ChannelError("Подпроцесс не ответил вовремя") from None
            if event is None:
                raise ChannelError("Подпроцесс завершился до ответа")
            if match(event):
                return event

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout):
            if stream and not stream.closed:
                stream.close()

    def __enter__(self) -> Channel:
        return self

    def __exit__(self, *_) -> None:
        self.close()
