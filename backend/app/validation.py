"""Shared input-validation helpers."""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation

TICKER_RE = re.compile(r"^[A-Z0-9]{1,10}$")
MIN_TRADE_QUANTITY = Decimal("0.0001")
MAX_TRADE_QUANTITY_DECIMALS = 4


def normalize_ticker(raw: str) -> str:
    ticker = raw.upper().strip()
    if not ticker:
        raise ValueError("ticker must be non-empty")
    if not TICKER_RE.fullmatch(ticker):
        raise ValueError("ticker must be 1-10 alphanumeric characters")
    return ticker


def normalize_trade_quantity(quantity: float) -> float:
    if not math.isfinite(quantity):
        raise ValueError("quantity must be a finite number")

    try:
        normalized = Decimal(str(quantity))
    except InvalidOperation as exc:
        raise ValueError("quantity must be a valid decimal") from exc

    if normalized < MIN_TRADE_QUANTITY:
        raise ValueError(f"quantity must be at least {MIN_TRADE_QUANTITY}")
    if normalized.as_tuple().exponent < -MAX_TRADE_QUANTITY_DECIMALS:
        raise ValueError(
            f"quantity must have at most {MAX_TRADE_QUANTITY_DECIMALS} decimal places"
        )

    return float(normalized)
