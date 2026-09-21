"""Папка одного хода: состояние в state.json, транспорт — в журналах на дозапись.

Владелец папки определяется замком операционной системы, а не записанным PID:
замок снимается сам, когда процесс умер или контейнер перезапустили, поэтому
брошенную работу не спутать с идущей.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

RESERVED = (".", "..")


class Busy(RuntimeError):
    """Папкой владеет живой процесс."""


class NotRemoved(RuntimeError):
    """Папку не удалось снести целиком — она снова всплывёт в лотке."""


EMPTY = {"state": "prepared", "started": False, "session_id": None, "pid": None}


def digest(value) -> str:
    """Стабильный ключ хода: один и тот же запрос попадает в ту же папку."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def folder_for(root: Path, key: str) -> Path:
    """Ключ — имя папки внутри root, и ничего больше.

    Проверка обязательна: ключ приходит снаружи, а папку потом сносят рекурсивно.
    Без неё ключ «..» или абсолютный путь удалил бы чужой каталог.
    """
    if not isinstance(key, str) or not key or key in RESERVED:
        raise ValueError(f"Недопустимый ключ хода: {key!r}")
    if os.sep in key or (os.altsep and os.altsep in key) or "/" in key or ":" in key:
        raise ValueError(f"Ключ хода — имя папки, а не путь: {key!r}")
    base = Path(root).resolve()
    folder = (base / key).resolve()
    if folder.parent != base or folder == base:
        raise ValueError(f"Ключ хода выводит за пределы каталога: {key!r}")
    return folder


@contextmanager
def registry(root: Path):
    """Замок на каталог ходов: он переживает удаление любой папки внутри.

    Нужен, потому что замок самой папки исчезает вместе с ней: между его снятием и
    удалением каталога другой процесс успел бы взять папку и начать платный ход,
    а удаление снесло бы его журналы. Берётся на мгновение — только чтобы взятие
    папки и её удаление не наложились друг на друга.
    """
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / ".registry.lock").open("a+b")
    wait_lock(handle)
    try:
        yield
    finally:
        try:
            free_lock(handle)
        finally:
            handle.close()


class Entry:
    def __init__(self, root: Path, key: str) -> None:
        self.folder = folder_for(root, key)
        self.state_path = self.folder / "state.json"
        self.lock = None

    @property
    def stdout(self) -> Path:
        return self.folder / "stdout.jsonl"

    @property
    def stderr(self) -> Path:
        return self.folder / "stderr.txt"

    @property
    def meta(self) -> dict:
        """Пока никто не владел папкой, состояния нет — и это не пустая заготовка."""
        if not self.state_path.is_file():
            return dict(EMPTY)
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except ValueError:
            return dict(EMPTY)

    @property
    def attempted(self) -> bool:
        """Ход уже начинали — неважно, чем он кончился."""
        return bool(self.meta.get("started"))

    def update(self, **values) -> None:
        self._write({**self.meta, **values})

    def _write(self, value: dict) -> None:
        # Через временный файл, и имя у него своё на каждый процесс: два пишущих
        # не должны спорить за один и тот же промежуточный файл.
        self.folder.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f"state.{os.getpid()}.{uuid4().hex[:8]}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    def read(self, relative: str, *, tail: int | None = None) -> str:
        path = self.folder / relative
        if not path.is_file():
            return ""
        with path.open("rb") as stream:
            if tail:
                stream.seek(max(0, path.stat().st_size - tail))
            return stream.read().decode("utf-8", errors="ignore")

    def write(self, relative: str, text: str) -> Path:
        path = self.folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def claim(self) -> bool:
        """Взять папку во владение. False — ею уже владеет живой процесс.

        Замок держится операционной системой и снимается сама, когда владелец исчез,
        поэтому переживает и падение процесса, и перезапуск контейнера.
        """
        if self.lock is not None:
            return True
        with registry(self.folder.parent):
            self.folder.mkdir(parents=True, exist_ok=True)
            handle = (self.folder / "owner.lock").open("a+b")
            try:
                take_lock(handle)
            except OSError:
                handle.close()
                return False
            self.lock = handle
            return True

    def release(self) -> None:
        if self.lock is None:
            return
        try:
            free_lock(self.lock)
        finally:
            self.lock.close()
            self.lock = None

    def mark_started(self) -> None:
        """Ход действительно начинается: отметка переживёт падение и не даст его повторить."""
        self.update(started=True)

    def restart(self) -> int:
        """Повтор: прежнюю попытку отодвигаем целиком, чтобы разбор не смешал две.

        Переносим всё, кроме состояния и замка: что ещё лежит в папке, знает провайдер.
        """
        attempt = int(self.meta.get("attempt", 0)) + 1
        archive = self.folder / f"attempt-{attempt}"
        archive.mkdir(exist_ok=True)
        keep = {self.state_path.name, "owner.lock"}
        for path in self.folder.iterdir():
            if path.name in keep or path.name.startswith(("attempt-", "state.")):
                continue
            path.replace(archive / path.name)
        self._write({**self.meta, "state": "prepared", "started": False,
                     "pid": None, "attempt": attempt})
        return attempt

    def drop(self) -> None:
        """Забрали — папка не нужна: сносим целиком, вместе с отложенными попытками.

        Под замком каталога: пока идёт удаление, взять эту папку никто не может.
        Молчать об отказе нельзя — недоснесённая папка снова всплывёт в лотке.
        """
        with registry(self.folder.parent):
            self.release()
            if not self.folder.exists():
                return
            shutil.rmtree(self.folder)
            if self.folder.exists():
                raise NotRemoved(f"Папку хода не удалось снести: {self.folder}")


if os.name == "nt":
    import msvcrt

    def take_lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def wait_lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def free_lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def take_lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def wait_lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def free_lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
