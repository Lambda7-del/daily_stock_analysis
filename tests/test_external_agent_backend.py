# -*- coding: utf-8 -*-
"""External-agent hand-off: prompt capture and response replay through the real analyzer."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    from tests.litellm_stub import ensure_litellm_stub

    ensure_litellm_stub()

from src.analyzer import GeminiAnalyzer
from src.core.pipeline import resolve_agent_pipeline_reason
from src.llm.external_agent_backend import (
    PromptCaptureBackend,
    ResponseReplayBackend,
    generation_backend_override,
    get_generation_backend_override,
    handoff_file_stem,
    handoff_paths,
)


def _analyzer_without_model() -> GeminiAnalyzer:
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    analyzer._router = None
    analyzer._litellm_available = False
    analyzer._skill_instructions_override = ""
    analyzer._default_skill_policy_override = ""
    analyzer._use_legacy_default_prompt_override = True
    analyzer._config_override = SimpleNamespace(
        generation_backend="litellm",
        generation_fallback_backend="litellm",
        litellm_model="",
        litellm_fallback_models=[],
        llm_model_list=[],
        report_language="zh",
        gemini_request_delay=0,
        llm_temperature=0.7,
        report_integrity_enabled=False,
        report_integrity_retry=0,
        news_max_age_days=3,
        news_strategy_profile="short",
    )
    return analyzer


def _context() -> dict:
    return {
        "code": "600519",
        "stock_name": "贵州茅台",
        "date": "2026-11-13",
        "today": {"close": 1435.26, "open": 1404.0, "high": 1437.69, "low": 1394.0, "pct_chg": 1.77,
                  "ma5": 1426.36, "ma10": 1440.24, "ma20": 1390.25},
        "ma_status": "震荡整理",
    }


_RESPONSE = {
    "stock_name": "贵州茅台",
    "sentiment_score": 55,
    "trend_prediction": "震荡",
    "operation_advice": "观望",
    "decision_type": "hold",
    "confidence_level": "中",
    "analysis_summary": "均线缠绕，技术待确认。",
    "dashboard": {
        "core_conclusion": {"one_sentence": "持仓观望，等待放量站稳MA10。"},
        "intelligence": {"risk_alerts": []},
        "battle_plan": {"sniper_points": {"stop_loss": "1380.00"}},
    },
}


class ExternalAgentAnalyzerTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.handoff_dir = Path(tmp.name)
        self.analyzer = _analyzer_without_model()

    def test_capture_writes_prompts_without_a_configured_model(self) -> None:
        backend = PromptCaptureBackend(self.handoff_dir)
        with generation_backend_override(backend):
            result = self.analyzer.analyze(_context(), news_context=None)

        self.assertFalse(result.success)
        self.assertEqual(result.error_details["reason"], "prompt_captured")
        paths = handoff_paths(self.handoff_dir, "600519")
        self.assertEqual(backend.captured["600519"]["prompt_path"], str(paths["prompt_path"]))
        self.assertIn("600519", paths["prompt_path"].read_text(encoding="utf-8"))
        self.assertIn("决策仪表盘", paths["system_prompt_path"].read_text(encoding="utf-8"))
        self.assertFalse(paths["response_path"].exists())

    def test_replay_feeds_agent_response_through_standard_parser(self) -> None:
        response_path = handoff_paths(self.handoff_dir, "600519")["response_path"]
        response_path.write_text(json.dumps(_RESPONSE, ensure_ascii=False), encoding="utf-8")

        backend = ResponseReplayBackend(self.handoff_dir, model_label="dot")
        with generation_backend_override(backend):
            result = self.analyzer.analyze(_context(), news_context=None)

        self.assertTrue(result.success, result.error_message)
        self.assertEqual(result.sentiment_score, 55)
        self.assertEqual(result.decision_type, "hold")
        self.assertEqual(result.model_used, "dot")
        self.assertEqual(result.dashboard["battle_plan"]["sniper_points"]["stop_loss"], "1380.00")
        self.assertEqual(backend.replayed, ["600519"])

    def test_replay_invalid_json_reports_validation_details(self) -> None:
        response_path = handoff_paths(self.handoff_dir, "600519")["response_path"]
        response_path.write_text("这不是 JSON", encoding="utf-8")

        with generation_backend_override(ResponseReplayBackend(self.handoff_dir)):
            result = self.analyzer.analyze(_context(), news_context=None)

        self.assertFalse(result.success)
        self.assertEqual(result.error_details["error_code"], "invalid_json")
        self.assertEqual(result.error_details["stage"], "validation")
        self.assertEqual(result.error_details["backend"], "external_agent")
        self.assertEqual(result.error_details["response_path"], str(response_path))
        self.assertTrue(result.error_details["message"])

    def test_replay_missing_response_file(self) -> None:
        with generation_backend_override(ResponseReplayBackend(self.handoff_dir)):
            result = self.analyzer.analyze(_context(), news_context=None)

        self.assertFalse(result.success)
        self.assertEqual(result.error_details["reason"], "response_file_missing")

    def test_override_never_reaches_configured_backend(self) -> None:
        analyzer = _analyzer_without_model()
        analyzer._litellm_available = True
        with patch("src.analyzer.create_generation_backend", side_effect=AssertionError("model called")), \
                generation_backend_override(PromptCaptureBackend(self.handoff_dir)):
            # Optional LLM steps (market context, etc.) must see "no model" and take their fallback.
            self.assertFalse(analyzer.is_available())
            self.assertIsNone(analyzer.get_generation_backend_config_error())
            with self.assertRaises(Exception) as ctx:
                analyzer._call_litellm("prompt", {}, audit_context={"analysis_mode": "market_review"})
        self.assertNotIsInstance(ctx.exception, AssertionError)
        self.assertEqual(ctx.exception.details["reason"], "external_agent_stock_analysis_only")


class ExternalAgentHelpersTest(unittest.TestCase):
    def test_override_is_exclusive_and_reset(self) -> None:
        backend = PromptCaptureBackend(Path("."))
        with generation_backend_override(backend):
            self.assertIs(get_generation_backend_override(), backend)
            with self.assertRaises(RuntimeError):
                with generation_backend_override(backend):
                    pass
        self.assertIsNone(get_generation_backend_override())

    def test_handoff_file_stem_is_filesystem_safe(self) -> None:
        self.assertEqual(handoff_file_stem("HK00700"), "HK00700")
        self.assertEqual(handoff_file_stem("../BRK.B"), "BRK.B")
        self.assertEqual(handoff_file_stem("a/b c"), "a_b_c")
        self.assertEqual(handoff_file_stem(""), "unknown")

    def test_resolve_agent_pipeline_reason(self) -> None:
        self.assertIsNone(resolve_agent_pipeline_reason(SimpleNamespace(agent_mode=False, agent_skills=[])))
        self.assertIsNone(resolve_agent_pipeline_reason(SimpleNamespace(agent_mode=False, agent_skills=["all"])))
        self.assertEqual(resolve_agent_pipeline_reason(SimpleNamespace(agent_mode=True)), "agent_mode")
        self.assertIn("configured skills",
                      resolve_agent_pipeline_reason(SimpleNamespace(agent_mode=False, agent_skills=["chan"])))
        self.assertIn("request skills", resolve_agent_pipeline_reason(SimpleNamespace(), ["chan"]))


if __name__ == "__main__":
    unittest.main()
