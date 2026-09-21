"""Лоток готовых результатов: что лежит, сколько занято, как забрать.

Отдельно от воркера, потому что перечислить и забрать можно и тогда, когда сама CLI
недоступна: сломалась при обновлении, снесена, или это машина, где только разбирают
накопленное.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .entry import Busy, Entry, folder_for

DELIVERED = ("answered", "incomplete")   # ход кончился, результат ждёт получателя


@dataclass
class Outbox:
    root: Path

    def entries(self) -> list[Entry]:
        if not self.root.is_dir():
            return []
        return [Entry(self.root, path.name) for path in sorted(self.root.iterdir())
                if path.is_dir() and (path / "state.json").is_file()]

    def occupied(self, *, besides: str | None = None) -> int:
        """Занятые места: готовые результаты, начатые ходы и живые брони.

        Бронь — место, взятое до запуска CLI. Если процесс убили в этот момент, бронь
        остаётся на диске, но её никто не держит: такую не считаем, иначе череда
        падений незаметно заполнила бы лоток. besides — своя папка: её мы сами держим,
        и в чужих местах она не учитывается.
        """
        return sum(1 for entry in self.entries() if entry.folder.name != besides and (
            entry.meta.get("state") in DELIVERED or entry.meta.get("started")
            or (entry.meta.get("state") == "reserved" and entry.busy())))

    def pending(self) -> list[Entry]:
        """Готовые результаты, которых ещё не забрали. Папка живёт, пока её не заберут."""
        return [entry for entry in self.entries() if entry.meta.get("state") in DELIVERED]

    def collect(self, entry: Entry | str) -> None:
        """Забрали — папку сносим. Сколько хранить, решает тот, кто забирает.

        Сносим только под замком: пока ход идёт, его журналы и сам замок удалять
        нельзя — процесс продолжит писать в снесённые файлы, а следующий вызов
        заведёт новый замок и оплатит ту же работу второй раз.
        """
        if isinstance(entry, str):
            folder = folder_for(self.root, entry)   # ключ приходит снаружи, его проверяют
            if not (folder / "state.json").is_file():
                raise ValueError(f"Хода с таким ключом нет: {entry}")
            entry = Entry(self.root, folder.name)
        if not entry.claim():
            raise Busy(f"Ход {entry.folder.name} выполняется прямо сейчас; забирать нельзя")
        entry.drop()
