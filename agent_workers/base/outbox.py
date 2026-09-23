"""Лоток готовых результатов: что лежит и как забрать.

Отдельно от воркера, потому что перечислить и забрать можно и тогда, когда сама CLI
недоступна: сломалась при обновлении, снесена, или это машина, где только разбирают
накопленное.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .entry import Busy, Entry, folder_for


@dataclass
class Outbox:
    root: Path

    def entries(self) -> list[Entry]:
        if not self.root.is_dir():
            return []
        return [Entry(self.root, path.name) for path in sorted(self.root.iterdir())
                if path.is_dir()]

    def pending(self) -> list[Entry]:
        """Всё, чем никто не занят: готовые ответы и следы оборванных ходов.

        Папка без владельца — это итог, каким бы он ни был, и его можно забрать.
        Невидимых папок не бывает: иначе они копились бы, и убрать их было бы нечем.
        """
        return [entry for entry in self.entries() if not entry.busy()]

    def collect(self, entry: Entry | str) -> None:
        """Забрали — папку сносим. Сколько хранить, решает тот, кто забирает.

        Сносим только под замком: пока ход идёт, его журналы и сам замок удалять
        нельзя — процесс продолжит писать в снесённые файлы.
        """
        if isinstance(entry, str):
            folder = folder_for(self.root, entry)   # ключ приходит снаружи, его проверяют
            if not folder.is_dir():
                raise ValueError(f"Хода с таким ключом нет: {entry}")
            entry = Entry(self.root, folder.name)
        if not entry.claim():
            raise Busy(f"Ход {entry.folder.name} выполняется прямо сейчас; забирать нельзя")
        entry.drop()
