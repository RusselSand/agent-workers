"""Папка одного хода: состояние в state.json, транспорт — в журналах на дозапись."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path


def digest(value) -> str:
    """Стабильный ключ хода: один и тот же запрос попадает в ту же папку."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class Entry:
    def __init__(self, root: Path, key: str) -> None:
        self.folder = Path(root) / key
        self.folder.mkdir(parents=True, exist_ok=True)
        self.state_path = self.folder / "state.json"
        if not self.state_path.exists():
            self._write({"state": "prepared", "started": False, "session_id": None, "pid": None})

    @property
    def stdout(self) -> Path:
        return self.folder / "stdout.jsonl"

    @property
    def stderr(self) -> Path:
        return self.folder / "stderr.txt"

    @property
    def meta(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def update(self, **values) -> None:
        self._write({**self.meta, **values})

    def _write(self, value: dict) -> None:
        # Запись через временный файл: прерывание не оставит обрезанный state.json.
        temporary = self.state_path.with_suffix(".tmp")
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

    def reserve_start(self) -> bool:
        """Маркер ставится атомарно: два процесса не начнут один и тот же ход дважды."""
        try:
            os.close(os.open(self.folder / "started", os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            return False
        self.update(started=True)
        return True

    def release_start(self) -> None:
        """Ход так и не начался: снимаем маркер, папка снова свободна."""
        (self.folder / "started").unlink(missing_ok=True)
        self.update(started=False, state="prepared")

    def restart(self) -> int:
        """Повтор: прежнюю попытку отодвигаем целиком, чтобы разбор не смешал две.

        Переносим всё, кроме состояния: что именно лежит в папке, знает провайдер.
        """
        attempt = int(self.meta.get("attempt", 0)) + 1
        archive = self.folder / f"attempt-{attempt}"
        archive.mkdir(exist_ok=True)
        for path in self.folder.iterdir():
            if path.name != self.state_path.name and not path.name.startswith("attempt-"):
                path.replace(archive / path.name)
        self._write({**self.meta, "state": "prepared", "started": False,
                     "pid": None, "attempt": attempt})
        return attempt

    def drop(self) -> None:
        """Забрали — папка не нужна: сносим целиком, вместе с отложенными попытками."""
        shutil.rmtree(self.folder, ignore_errors=True)
