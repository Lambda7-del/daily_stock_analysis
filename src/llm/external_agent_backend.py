# -*- coding: utf-8 -*-
"""External-agent generation hand-off backends.

Used by ``src.agent_cli`` so an outside agent (e.g. ChatGPT dot) can act as the
model while DSA keeps data collection, prompt building, parsing, guardrails and
persistence:

- ``PromptCaptureBackend`` writes the stock-analysis prompts to a hand-off
  directory and stops before any model call.
- ``ResponseReplayBackend`` returns the agent-written response files as model
  output so the standard analysis path consumes them.

The override is process-scoped and only enabled by the agent CLI; it is not a
``GENERATION_BACKEND`` value and is never exposed to Web/API configuration.
"""

from __future__ import annotations

import contextlib
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from src.llm.generation_backend import (
    GenerationBackend,
    GenerationCapabilities,
    GenerationError,
    GenerationErrorCode,
    GenerationResult,
)

EXTERNAL_AGENT_BACKEND_ID = "external_agent"
STOCK_ANALYSIS_MODE = "stock_analysis"
PROMPT_CAPTURED_REASON = "prompt_captured"

_CAPABILITIES = GenerationCapabilities(
    supports_json=True,
    supports_tools=False,
    supports_stream=False,
    supports_vision=False,
    supports_health_check=False,
    supports_smoke_test=False,
)
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

_override_lock = threading.Lock()
_override: Optional[GenerationBackend] = None


def get_generation_backend_override() -> Optional[GenerationBackend]:
    return _override


@contextlib.contextmanager
def generation_backend_override(backend: GenerationBackend) -> Iterator[GenerationBackend]:
    """Route every analyzer generation call to ``backend`` for the duration."""
    global _override
    with _override_lock:
        if _override is not None:
            raise RuntimeError("a generation backend override is already active")
        _override = backend
    try:
        yield backend
    finally:
        with _override_lock:
            _override = None


def handoff_file_stem(stock_code: str) -> str:
    stem = _UNSAFE_FILENAME_CHARS.sub("_", str(stock_code or "").strip()).strip("._")
    return stem or "unknown"


def handoff_paths(handoff_dir: Path, stock_code: str) -> Dict[str, Path]:
    stem = handoff_file_stem(stock_code)
    return {
        "system_prompt_path": handoff_dir / f"{stem}.system.md",
        "prompt_path": handoff_dir / f"{stem}.prompt.md",
        "response_path": handoff_dir / f"{stem}.response.json",
    }


def _audit_stock_code(audit_context: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(audit_context, dict) or audit_context.get("analysis_mode") != STOCK_ANALYSIS_MODE:
        return None
    for marker in audit_context.get("dynamic_markers") or []:
        if isinstance(marker, dict) and marker.get("marker_name") == "stock_code":
            text = str(marker.get("text") or "").strip()
            return text or None
    return None


class _ExternalAgentBackend:
    backend_id = EXTERNAL_AGENT_BACKEND_ID
    capabilities = _CAPABILITIES

    def __init__(self, handoff_dir: Path) -> None:
        self.handoff_dir = Path(handoff_dir)

    def _error(self, code: GenerationErrorCode, *, stage: str, details: Dict[str, Any]) -> GenerationError:
        return GenerationError(
            error_code=code,
            stage=stage,
            retryable=False,
            fallbackable=False,
            backend=self.backend_id,
            details=details,
        )

    def _require_stock_code(self, audit_context: Optional[Dict[str, Any]]) -> str:
        stock_code = _audit_stock_code(audit_context)
        if stock_code is None:
            # Market review, chat and other generation paths have no hand-off
            # contract; failing here guarantees no hidden model call happens.
            raise self._error(
                GenerationErrorCode.CAPABILITY_UNSUPPORTED,
                stage="generation",
                details={"reason": "external_agent_stock_analysis_only"},
            )
        return stock_code


class PromptCaptureBackend(_ExternalAgentBackend):
    """Write stock-analysis prompts to the hand-off directory instead of generating."""

    def __init__(self, handoff_dir: Path) -> None:
        super().__init__(handoff_dir)
        self.captured: Dict[str, Dict[str, str]] = {}

    def generate(
        self,
        prompt: str,
        generation_config: Dict[str, Any],
        *,
        system_prompt: Optional[str] = None,
        stream: bool = False,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
        response_validator: Optional[Callable[[str], None]] = None,
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> GenerationResult:
        stock_code = self._require_stock_code(audit_context)
        paths = handoff_paths(self.handoff_dir, stock_code)
        self.handoff_dir.mkdir(parents=True, exist_ok=True)
        paths["system_prompt_path"].write_text(system_prompt or "", encoding="utf-8")
        paths["prompt_path"].write_text(prompt, encoding="utf-8")
        self.captured[stock_code] = {key: str(path) for key, path in paths.items()}
        raise self._error(
            GenerationErrorCode.CAPABILITY_UNSUPPORTED,
            stage="generation",
            details={"reason": PROMPT_CAPTURED_REASON},
        )


class ResponseReplayBackend(_ExternalAgentBackend):
    """Return agent-written response files as the model output."""

    def __init__(self, handoff_dir: Path, *, model_label: str = EXTERNAL_AGENT_BACKEND_ID) -> None:
        super().__init__(handoff_dir)
        self.model_label = model_label
        self.replayed: List[str] = []

    def generate(
        self,
        prompt: str,
        generation_config: Dict[str, Any],
        *,
        system_prompt: Optional[str] = None,
        stream: bool = False,
        stream_progress_callback: Optional[Callable[[int], None]] = None,
        response_validator: Optional[Callable[[str], None]] = None,
        audit_context: Optional[Dict[str, Any]] = None,
    ) -> GenerationResult:
        stock_code = self._require_stock_code(audit_context)
        response_path = handoff_paths(self.handoff_dir, stock_code)["response_path"]
        try:
            text = response_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise self._error(
                GenerationErrorCode.EMPTY_OUTPUT,
                stage="execution",
                details={"reason": "response_file_missing", "response_path": str(response_path)},
            ) from None
        if not text.strip():
            raise self._error(
                GenerationErrorCode.EMPTY_OUTPUT,
                stage="execution",
                details={"reason": "response_file_empty", "response_path": str(response_path)},
            )
        if response_validator is not None:
            try:
                response_validator(text)
            except GenerationError as exc:
                raise self._error(
                    exc.error_code,
                    stage="validation",
                    details={**(exc.details or {}), "response_path": str(response_path)},
                ) from exc
        if stock_code not in self.replayed:
            self.replayed.append(stock_code)
        return GenerationResult(
            text=text,
            model=self.model_label,
            provider=self.backend_id,
            backend=self.backend_id,
            usage={},
        )
