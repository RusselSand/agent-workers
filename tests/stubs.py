"""Исполняемая заглушка вместо настоящей CLI.

Адаптеры при сборке проверяют, что CLI есть и её можно запустить. Файл теста для этого
не годится: в обычном checkout на Linux у него права 0644.
"""
import os
import tempfile
from functools import cache
from pathlib import Path

_FOLDER = tempfile.TemporaryDirectory(prefix="agent-cli-stub-")   # удалится при выходе


@cache
def cli_stub() -> str:
    stub = Path(_FOLDER.name) / ("cli.exe" if os.name == "nt" else "cli")
    stub.write_text("", encoding="utf-8")
    stub.chmod(0o755)
    return str(stub)
