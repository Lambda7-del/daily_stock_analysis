# -*- coding: utf-8 -*-
"""Tests for the external-agent JSON CLI (``python -m src.agent_cli``)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock, patch

import pandas as pd

from src import agent_cli
from src.config import Config
from src.services.portfolio_service import PortfolioService
from src.storage import DatabaseManager

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(argv: List[str]) -> Tuple[int, Dict[str, Any]]:
    out = io.StringIO()
    code = agent_cli.run(argv, stdout=out, setup_runtime=lambda debug: None)
    text = out.getvalue()
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 1, f"stdout must contain exactly one JSON line, got: {text!r}"
    return code, json.loads(lines[0])


class _TempDbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.env_path = root / ".env"
        self.db_path = root / "agent_cli_test.db"
        self.log_dir = root / "logs"
        self.env_path.write_text(
            "\n".join(
                [
                    "STOCK_LIST=600519",
                    "GEMINI_API_KEY=test",
                    "ADMIN_AUTH_ENABLED=false",
                    f"DATABASE_PATH={self.db_path}",
                    f"LOG_DIR={self.log_dir}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        self.env_patch = patch.dict(os.environ, {"ENV_FILE": str(self.env_path), "DATABASE_PATH": str(self.db_path)})
        self.env_patch.start()
        Config.reset_instance()
        DatabaseManager.reset_instance()
        self.db = DatabaseManager.get_instance()

    def tearDown(self) -> None:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def _create_account(self) -> int:
        code, payload = _run(
            [
                "portfolio", "account-create",
                "--name", "agent-test", "--market", "cn", "--base-currency", "CNY",
                "--confirmed",
            ]
        )
        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        return int(payload["data"]["id"])

    def _trade_args(self, account_id: int, **overrides: str) -> List[str]:
        values = {
            "--account-id": str(account_id),
            "--symbol": "600519",
            "--trade-date": "2026-11-10",
            "--side": "buy",
            "--quantity": "100",
            "--price": "1400",
            "--trade-uid": "broker-20261110-600519-buy-1",
        }
        values.update(overrides)
        argv = ["portfolio", "trade-add"]
        for key, value in values.items():
            argv.extend([key, value])
        return argv


class AgentCliParserTest(unittest.TestCase):
    def test_unknown_command_returns_usage_error_json(self) -> None:
        code, payload = _run(["bogus"])
        self.assertEqual(code, agent_cli.EXIT_USAGE_ERROR)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["type"], "usage_error")

    def test_trade_add_requires_trade_uid(self) -> None:
        code, payload = _run(
            [
                "portfolio", "trade-add", "--account-id", "1", "--symbol", "600519",
                "--trade-date", "2026-11-10", "--side", "buy", "--quantity", "1",
                "--price", "1", "--confirmed",
            ]
        )
        self.assertEqual(code, agent_cli.EXIT_USAGE_ERROR)
        self.assertEqual(payload["error"]["type"], "usage_error")
        self.assertIn("--trade-uid", payload["error"]["message"])

    def test_invalid_date_is_usage_error(self) -> None:
        code, payload = _run(["portfolio", "snapshot", "--as-of", "2026/11/13"])
        self.assertEqual(code, agent_cli.EXIT_USAGE_ERROR)
        self.assertEqual(payload["error"]["type"], "usage_error")

    def test_every_subcommand_parses(self) -> None:
        parser = agent_cli.build_parser()
        cases = {
            ("analyze", "--stocks", "600519,AAPL"): "analyze",
            ("market-review",): "market-review",
            ("portfolio", "accounts"): "portfolio accounts",
            ("portfolio", "snapshot", "--no-realtime"): "portfolio snapshot",
            ("portfolio", "risk", "--account-id", "1"): "portfolio risk",
            ("portfolio", "trades", "--symbol", "600519"): "portfolio trades",
            ("portfolio", "cash-add", "--account-id", "1", "--event-date", "2026-11-13",
             "--direction", "in", "--amount", "10"): "portfolio cash-add",
            ("signal", "latest", "600519"): "signal latest",
            ("gold", "--days", "5", "--no-realtime"): "gold",
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(parser.parse_args(list(argv)).command_name, expected)


class AgentCliPortfolioTest(_TempDbTestCase):
    def test_gold_ledger_cli_flow(self) -> None:
        account_id = self._create_account()
        args = self._trade_args(account_id, **{
            "--symbol": "Au99.99", "--quantity": "1.5", "--price": "800",
        })
        code, payload = _run(args)
        self.assertEqual(payload["error"]["type"], "confirmation_required")
        code, payload = _run(args + ["--confirmed"])
        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        code, payload = _run(args + ["--confirmed"])
        self.assertEqual(payload["error"]["type"], "already_recorded")
        code, payload = _run(self._trade_args(account_id, **{
            "--symbol": "AU9999", "--side": "sell", "--quantity": "0.5", "--price": "900",
            "--trade-uid": "gold-sell",
        }) + ["--confirmed"])
        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        code, payload = _run(["portfolio", "trades", "--account-id", str(account_id), "--symbol", "au99.99"])
        self.assertEqual(payload["data"]["total"], 2)
        self.assertEqual(payload["data"]["items"][0]["price_unit"], "CNY/g")
        code, payload = _run(["portfolio", "snapshot", "--account-id", str(account_id), "--no-realtime"])
        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        pos = payload["data"]["accounts"][0]["positions"][0]
        self.assertEqual((pos["symbol"], pos["quantity"], pos["quantity_unit"]), ("AU9999", 1.0, "g"))
        self.assertEqual(payload["data"]["realized_pnl"], 50)
        self.assertFalse(pos["price_available"])

    def test_write_commands_require_confirmation(self) -> None:
        code, payload = _run(
            ["portfolio", "account-create", "--name", "x", "--market", "cn", "--base-currency", "CNY"]
        )
        self.assertEqual(code, agent_cli.EXIT_USAGE_ERROR)
        self.assertEqual(payload["error"]["type"], "confirmation_required")
        self.assertEqual(PortfolioService().list_accounts(), [])

        account_id = self._create_account()
        code, payload = _run(self._trade_args(account_id))
        self.assertEqual(code, agent_cli.EXIT_USAGE_ERROR)
        self.assertEqual(payload["error"]["type"], "confirmation_required")
        self.assertEqual(PortfolioService().list_trade_events(account_id=account_id)["total"], 0)

        code, payload = _run(
            ["portfolio", "cash-add", "--account-id", str(account_id), "--event-date", "2026-11-10",
             "--direction", "in", "--amount", "1000"]
        )
        self.assertEqual(payload["error"]["type"], "confirmation_required")

    def test_ledger_flow_with_idempotent_retry_and_oversell(self) -> None:
        account_id = self._create_account()

        code, payload = _run(
            ["portfolio", "cash-add", "--account-id", str(account_id), "--event-date", "2026-11-09",
             "--direction", "in", "--amount", "200000", "--confirmed"]
        )
        self.assertEqual(code, agent_cli.EXIT_OK, payload)

        code, payload = _run(self._trade_args(account_id) + ["--confirmed"])
        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        self.assertIn("id", payload["data"])

        code, payload = _run(self._trade_args(account_id) + ["--confirmed"])
        self.assertEqual(code, agent_cli.EXIT_USAGE_ERROR)
        self.assertEqual(payload["error"]["type"], "already_recorded")

        code, payload = _run(
            self._trade_args(
                account_id,
                **{"--side": "sell", "--quantity": "500", "--trade-date": "2026-11-11",
                   "--trade-uid": "broker-20261111-600519-sell-1"},
            )
            + ["--confirmed"]
        )
        self.assertEqual(code, agent_cli.EXIT_USAGE_ERROR)
        self.assertEqual(payload["error"]["type"], "portfolio_oversell")

        code, payload = _run(["portfolio", "trades", "--account-id", str(account_id)])
        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        self.assertEqual(payload["data"]["total"], 1)

        df = pd.DataFrame(
            [{"date": date(2026, 11, 12), "open": 1450.0, "high": 1450.0, "low": 1450.0,
              "close": 1450.0, "volume": 1.0, "amount": 1450.0, "pct_chg": 0.0}]
        )
        self.db.save_daily_data(df, code="600519", data_source="unit-test")

        code, payload = _run(
            ["portfolio", "snapshot", "--account-id", str(account_id), "--as-of", "2026-11-12",
             "--no-realtime"]
        )
        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        self.assertAlmostEqual(payload["data"]["total_market_value"], 145000.0)
        self.assertAlmostEqual(payload["data"]["total_cash"], 60000.0)

        code, payload = _run(
            ["portfolio", "risk", "--account-id", str(account_id), "--as-of", "2026-11-12",
             "--no-realtime"]
        )
        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        self.assertIn("concentration", payload["data"])

    def test_validation_error_from_service(self) -> None:
        code, payload = _run(self._trade_args(999) + ["--confirmed"])
        self.assertEqual(code, agent_cli.EXIT_USAGE_ERROR)
        self.assertEqual(payload["error"]["type"], "validation_error")

    def test_signal_latest_empty(self) -> None:
        code, payload = _run(["signal", "latest", "600519"])
        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        self.assertEqual(payload["data"]["items"], [])


def _fake_result(code: str, *, success: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        code=code,
        name=f"name-{code}",
        success=success,
        error_message=None if success else "llm failed",
        sentiment_score=57,
        trend_prediction="震荡",
        operation_advice="观望",
        decision_type="hold",
        action="watch",
        action_label="观望",
        confidence_level="中",
        current_price=1435.26,
        change_pct=1.77,
        analysis_summary="summary",
        model_used="codex_cli",
        error_details=None if success else {"error_code": "non_zero_exit", "reason": "network_unreachable",
                                            "stderr_tail_preview": "<truncated>...error sending request"},
        dashboard={"core_conclusion": {"one_sentence": "等待"}, "battle_plan": {"sniper_points": {}},
                   "intelligence": {"ignored": True}},
    )


def agent_cli_override():
    from src.llm.external_agent_backend import get_generation_backend_override

    return get_generation_backend_override()


class AgentCliAnalysisTest(unittest.TestCase):
    def test_analyze_summarizes_results_and_saves_report(self) -> None:
        notifier = MagicMock()
        notifier.generate_aggregate_report.return_value = "# report"
        notifier.save_report_to_file.return_value = "/tmp/agent_report.md"
        with patch("src.services.analyzer_service.analyze_stocks", return_value=[_fake_result("600519")]) as analyze, \
                patch("src.config.get_config", return_value=SimpleNamespace()), \
                patch("src.notification.NotificationService", return_value=notifier):
            code, payload = _run(["analyze", "--stocks", "600519, AAPL,600519"])

        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        self.assertEqual(analyze.call_args.args[0], ["600519", "AAPL"])
        data = payload["data"]
        self.assertEqual(data["missing"], ["AAPL"])
        self.assertEqual(data["report_path"], "/tmp/agent_report.md")
        item = data["results"][0]
        self.assertEqual(item["action"], "watch")
        self.assertEqual(item["core_conclusion"], {"one_sentence": "等待"})
        self.assertNotIn("intelligence", item)
        filename = notifier.save_report_to_file.call_args.kwargs["filename"]
        self.assertTrue(filename.startswith("agent_report_"))

    def test_analyze_all_failed_is_runtime_error(self) -> None:
        with patch("src.services.analyzer_service.analyze_stocks",
                   return_value=[_fake_result("600519", success=False)]), \
                patch("src.config.get_config", return_value=SimpleNamespace()), \
                patch("src.notification.NotificationService") as notifier_cls:
            code, payload = _run(["analyze", "--stocks", "600519"])

        self.assertEqual(code, agent_cli.EXIT_RUNTIME_ERROR)
        self.assertEqual(payload["error"]["type"], "analysis_failed")
        failed = payload["error"]["details"]["results"][0]
        self.assertEqual(failed["error_message"], "llm failed")
        self.assertEqual(failed["error_details"]["reason"], "network_unreachable")
        self.assertIn("error sending request", failed["error_details"]["stderr_tail_preview"])
        self.assertEqual(payload["error"]["details"]["missing"], [])
        notifier_cls.assert_not_called()

    def test_context_returns_handoff_paths_from_capture_backend(self) -> None:
        from src.llm.external_agent_backend import PromptCaptureBackend, get_generation_backend_override

        def _fake_analyze(codes, **_kwargs):
            backend = get_generation_backend_override()
            self.assertIsInstance(backend, PromptCaptureBackend)
            with self.assertRaises(Exception):
                backend.generate("user prompt", {}, system_prompt="system prompt", audit_context={
                    "analysis_mode": "stock_analysis",
                    "dynamic_markers": [{"marker_name": "stock_code", "text": "600519"}],
                })
            return [_fake_result("600519", success=False), _fake_result("AAPL", success=False)]

        with tempfile.TemporaryDirectory() as tmp, \
                patch("src.services.analyzer_service.analyze_stocks", side_effect=_fake_analyze), \
                patch("src.config.get_config", return_value=SimpleNamespace(agent_mode=False, agent_skills=[])):
            code, payload = _run(["context", "--stocks", "600519,AAPL,TSLA", "--handoff-dir", tmp])
            prompt_text = Path(payload["data"]["stocks"][0]["prompt_path"]).read_text(encoding="utf-8")

        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        data = payload["data"]
        self.assertEqual(prompt_text, "user prompt")
        self.assertEqual([s["code"] for s in data["stocks"]], ["600519"])
        self.assertTrue(data["stocks"][0]["response_path"].endswith("600519.response.json"))
        self.assertEqual([f["code"] for f in data["failed"]], ["AAPL"])
        self.assertEqual(data["missing"], ["TSLA"])
        self.assertEqual(data["next_command"],
                         f"python -m src.agent_cli analyze --stocks 600519 --responses {tmp}")
        self.assertIsNone(agent_cli_override())

    def test_context_rejects_agent_pipeline(self) -> None:
        with patch("src.services.analyzer_service.analyze_stocks") as analyze, \
                patch("src.config.get_config", return_value=SimpleNamespace(agent_mode=True)):
            code, payload = _run(["context", "--stocks", "600519"])

        self.assertEqual(code, agent_cli.EXIT_USAGE_ERROR)
        self.assertEqual(payload["error"]["type"], "agent_pipeline_unsupported")
        analyze.assert_not_called()

    def test_analyze_responses_replays_through_override(self) -> None:
        from src.llm.external_agent_backend import ResponseReplayBackend, get_generation_backend_override

        seen = {}

        def _fake_analyze(codes, **_kwargs):
            seen["backend"] = get_generation_backend_override()
            return [_fake_result("600519")]

        notifier = MagicMock()
        notifier.save_report_to_file.return_value = "/tmp/agent_report.md"
        with tempfile.TemporaryDirectory() as tmp, \
                patch("src.services.analyzer_service.analyze_stocks", side_effect=_fake_analyze), \
                patch("src.config.get_config", return_value=SimpleNamespace(agent_mode=False, agent_skills=[])), \
                patch("src.notification.NotificationService", return_value=notifier):
            code, payload = _run(["analyze", "--stocks", "600519", "--responses", tmp, "--model-label", "dot"])

        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        self.assertIsInstance(seen["backend"], ResponseReplayBackend)
        self.assertEqual(seen["backend"].model_label, "dot")
        self.assertIsNone(agent_cli_override())

    def test_analyze_responses_requires_existing_dir(self) -> None:
        with patch("src.services.analyzer_service.analyze_stocks") as analyze, \
                patch("src.config.get_config", return_value=SimpleNamespace()):
            code, payload = _run(["analyze", "--stocks", "600519", "--responses", "/nonexistent/handoff"])

        self.assertEqual(code, agent_cli.EXIT_USAGE_ERROR)
        self.assertEqual(payload["error"]["type"], "validation_error")
        analyze.assert_not_called()

    def test_generation_error_details_walks_exception_chain_and_whitelists_keys(self) -> None:
        from src.analyzer import _generation_error_details
        from src.llm.generation_backend import GenerationError, GenerationErrorCode

        backend_error = GenerationError(
            error_code=GenerationErrorCode.NON_ZERO_EXIT,
            stage="execution",
            retryable=False,
            fallbackable=True,
            backend="codex_cli",
            details={"reason": "network_unreachable", "returncode": 1,
                     "stderr_tail_preview": "tail", "argv": ["codex", "exec"]},
        )
        try:
            try:
                raise backend_error
            except GenerationError as exc:
                raise RuntimeError("wrapped") from exc
        except RuntimeError as wrapped:
            details = _generation_error_details(wrapped)

        self.assertEqual(details["error_code"], "non_zero_exit")
        self.assertEqual(details["backend"], "codex_cli")
        self.assertEqual(details["reason"], "network_unreachable")
        self.assertEqual(details["stderr_tail_preview"], "tail")
        self.assertNotIn("argv", details)
        self.assertIsNone(_generation_error_details(ValueError("plain")))

    def test_market_review_does_not_notify(self) -> None:
        with patch("src.services.analyzer_service.perform_market_review", return_value="# review") as review, \
                patch("src.config.get_config", return_value=SimpleNamespace()):
            code, payload = _run(["market-review"])

        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        self.assertEqual(payload["data"]["report"], "# review")
        self.assertIs(review.call_args.kwargs["send_notification"], False)


class AgentCliWatchlistTest(_TempDbTestCase):
    def test_watchlist_round_trip_and_conflict(self) -> None:
        code, payload = _run(["watchlist", "list"])
        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        stale_version = payload["data"]["version"]
        code, payload = _run(["watchlist", "add", "Au99.99"])
        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        self.assertEqual(payload["data"]["gold_codes"], ["AU9999"])
        self.assertEqual(payload["data"]["stock_codes"], ["600519"])
        review = {"run_id": "cli-round", "version": stale_version, "decisions": [
            {"symbol": "AAPL", "action": "add", "evaluation": "complete", "reason": "已验证事件关联",
             "evidence": ["https://example.com/announcement"]},
        ]}
        review_file = Path(self.temp_dir.name) / "review.json"
        review_file.write_text(json.dumps(review), encoding="utf-8")
        code, failed = _run(["watchlist", "apply", "--file", str(review_file)])
        self.assertEqual(code, agent_cli.EXIT_USAGE_ERROR)
        self.assertEqual(failed["error"]["type"], "watchlist_conflict")
        review["version"] = payload["data"]["version"]
        review_file.write_text(json.dumps(review), encoding="utf-8")
        code, payload = _run(["watchlist", "apply", "--file", str(review_file)])
        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        self.assertIn("AAPL", payload["data"]["stock_codes"])
        code, payload = _run(["watchlist", "apply", "--file", str(review_file)])
        self.assertTrue(payload["data"]["already_recorded"])
        code, payload = _run(["watchlist", "history"])
        self.assertEqual(len(payload["data"]["items"]), 2)

    def test_news_reports_source_failures_and_reads_existing_items(self) -> None:
        from datetime import datetime
        from src.repositories.intelligence_repo import IntelligenceRepository

        IntelligenceRepository(self.db).upsert_items([{
            "title": "公告", "url": "https://example.com/news", "source_name": "fixture",
            "source_type": "rss", "scope_type": "market", "market": "cn", "published_at": datetime.now(),
        }])
        failure = {"source_count": 1, "results": [{"ok": False, "source_id": 1, "error": "timeout"}]}
        with patch("src.services.intelligence_service.IntelligenceService.fetch_enabled_sources", return_value=failure):
            code, payload = _run(["news", "--refresh", "--market", "cn"])
        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        self.assertEqual(payload["data"]["items"][0]["title"], "公告")
        self.assertFalse(payload["data"]["refresh"]["results"][0]["ok"])
        code, payload = _run(["news", "--days", "0"])
        self.assertEqual(code, agent_cli.EXIT_USAGE_ERROR)

    def test_data_only_market_review_never_constructs_analysis_pipeline(self) -> None:
        from src.market_analyzer import MarketOverview

        with patch("src.services.analyzer_service.StockAnalysisPipeline", side_effect=AssertionError("nested model")), \
                patch("src.market_analyzer.DataFetcherManager"), \
                patch("src.market_analyzer.MarketAnalyzer.get_market_overview", return_value=MarketOverview(date="2026-11-16")), \
                patch("src.market_analyzer.MarketAnalyzer.build_market_light_snapshot", return_value=None), \
                patch("src.market_analyzer.MarketAnalyzer.generate_market_review", side_effect=AssertionError("LLM")):
            code, payload = _run(["market-review", "--data-only", "--region", "us"])
        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        self.assertEqual(payload["data"]["region"], "us")
        self.assertEqual(payload["data"]["markdown_report"], "")


class AgentCliGoldTest(unittest.TestCase):
    @staticmethod
    def _history() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "date": [date(2026, 11, 11), date(2026, 11, 12), date(2026, 11, 13)],
                "open": [876.0, 869.0, 847.8],
                "high": [878.6, 871.4, 857.8],
                "low": [862.05, 850.02, 829.98],
                "close": [872.98, 851.33, 837.55],
            }
        )

    def _fetcher(self, quote: Any = None, history: Any = None) -> MagicMock:
        fetcher = MagicMock()
        for attr, value in (("get_sge_spot_quote", quote), ("get_sge_spot_history", history)):
            if isinstance(value, Exception):
                getattr(fetcher, attr).side_effect = value
            else:
                getattr(fetcher, attr).return_value = value
        return fetcher

    def test_gold_returns_quote_and_history(self) -> None:
        fetcher = self._fetcher(
            quote={"symbol": "Au99.99", "price": 836.0, "time": "02:29:00", "updated_at": "x"},
            history=self._history(),
        )
        with patch("data_provider.akshare_fetcher.AkshareFetcher", return_value=fetcher):
            code, payload = _run(["gold", "--days", "2"])

        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        data = payload["data"]
        self.assertEqual(data["unit"], "CNY/g")
        self.assertEqual(data["quote"]["price"], 836.0)
        self.assertEqual([row["date"] for row in data["history"]], ["2026-11-12", "2026-11-13"])
        self.assertEqual(data["change_basis"], "realtime_vs_last_close")
        self.assertEqual(data["warnings"], [])

    def test_gold_degrades_to_history_when_realtime_fails(self) -> None:
        fetcher = self._fetcher(quote=TimeoutError("slow"), history=self._history())
        with patch("data_provider.akshare_fetcher.AkshareFetcher", return_value=fetcher):
            code, payload = _run(["gold"])

        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        data = payload["data"]
        self.assertIsNone(data["quote"])
        self.assertEqual(data["change_basis"], "last_two_closes")
        self.assertEqual(len(data["warnings"]), 1)
        self.assertIn("slow", data["warnings"][0])

    def test_gold_no_realtime_skips_quote(self) -> None:
        fetcher = self._fetcher(history=self._history())
        with patch("data_provider.akshare_fetcher.AkshareFetcher", return_value=fetcher):
            code, payload = _run(["gold", "--no-realtime"])

        self.assertEqual(code, agent_cli.EXIT_OK, payload)
        fetcher.get_sge_spot_quote.assert_not_called()
        self.assertEqual(payload["data"]["warnings"], [])

    def test_gold_all_sources_failed_is_runtime_error(self) -> None:
        fetcher = self._fetcher(quote=RuntimeError("q down"), history=RuntimeError("h down"))
        with patch("data_provider.akshare_fetcher.AkshareFetcher", return_value=fetcher):
            code, payload = _run(["gold"])

        self.assertEqual(code, agent_cli.EXIT_RUNTIME_ERROR)
        self.assertEqual(payload["error"]["type"], "data_unavailable")
        self.assertIn("q down", payload["error"]["message"])
        self.assertIn("h down", payload["error"]["message"])

    def test_gold_days_out_of_range_is_usage_error(self) -> None:
        for raw in ("0", "251", "abc"):
            with self.subTest(days=raw):
                code, payload = _run(["gold", "--days", raw])
                self.assertEqual(code, agent_cli.EXIT_USAGE_ERROR)
                self.assertEqual(payload["error"]["type"], "usage_error")


class PerformMarketReviewNotificationTest(unittest.TestCase):
    def test_send_notification_defaults_true_and_passes_through(self) -> None:
        from src.services import analyzer_service

        with patch.object(analyzer_service, "StockAnalysisPipeline") as pipeline_cls, \
                patch.object(analyzer_service, "run_market_review", return_value="ok") as run_review:
            pipeline_cls.return_value = MagicMock()
            analyzer_service.perform_market_review(config=SimpleNamespace())
            self.assertIs(run_review.call_args.kwargs["send_notification"], True)
            analyzer_service.perform_market_review(config=SimpleNamespace(), send_notification=False)
            self.assertIs(run_review.call_args.kwargs["send_notification"], False)


class AgentCliProcessStdoutTest(_TempDbTestCase):
    def test_stdout_contains_only_json_with_real_logging(self) -> None:
        env = {**os.environ, "ENV_FILE": str(self.env_path), "DATABASE_PATH": str(self.db_path),
               "LOG_DIR": str(self.log_dir)}
        proc = subprocess.run(
            [sys.executable, "-m", "src.agent_cli", "portfolio", "accounts"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, proc.stdout)
        payload = json.loads(lines[0])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["accounts"], [])
        self.assertIn("日志系统初始化完成", proc.stderr)


if __name__ == "__main__":
    unittest.main()
