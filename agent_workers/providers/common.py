"""Общее у провайдеров: окружение подпроцесса, поиск CLI, разбор чисел, цена по таблице.

Лежит в providers, а не в base: база о провайдерах не знает, а это — их общие детали.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..base.contract import Cost, Profile, Rates, Reply
from ..base.entry import private_dir
from ..base.pricing import estimate

# Ключи проекта в подпроцесс не уезжают: пропускаем то, без чего CLI не живёт,
# и то, без чего она не выйдет в сеть — прокси и корпоративные сертификаты.
ALLOWED = {"APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME",
           "TEMP", "TMP", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATH", "PATHEXT",
           "PROGRAMFILES", "PROGRAMW6432", "PROGRAMFILES(X86)", "WINDIR", "LANG",
           "PYTHONUTF8", "TERM", "TZ",
           "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
           "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS"}


def find_executable(explicit: str | None, variable: str, *names: str) -> str:
    """Явный путь, затем переменная окружения, затем PATH. Не нашли — ошибка сразу."""
    found = explicit or os.environ.get(variable)
    for name in names:
        found = found or shutil.which(name)
    if not found or not Path(found).is_file():
        raise RuntimeError(f"CLI не найдена: {' / '.join(names)}")
    if os.name != "nt" and not os.access(found, os.X_OK):
        # Файл есть, но запустить его нельзя: лучше сказать сразу, чем упасть на входе.
        raise RuntimeError(f"CLI не запускается — нет права на выполнение: {found}")
    # Абсолютный путь: подпроцессы стартуют из разных каталогов, и относительный
    # путь вроде ./bin/claude там уже никуда не ведёт.
    return str(Path(found).resolve())


def environment(profile: Profile, home_variable: str, extra: set[str] = frozenset()) -> dict:
    private_dir(profile.home)   # там токены входа: соседу по машине туда незачем
    allowed = ALLOWED | extra
    base = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    return {**base, home_variable: str(profile.home)}


def number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def count(value) -> int:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0


def moment(value) -> datetime | None:
    """Момент времени из того, что прислала CLI: секунды эпохи или ISO-строка."""
    if number(value):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except InvalidOperation:
        return None


def table_price(reply: Reply, prices: Mapping[str, Rates]) -> Cost | None:
    """Цена по таблице провайдера. Незнакомая модель — None, а не выдуманное число."""
    rates = prices.get(str(reply.model or ""))
    return estimate(reply.tokens, rates) if rates and reply.tokens else None
