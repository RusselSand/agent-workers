"""Цикл опроса с аккуратной остановкой по сигналу."""

from __future__ import annotations

import logging
import signal
import threading

log = logging.getLogger(__name__)


def run_loop(tick, *, once: bool = False, poll_seconds: float = 15, stop=None) -> int:
    """Цикл зовёт tick(stop) и отдаёт ему тот же признак остановки.

    Иначе сигнал, пришедший во время долгого хода, дождался бы только конца хода:
    обработчик заменяет обычное завершение, и процесс висел бы до таймаута.
    """
    stop = stop or threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, lambda *_: stop.set())
    try:
        while not stop.is_set():
            try:
                tick(stop)
            except Exception as exc:
                log.error("Ход не удался (%s); сохранённое состояние остаётся на месте",
                          type(exc).__name__)
                if once:
                    return 1
            if once:
                break
            stop.wait(poll_seconds)
        return 0
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
