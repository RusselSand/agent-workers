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


PRIVATE_DIR = 0o700
PRIVATE_FILE = 0o600


def make_private(path: Path, mode: int) -> None:
    """В папке лежат запросы и ответы: соседу по машине их видеть незачем."""
    try:
        path.chmod(mode)
    except OSError:      # на Windows права выставляются иначе, молча продолжаем
        pass


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR)
    make_private(path, PRIVATE_DIR)
    return path


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
    private_dir(root)
    handle = (root / ".registry.lock").open("a+b")
    make_private(root / ".registry.lock", PRIVATE_FILE)
    try:
        wait_lock(handle)   # внутри try: не взяли замок — не потеряем дескриптор
        try:
            yield
        finally:
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
        private_dir(self.folder)
        temporary = self.state_path.with_name(f"state.{os.getpid()}.{uuid4().hex[:8]}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        make_private(temporary, PRIVATE_FILE)
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
        private_dir(path.parent)
        path.write_text(text, encoding="utf-8")
        make_private(path, PRIVATE_FILE)
        return path

    def claim(self) -> bool:
        """Взять папку во владение. False — ею уже владеет живой процесс.

        Замок держится операционной системой и снимается сама, когда владелец исчез,
        поэтому переживает и падение процесса, и перезапуск контейнера.
        """
        if self.lock is not None:
            return True
        with registry(self.folder.parent):
            private_dir(self.folder)
            handle = (self.folder / "owner.lock").open("a+b")
            make_private(self.folder / "owner.lock", PRIVATE_FILE)
            try:
                take_lock(handle)
            except OSError:
                handle.close()
                return False
            self.lock = handle
            return True

    def busy(self) -> bool:
        """Владеет ли папкой живой процесс. Пробуем замок и сразу отпускаем.

        Без замка каталога: зовётся из-под него, а он не реентерабелен.
        """
        if self.lock is not None:
            return True
        path = self.folder / "owner.lock"
        if not path.is_file():
            return False
        handle = path.open("a+b")
        try:
            try:
                take_lock(handle)
            except OSError:
                return True
            free_lock(handle)
            return False
        finally:
            handle.close()

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
        archive = private_dir(self.folder / f"attempt-{attempt}")
        keep = {self.state_path.name, "owner.lock"}
        for path in self.folder.iterdir():
            if path.name in keep or path.name.startswith(("attempt-", "state.")):
                continue
            path.replace(archive / path.name)
        # Всё, что относилось к прошлой попытке — расход, сессия, код возврата, —
        # уезжает вместе с ней: иначе новый ход отчитается чужими цифрами.
        self._write({**EMPTY, "attempt": attempt})
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
        # LK_LOCK не ждёт бесконечно: сдаётся примерно через десять секунд с OSError.
        # Удаление большой папки под замком может идти дольше — ждём, сколько нужно.
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                continue

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
