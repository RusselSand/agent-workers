"""Во что обошёлся бы ход, если платить за токены.

Подписка оплачена отдельно, поэтому это не счёт, а оценка: столько же работы через
API стоило бы вот столько. Арифметика общая, а цены и раскладка токенов — знание
провайдера: он и передаёт сюда готовый Rates.
"""

from __future__ import annotations

from decimal import Decimal

from .contract import Cost, Rates, Usage

MILLION = Decimal(1_000_000)
CENT = Decimal("0.000001")


def rates(input: str, output: str, cached_input: str | None = None,
          cache_write: str | None = None, *, source: str = "", currency: str = "USD") -> Rates:
    """Цены за миллион токенов строками: Decimal из строки не тащит двоичную погрешность."""
    return Rates(Decimal(input), Decimal(output),
                 Decimal(cached_input) if cached_input is not None else None,
                 Decimal(cache_write) if cache_write is not None else None,
                 currency, source)


def estimate(usage: Usage, price: Rates) -> Cost:
    """Разбивку возвращаем целиком: по одной сумме не видно, что съело деньги."""
    cached = price.cached_input if price.cached_input is not None else price.input
    written = price.cache_write if price.cache_write is not None else price.input
    parts = {
        "input": usage.input * price.input,
        "cached_input": usage.cached_input * cached,
        "cache_write": usage.cache_write * written,
        "output": usage.output * price.output,
    }
    parts = {key: (value / MILLION).quantize(CENT) for key, value in parts.items() if value}
    total = sum(parts.values(), Decimal(0)).quantize(CENT)
    return Cost(total, price.currency, price.source or "table", parts)
