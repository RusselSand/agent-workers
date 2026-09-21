"""Команды одного воркера: войти, выйти, посмотреть подписку, сделать ход.

Каждая команда работает с тем единственным подключением, которое названо в .env или
флагами, и первой же строкой печатает, с каким именно, — гадать не приходится.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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
        meta = entry.meta
        print(f"{entry.folder.name}  {meta.get('state')}  модель {meta.get('model') or '—'}")
    print()
    print(f"всего {len(waiting)}; забрать: python -m agent_workers collect <ключ|--all>")
    return 0


def collect(outbox: Outbox, options) -> int:
    """Забрали — сносим. Пока не забрали, результат лежит и ждёт."""
    if options.all:
        taken, busy, failed = 0, 0, []
        for entry in outbox.pending():
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


def run(worker: Worker, options) -> int:
    """Один ход: задание -> ответ, с расходом подписки и оценкой стоимости."""
    text = sys.stdin.read() if options.prompt == "-" else options.prompt
    request = {"user": text, "model": worker.adapter.model}
    if options.session:
        request["session"] = options.session   # продолжить прежнюю беседу, а не начать новую
    if options.system_file:
        request["system"] = Path(options.system_file).read_text(encoding="utf-8")

    if options.dry_run:
        entry = Entry(worker.root, worker.key_for(request))
        if not entry.claim():
            # Тот же запрос выполняется прямо сейчас: его файлы трогать нельзя.
            print("этот ход уже идёт в другом процессе", file=sys.stderr)
            return 5
        try:
            command = worker.adapter.ask(entry, request, worker.profile)
            print(f"каталог хода: {entry.folder}")
            print("команда:", " ".join(command.argv))
            print(f"stdin: {command.stdin}")
        finally:
            entry.release()
        print()
        print("Ничего не запущено и не потрачено.")
        return 0

    try:
        # Вход проверяется внутри и только когда ход действительно нужен: готовый ответ
        # из лотка отдаётся и без входа — например, пока учётная запись разлогинена.
        result = worker.run(request, retry=options.retry, ensure_login=True)
    except ValueError as exc:
        # Ошибка в самом запросе — например, испорченный --session: не про вход.
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
    if state == "in_progress":
        print("этот ход уже идёт в другом процессе", file=sys.stderr)
        return 5
    if state == "outbox_full":
        print("лоток полон: " + result["reason"], file=sys.stderr)
        print("заберите результаты: python -m agent_workers pending", file=sys.stderr)
        return 6
    if state == "aborted":
        print("ход не начат: " + result["reason"], file=sys.stderr)
        return 7
    if result["reply"] is None or not result["reply"].text:
        print(explain(result), file=sys.stderr)
        return 4

    print(result["reply"].text)
    if state == "resumed":
        print("ответ взят из прежнего хода — ничего не потрачено", file=sys.stderr)
    report(result)
    if result["reason"]:
        print(result["reason"], file=sys.stderr)
    return 0 if state in ("answered", "resumed") else 4


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
    if result["reply"].session_id:
        # Единственное место, откуда человек узнаёт, чем продолжить беседу.
        print(f"сессия: {result['reply'].session_id} · продолжить: "
              f"--session {result['reply'].session_id}", file=sys.stderr)
    print(f"папка хода: {result['entry'].folder}", file=sys.stderr)


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
            command.add_argument("--session", help="продолжить сессию по её идентификатору")
            command.add_argument("--retry", action="store_true",
                                 help="переделать ход заново, отодвинув прошлую попытку")
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
    if options.command in ("pending", "collect"):
        # Разобрать лоток можно и без CLI: она может быть снесена или сломана обновлением.
        print(f"лоток: {settings.runs}", file=sys.stderr)
        outbox = Outbox(settings.runs)
        return pending(outbox) if options.command == "pending" else collect(outbox, options)

    try:
        worker = build(settings)
    except (ValueError, RuntimeError) as exc:   # нет настроек или не найдена сама CLI
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
