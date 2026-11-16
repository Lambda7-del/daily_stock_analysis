# -*- coding: utf-8 -*-
"""上海黄金交易所 Au99.99 行情查询（元/克），供 agent CLI 等轻量入口使用。

不进入个股分析流水线：只取实时价与日线，分析交给调用方（如外部 Agent）。
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

GOLD_SYMBOL = "Au99.99"
GOLD_UNIT = "CNY/g"
GOLD_SOURCE = "akshare:SGE"
GOLD_LEDGER_SYMBOL = "AU9999"
GOLD_ALIASES = (GOLD_LEDGER_SYMBOL, "AU99.99")


def is_gold_symbol(symbol: str) -> bool:
    """Only SGE Au99.99 aliases; ETFs, futures and overseas gold stay distinct."""
    return (symbol or "").strip().upper() in GOLD_ALIASES


def gold_quote_date(quote: Dict[str, Any]) -> Optional[date]:
    """Use the provider's update date, never the local fetch date (SGE has night sessions)."""
    match = re.match(
        r"^(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})(?:日|(?=[T\s]|$))",
        str(quote.get("updated_at") or ""),
    )
    if match:
        try:
            return date(*(int(part) for part in match.groups()))
        except ValueError:
            pass
    return None


class GoldQuoteUnavailableError(RuntimeError):
    """实时价与日线均未取到。"""


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _pct_change(current: float, base: float) -> Optional[float]:
    if not base:
        return None
    return _round((current - base) / base * 100)


def get_gold_quote(days: Optional[int] = 30, realtime: bool = True, fetcher: Any = None) -> Dict[str, Any]:
    """返回 Au99.99 的最新报价与最近 ``days`` 条日线。

    ``days=None`` 供账本缓存完整日线；CLI 仍限制最多 250 条。
    任一路失败会记入 ``warnings`` 并降级；两路都失败时抛出
    ``GoldQuoteUnavailableError``，不返回空结果。
    """
    if days is not None and days < 1:
        raise ValueError(f"days 必须 >= 1: {days}")
    if fetcher is None:
        from data_provider.akshare_fetcher import AkshareFetcher

        fetcher = AkshareFetcher()

    warnings: List[str] = []
    errors: List[str] = []

    quote: Optional[Dict[str, Any]] = None
    if realtime:
        try:
            quote = fetcher.get_sge_spot_quote(GOLD_SYMBOL)
        except Exception as exc:  # noqa: BLE001 - 降级原因需显式返回给调用方
            logger.warning("获取 %s 实时报价失败: %s", GOLD_SYMBOL, exc)
            errors.append(f"realtime: {type(exc).__name__}: {exc}")
            warnings.append(f"实时价获取失败，仅返回日线: {exc}")

    recent: List[Dict[str, Any]] = []
    try:
        df = fetcher.get_sge_spot_history(GOLD_SYMBOL)
        history_df = df if days is None else df.tail(max(days, 2))
        for row in history_df.itertuples(index=False):
            recent.append(
                {
                    "date": row.date.isoformat(),
                    "open": _round(row.open),
                    "high": _round(row.high),
                    "low": _round(row.low),
                    "close": _round(row.close),
                }
            )
    except Exception as exc:  # noqa: BLE001 - 降级原因需显式返回给调用方
        logger.warning("获取 %s 日线失败: %s", GOLD_SYMBOL, exc)
        errors.append(f"history: {type(exc).__name__}: {exc}")
        warnings.append(f"日线获取失败，无法计算涨跌幅: {exc}")

    if quote is None and not recent:
        raise GoldQuoteUnavailableError(f"{GOLD_SYMBOL} 实时价与日线均未取到: {'; '.join(errors)}")

    last_close = recent[-1]["close"] if recent else None
    last_close_date = recent[-1]["date"] if recent else None
    change_pct: Optional[float] = None
    change_basis: Optional[str] = None
    if quote is not None and last_close is not None:
        change_pct = _pct_change(quote["price"], last_close)
        change_basis = "realtime_vs_last_close"
    elif len(recent) >= 2:
        change_pct = _pct_change(recent[-1]["close"], recent[-2]["close"])
        change_basis = "last_two_closes"
    history = recent if days is None else recent[-days:]

    return {
        "symbol": GOLD_SYMBOL,
        "unit": GOLD_UNIT,
        "source": GOLD_SOURCE,
        "quote": quote,
        "last_close": last_close,
        "last_close_date": last_close_date,
        "change_pct": change_pct,
        "change_basis": change_basis,
        "history": history,
        "warnings": warnings,
    }
