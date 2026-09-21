"""Команды одного воркера: войти, выйти, посмотреть подписку, сделать ход.

Каждая команда работает с тем единственным подключением, которое названо в .env или
флагами, и первой же строкой печатает, с каким именно, — гадать не приходится.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .base.entry import Entry, digest
from .base.worker import Worker
from .build import ADAPTERS, build
from .config import Settings


def destination(worker: Worker, settings: Settings) -> None:
    source = settings.path or "файл .env не найден, берутся умолчания"
    print(f"подключение: {worker.adapter.name} · {worker.profile.home}", file=sys.stderr)
    print(f"настройки: {source}", file=sys.stderr)


def status(worker: Worker) -> int:
    try:
        worker.check()
    except Exception as exc:
        print(f"вход не выполнен: {exc}", file=sys.stderr)
        print("почините так: python -m agent_workers login", file=sys.stderr)
        return 2
    limits = worker.guard.measure("before")
    if limits is None:
        print("вход есть, но расход прочитать не удалось", file=sys.stderr)
        return 1
    extra = f" · кредиты {limits.credits}" if limits.credits is not None else ""
    print(f"вход есть · план {limits.plan}{extra} · источник {limits.source}")
    for window in limits.windows:
        when = (window.resets_at.strftime("%d.%m %H:%M") if window.resets_at
                else window.resets_hint or "—")
        print(f"{window.name}: {window.used_percent}% · сброс {when}")
    return 0


def run(worker: Worker, options) -> int:
    """Один ход: задание -> ответ, с расходом подписки и оценкой стоимости."""
    text = sys.stdin.read() if options.prompt == "-" else options.prompt
    request = {"user": text, "model": worker.adapter.model}
    if options.session:
        request["session"] = options.session   # продолжить прежнюю беседу, а не начать новую
    if options.system_file:
        request["system"] = Path(options.system_file).read_text(encoding="utf-8")

    if options.dry_run:
        entry = Entry(worker.root, digest({"adapter": worker.adapter.name, "request": request}))
        command = worker.adapter.ask(entry, request, worker.profile)
        print(f"каталог хода: {entry.folder}")
        print("команда:", " ".join(command.argv))
        print(f"stdin: {command.stdin}")
        print("\nНичего не запущено и не потрачено.")
        return 0

    try:
        worker.check()   # бесплатно и до траты: без входа CLI ответит невнятной ошибкой
    except Exception as exc:
        print(f"{exc}\nпочините так: python -m agent_workers login", file=sys.stderr)
        return 2

    result = worker.run(request)
    if result["state"] == "limit_reached":
        print("ход не начат: " + result["reason"], file=sys.stderr)
        return 3
    print(result["reply"].text)
    report(result)
    return 0 if result["state"] in ("answered", "resumed") else 4


def report(result) -> None:
    """Всё, что не ответ, уходит в stderr: ответ можно спокойно перенаправить в файл."""
    tokens, cost = result["tokens"], result["cost"]
    print(file=sys.stderr)
    if tokens:
        print(f"токены: вход {tokens.input} · из кеша {tokens.cached_input} · "
              f"в кеш {tokens.cache_write} · выход {tokens.output}", file=sys.stderr)
    if cost:
        print(f"по API: ${cost.amount} ({cost.source})", file=sys.stderr)
    before, after = result["before"], result["after"]
    for name, delta in (result["spent"] or {}).items():
        was = next(window.used_percent for window in before.windows if window.name == name)
        now = next(window.used_percent for window in after.windows if window.name == name)
        print(f"окно {name}: {was}% -> {now}% ({delta:+})", file=sys.stderr)
    if result["reply"].diagnostic:
        print(f"замечание: {result['reply'].diagnostic}", file=sys.stderr)
    print(f"папка хода: {result['entry'].folder}", file=sys.stderr)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser("agent_workers", description="Воркер поверх CLI с подпиской")
    commands = root.add_subparsers(dest="command")
    for name, help_text in (("status", "вход и расход подписки"),
                            ("login", "войти в учётную запись"),
                            ("logout", "выйти из учётной записи"),
                            ("run", "один ход: задание -> ответ")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--provider", choices=sorted(ADAPTERS), help="какая CLI")
        command.add_argument("--home", help="каталог учётной записи")
        command.add_argument("--model", help="модель, иначе из .env или умолчание адаптера")
        if name == "run":
            command.add_argument("prompt", help="текст задания, или - чтобы читать со stdin")
            command.add_argument("--system-file", help="файл с системной инструкцией")
            command.add_argument("--session", help="продолжить сессию по её идентификатору")
            command.add_argument("--dry-run", action="store_true",
                                 help="показать команду и ничего не запускать")
    return root


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    if not options.command:
        parser().print_help()
        return 2
    settings = Settings.load().override(provider=options.provider, home=options.home,
                                        model=options.model)
    try:
        worker = build(settings)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    destination(worker, settings)
    if options.command == "login":
        return worker.login()
    if options.command == "logout":
        return worker.logout()
    if options.command == "status":
        return status(worker)
    return run(worker, options)
