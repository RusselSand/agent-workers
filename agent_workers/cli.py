"""Команды одного воркера: войти, выйти, посмотреть подписку, сделать ход.

Каждая команда работает с тем единственным подключением, которое названо в .env или
флагами, и первой же строкой печатает, с каким именно, — гадать не приходится.
"""

from __future__ import annotations

import argparse
import signal
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .base.entry import Busy, Entry, NotRemoved
from .base.outbox import Outbox
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


def pending(outbox: Outbox) -> int:
    """Что уже готово и ждёт получателя."""
    waiting = outbox.pending()
    if not waiting:
        print("лоток пуст")
        return 0
    for entry in waiting:
        print(f"{entry.folder.name}  {entry.outcome}  модель {entry.meta.get('model') or '—'}")
    print()
    print(f"всего {len(waiting)}; забрать: python -m agent_workers collect <ключ|--all>")
    return 0


def collect(outbox: Outbox, options) -> int:
    """Забрали — сносим. Пока не забрали, результат лежит и ждёт."""
    if options.all:
        taken, busy, failed = 0, 0, []
        for entry in outbox.entries():
            try:
                outbox.collect(entry)
                taken += 1
            except Busy:
                busy += 1      # кто-то работает с этой папкой: придём в следующий раз
            except (NotRemoved, OSError) as exc:
                failed.append(str(exc))
        print(f"забрано: {taken}" + (f", занято: {busy}" if busy else ""))
        for message in failed:
            print(message, file=sys.stderr)
        return 1 if failed else 0
    if not options.key:
        print("нужен ключ хода или --all", file=sys.stderr)
        return 2
    try:
        outbox.collect(options.key)
    except (ValueError, Busy, NotRemoved, OSError) as exc:
        print(exc, file=sys.stderr)     # ключ приходит от человека, трассировка ему ни к чему
        return 2
    print(f"забрано: {options.key}")
    return 0


def request_of(worker: Worker, options) -> dict:
    """Запрос из аргументов команды. Путь к инструкции назвал человек: опечатка в нём —
    не повод для трассировки, поэтому ошибка чтения становится отказом в запросе."""
    text = sys.stdin.read() if options.prompt == "-" else options.prompt
    request = {"user": text, "model": worker.adapter.model}
    if options.system_file:
        try:
            request["system"] = Path(options.system_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"не прочитать файл инструкции: {exc}") from None
    return request


def run(worker: Worker, options) -> int:
    """Один ход: задание -> ответ, с расходом подписки и оценкой стоимости."""
    try:
        request = request_of(worker, options)
    except ValueError as exc:
        print(f"запрос отклонён: {exc}", file=sys.stderr)
        return 2
    if options.dry_run:
        return dry_run(worker, request)
    try:
        # У ручного хода нет задачи от координатора: каждый запуск — новый ход.
        with sigterm_as_interrupt():
            result = worker.run(request, key=uuid4().hex, ensure_login=True)
    except ValueError as exc:
        print(f"запрос отклонён: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        print("почините так: python -m agent_workers login", file=sys.stderr)
        return 2
    state = result["state"]
    if state == "limit_reached":
        print("ход не начат: " + result["reason"], file=sys.stderr)
        return 3
    if result["reply"] is not None and result["reply"].text:
        print(result["reply"].text)
        report(result)
    else:
        print(explain(result), file=sys.stderr)
    if state == "answered":
        deliver(worker, result["entry"])
        return 0
    # Ход оборвался: папку оставляем, в журналах видно, что пошло не так.
    print(f"папка хода: {result['entry'].folder}", file=sys.stderr)
    print(f"убрать: python -m agent_workers collect {result['entry'].folder.name}",
          file=sys.stderr)
    return 4


@contextmanager
def sigterm_as_interrupt():
    """SIGTERM на время хода — как Ctrl+C: тогда сработает уборка и CLI провайдера
    будет снята. Иначе Python выйдет молча, а CLI в своей сессии доживёт ход без нас
    и потратит подписку."""
    previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def dry_run(worker: Worker, request: dict) -> int:
    """Пробный ход собирается во временном каталоге и исчезает вместе с ним: ни папка
    ходов, ни диск не копят тексты заданий. Поэтому файлы печатаются, а не их пути."""
    with tempfile.TemporaryDirectory(prefix="agent-dry-run-") as scratch:
        entry = Entry(Path(scratch), "ход")
        command = worker.adapter.ask(entry, request, worker.profile)
        print("команда:", " ".join(command.argv))
        for path in sorted(entry.folder.rglob("*")):
            if path.is_file() and path != entry.state_path:
                mark = " (stdin)" if path == command.stdin else ""
                print(f"\n--- {path.relative_to(entry.folder).as_posix()}{mark}")
                print(path.read_text(encoding="utf-8"))
    print()
    print("Ничего не запущено и не потрачено.")
    return 0


def deliver(worker: Worker, entry: Entry) -> None:
    """Ответ напечатан — значит доставлен, хранить его больше незачем."""
    try:
        worker.collect(entry)
    except (Busy, NotRemoved, OSError) as exc:
        print(f"папку хода убрать не удалось: {exc}", file=sys.stderr)


def explain(result) -> str:
    """Почему ответа нет: причина воркера и диагностика CLI, если она есть."""
    reply = result["reply"]
    parts = [result["reason"], reply.diagnostic if reply else None]
    return "; ".join(part for part in parts if part) or "ход не дал ответа"


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


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser("agent_workers", description="Воркер поверх CLI с подпиской")
    commands = root.add_subparsers(dest="command")
    for name, help_text in (("status", "вход и расход подписки"),
                            ("login", "войти в учётную запись"),
                            ("logout", "выйти из учётной записи"),
                            ("pending", "готовые результаты, которых не забрали"),
                            ("collect", "забрать результат и снести его папку"),
                            ("run", "один ход: задание -> ответ")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--provider", choices=sorted(ADAPTERS), help="какая CLI")
        command.add_argument("--home", help="каталог учётной записи")
        command.add_argument("--model", help="модель, иначе из .env или умолчание адаптера")
        if name == "collect":
            command.add_argument("key", nargs="?", help="ключ хода из pending")
            command.add_argument("--all", action="store_true", help="забрать всё разом")
        if name == "run":
            command.add_argument("prompt", help="текст задания, или - чтобы читать со stdin")
            command.add_argument("--system-file", help="файл с системной инструкцией")
            command.add_argument("--dry-run", action="store_true",
                                 help="показать команду и ничего не запускать")
    return root


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    if not options.command:
        parser().print_help()
        return 2
    try:
        settings = Settings.load().override(provider=options.provider, home=options.home,
                                            model=options.model)
        if options.command in ("pending", "collect"):
            # Разобрать лоток можно и без CLI: она может быть снесена или сломана обновлением.
            outbox = Outbox(settings.runs)
            print(f"лоток: {outbox.root}", file=sys.stderr)
            return pending(outbox) if options.command == "pending" else collect(outbox, options)
        worker = build(settings)
    except (ValueError, RuntimeError) as exc:   # настройки не читаются, не заданы или нет CLI
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
