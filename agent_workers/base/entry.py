"""Папка одного хода: состояние в agent-entry.json, транспорт — в журналах на дозапись.

Имя папки — ключ задачи, который выдал координатор. Одну задачу в один момент делает
один процесс: это гарантирует аренда координатора, а не папка.

Замок операционной системы на файле внутри папки — страховка поверх аренды: он
снимается сам, когда процесс умер или контейнер перезапустили, поэтому брошенную
работу не спутать с идущей, а `collect` не снесёт журналы живого хода.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import time
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


def atomic_write(path: Path, text: str) -> None:
    """Файл подменяется целиком: читатель видит либо старое, либо новое, но не пустое.

    Имя временного файла своё на каждый процесс — два пишущих не спорят за него.
    На Windows подмена может споткнуться о читателя, открывшего цель на мгновение,
    поэтому несколько коротких повторов.
    """
    private_dir(path.parent)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp")
    temporary.write_text(text, encoding="utf-8")
    make_private(temporary, PRIVATE_FILE)
    for pause in (0.0, 0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.3):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            time.sleep(pause)
    temporary.replace(path)


class Busy(RuntimeError):
    """Папкой владеет живой процесс."""


class NotRemoved(RuntimeError):
    """Папку не удалось снести целиком — она снова всплывёт в лотке."""


# Имена свои, ни с чем не спутать: по ним папку признают ходом и разрешают её снести.
# Обычные owner.lock и state.json встречаются у чужих программ сплошь и рядом.
LOCK = "agent-entry.lock"
STATE = "agent-entry.json"


def is_entry(folder: Path) -> bool:
    """Папку хода узнаём по её файлам: замок заводится первым делом, при захвате.

    Чужие каталоги рядом — если каталог ходов по ошибке совпал с каталогом учётной
    записи или указывает на общий каталог, — ходами не считаются, и `collect` их не
    снесёт.
    """
    return (folder / LOCK).is_file() or (folder / STATE).is_file()


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


class Entry:
    def __init__(self, root: Path, key: str) -> None:
        self.folder = folder_for(root, key)
        self.state_path = self.folder / STATE
        self.lock = None

    @property
    def stdout(self) -> Path:
        return self.folder / "stdout.jsonl"

    @property
    def stderr(self) -> Path:
        return self.folder / "stderr.txt"

    @property
    def meta(self) -> dict:
        """Пока ход не начинали, состояния нет."""
        if not self.state_path.is_file():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    @property
    def outcome(self) -> str:
        """Итог папки, которой никто не владеет: всё, кроме готового ответа, — обрыв."""
        return "answered" if self.meta.get("state") == "answered" else "incomplete"

    def update(self, **values) -> None:
        self._write({**self.meta, **values})

    def _write(self, value: dict) -> None:
        atomic_write(self.state_path, json.dumps(value, ensure_ascii=False, indent=2))

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

        Чужую непустую папку с тем же именем не берём: став «нашей», она лишилась бы
        содержимого при подготовке хода. Пустую — берём: это наш же след, если процесс
        упал между созданием папки и замка.
        """
        if self.lock is not None:
            return True
        if (self.folder.is_dir() and not is_entry(self.folder)
                and any(self.folder.iterdir())):
            raise ValueError(f"Папка {self.folder} — не папка хода: каталог ходов "
                             "должен принадлежать воркеру, а не быть общим")
        private_dir(self.folder)
        handle = (self.folder / LOCK).open("a+b")
        make_private(self.folder / LOCK, PRIVATE_FILE)
        try:
            take_lock(handle)
        except OSError as exc:
            handle.close()
            if held(exc):
                return False
            raise   # замок сломан, а не занят: «идёт ход» здесь было бы неправдой
        self.lock = handle
        return True

    def busy(self) -> bool:
        """Владеет ли папкой живой процесс. Пробуем замок и сразу отпускаем."""
        if self.lock is not None:
            return True
        path = self.folder / LOCK
        if not path.is_file():
            return False
        handle = path.open("a+b")
        try:
            try:
                take_lock(handle)
            except OSError as exc:
                if held(exc):
                    return True
                raise
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

    def reset(self) -> None:
        """Новая попытка начинается с чистой папки: разбор не должен смешать её с прошлой.

        Прошлую попытку не храним: повтор решает координатор, а итог прошлой он уже
        получил или не получит никогда.
        """
        for path in self.folder.iterdir():
            if path.name == LOCK:
                continue
            shutil.rmtree(path) if path.is_dir() else path.unlink()

    def drop(self) -> None:
        """Забрали — папка не нужна: сносим целиком.

        Между снятием замка и удалением папку мог бы взять другой процесс с тем же
        ключом, но так не бывает по построению: забирают после того, как координатор
        принял результат, и задачу с этим ключом он больше не выдаёт. Держать замок
        до конца удаления нельзя: на Windows открытый файл замка не удалить.

        Молчать об отказе нельзя — недоснесённая папка снова всплывёт в лотке. Для этого
        её признаки — замок и состояние — удаляются последними: сорвётся удаление на
        середине, остаток всё ещё узнаётся как ход, а не повисает невидимым навсегда.
        """
        self.release()   # на Windows открытый файл замка не удалить
        if not self.folder.exists():
            return
        for path in self.folder.iterdir():
            if path.name in (LOCK, STATE):
                continue
            shutil.rmtree(path) if path.is_dir() else path.unlink()
        for name in (STATE, LOCK):
            (self.folder / name).unlink(missing_ok=True)
        self.folder.rmdir()
        if self.folder.exists():
            raise NotRemoved(f"Папку хода не удалось снести: {self.folder}")


def held(exc: OSError) -> bool:
    """Замок занят другим процессом — или сломан? Только первое значит «ход идёт».

    Любая другая ошибка замка (EIO, ENOLCK, замки не поддерживаются на этом томе)
    выдала бы себя за чужой ход: задача навсегда застряла бы в in_progress, а папка
    пропала бы из лотка. Такие ошибки пробрасываем.
    """
    return exc.errno in HELD


if os.name == "nt":
    import msvcrt

    HELD = {errno.EACCES}                       # так msvcrt отвечает на занятый участок

    def take_lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def free_lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    HELD = {errno.EAGAIN, errno.EWOULDBLOCK}    # так flock с LOCK_NB отвечает на занятый

    def take_lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def free_lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
