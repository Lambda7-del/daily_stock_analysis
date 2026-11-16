# -*- coding: utf-8 -*-
"""Tests for SGE Au99.99 quote fetching and the gold quote service."""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd

from data_provider.akshare_fetcher import AkshareFetcher
from data_provider.base import DataFetchError
from src.services.gold_quote_service import GoldQuoteUnavailableError, get_gold_quote, gold_quote_date, is_gold_symbol


def _raw_history() -> pd.DataFrame:
    # Column layout mirrors ak.spot_hist_sge (akshare 1.19.x): date is datetime.date, unsorted on purpose.
    return pd.DataFrame(
        {
            "date": [date(2026, 11, 13), date(2026, 11, 11), date(2026, 11, 12)],
            "open": [847.8, 876.0, 869.0],
            "close": [837.55, 872.98, 851.33],
            "low": [829.98, 862.05, 850.02],
            "high": [857.8, 878.6, 871.4],
        }
    )


def _raw_quotations() -> pd.DataFrame:
    # Column layout mirrors ak.spot_quotations_sge (akshare 1.19.x): minute-level rows for the session.
    return pd.DataFrame(
        {
            "品种": ["Au99.99", "Au99.99", "Au99.99"],
            "时间": ["02:27:00", "02:28:00", "02:29:00"],
            "现价": [835.0, 836.0, None],
            "更新时间": ["2026年11月16日 02:29:55"] * 3,
        }
    )


class SgeFetcherTest(unittest.TestCase):
    def test_ledger_identity_and_quote_dates(self) -> None:
        for symbol in ("AU9999", "Au99.99", " au9999 "):
            self.assertTrue(is_gold_symbol(symbol))
        for symbol in ("518880", "AU", "GC=F", "XAUUSD"):
            self.assertFalse(is_gold_symbol(symbol))
        for stamp in ("2026年11月16日 02:29:55", "2026-11-16T02:29:55", "2026-11-16"):
            self.assertEqual(gold_quote_date({"updated_at": stamp}), date(2026, 11, 16))
        for stamp in ("", "unknown", "2026-02-30", "02:29:55"):
            self.assertIsNone(gold_quote_date({"updated_at": stamp}))

    def setUp(self) -> None:
        self.fetcher = AkshareFetcher(sleep_min=0, sleep_max=0)

    def test_history_is_normalized_and_sorted(self) -> None:
        with patch("data_provider.akshare_fetcher._akshare_call_with_timeout", return_value=_raw_history()) as call:
            df = self.fetcher.get_sge_spot_history("Au99.99")

        self.assertEqual(call.call_args.kwargs["symbol"], "Au99.99")
        self.assertEqual(list(df.columns), ["date", "open", "high", "low", "close"])
        self.assertEqual(list(df["date"]), [date(2026, 11, 11), date(2026, 11, 12), date(2026, 11, 13)])
        self.assertEqual(df["close"].iloc[-1], 837.55)

    def test_history_missing_columns_raises(self) -> None:
        bad = _raw_history().drop(columns=["close"])
        with patch("data_provider.akshare_fetcher._akshare_call_with_timeout", return_value=bad):
            with self.assertRaises(DataFetchError):
                self.fetcher.get_sge_spot_history()

    def test_history_empty_raises(self) -> None:
        with patch("data_provider.akshare_fetcher._akshare_call_with_timeout", return_value=pd.DataFrame()):
            with self.assertRaises(DataFetchError):
                self.fetcher.get_sge_spot_history()

    def test_quote_uses_last_non_null_price(self) -> None:
        with patch("data_provider.akshare_fetcher._akshare_call_with_timeout", return_value=_raw_quotations()):
            quote = self.fetcher.get_sge_spot_quote("Au99.99")

        self.assertEqual(
            quote,
            {"symbol": "Au99.99", "price": 836.0, "time": "02:28:00", "updated_at": "2026年11月16日 02:29:55"},
        )

    def test_quote_without_price_raises(self) -> None:
        empty_prices = _raw_quotations().assign(现价=None)
        with patch("data_provider.akshare_fetcher._akshare_call_with_timeout", return_value=empty_prices):
            with self.assertRaises(DataFetchError):
                self.fetcher.get_sge_spot_quote()


class GoldQuoteServiceTest(unittest.TestCase):
    def test_ledger_can_request_full_history(self) -> None:
        history = pd.DataFrame({
            "date": pd.date_range("2025-01-01", periods=300).date,
            "open": 900, "high": 900, "low": 900, "close": 900,
        })
        data = get_gold_quote(days=None, realtime=False, fetcher=self._fetcher(history=history))
        self.assertEqual(len(data["history"]), 300)
        self.assertEqual(data["history"][0]["date"], "2025-01-01")

    def _fetcher(self, quote=None, history=None) -> MagicMock:
        fetcher = MagicMock()
        for attr, value in (("get_sge_spot_quote", quote), ("get_sge_spot_history", history)):
            if isinstance(value, Exception):
                getattr(fetcher, attr).side_effect = value
            else:
                getattr(fetcher, attr).return_value = value
        return fetcher

    def _history(self) -> pd.DataFrame:
        return _raw_history().sort_values("date").reset_index(drop=True)

    def test_change_pct_uses_realtime_against_last_close(self) -> None:
        fetcher = self._fetcher(quote={"symbol": "Au99.99", "price": 846.0, "time": "t", "updated_at": "u"},
                                history=self._history())
        data = get_gold_quote(days=30, fetcher=fetcher)

        self.assertEqual(data["last_close"], 837.55)
        self.assertEqual(data["last_close_date"], "2026-11-13")
        self.assertEqual(data["change_pct"], round((846.0 - 837.55) / 837.55 * 100, 2))
        self.assertEqual(data["change_basis"], "realtime_vs_last_close")
        self.assertEqual(len(data["history"]), 3)

    def test_single_day_still_computes_change_from_previous_close(self) -> None:
        fetcher = self._fetcher(history=self._history())
        data = get_gold_quote(days=1, realtime=False, fetcher=fetcher)

        self.assertEqual(len(data["history"]), 1)
        self.assertEqual(data["change_pct"], round((837.55 - 851.33) / 851.33 * 100, 2))
        self.assertEqual(data["change_basis"], "last_two_closes")
        fetcher.get_sge_spot_quote.assert_not_called()

    def test_quote_only_when_history_fails(self) -> None:
        fetcher = self._fetcher(quote={"symbol": "Au99.99", "price": 836.0, "time": "t", "updated_at": "u"},
                                history=DataFetchError("boom"))
        data = get_gold_quote(fetcher=fetcher)

        self.assertEqual(data["history"], [])
        self.assertIsNone(data["change_pct"])
        self.assertIsNone(data["change_basis"])
        self.assertEqual(len(data["warnings"]), 1)
        self.assertIn("boom", data["warnings"][0])

    def test_history_failure_with_realtime_disabled_raises(self) -> None:
        fetcher = self._fetcher(history=DataFetchError("boom"))
        with self.assertRaises(GoldQuoteUnavailableError) as ctx:
            get_gold_quote(realtime=False, fetcher=fetcher)
        self.assertIn("boom", str(ctx.exception))

    def test_invalid_days_rejected(self) -> None:
        with self.assertRaises(ValueError):
            get_gold_quote(days=0, fetcher=self._fetcher())


if __name__ == "__main__":
    unittest.main()
