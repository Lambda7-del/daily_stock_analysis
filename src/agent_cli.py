# -*- coding: utf-8 -*-
"""
===================================
Agent CLI - 面向外部 Agent 的 JSON 命令行入口
===================================

供 ChatGPT dot、Codex 等外部 Agent 在终端中调用 DSA 现有能力：

    python -m src.agent_cli analyze --stocks 600519,AAPL
    python -m src.agent_cli context --stocks 600519          # 只取数，提示词交给外部 Agent
    python -m src.agent_cli analyze --stocks 600519 --responses <handoff_dir>
    python -m src.agent_cli market-review
    python -m src.agent_cli portfolio snapshot
    python -m src.agent_cli portfolio trade-add ... --trade-uid X --confirmed
    python -m src.agent_cli signal latest 600519

约定：
- stdout 只输出一个 JSON 对象：成功 ``{"ok": true, "command", "data"}``，
  失败 ``{"ok": false, "command", "error": {"type", "message"}}``。
- 日志与第三方库输出一律写 stderr / 日志文件。
- 退出码：0 成功；2 参数、校验或确认类错误；1 运行错误。
- 持仓写操作必须带 ``--confirmed``，交易记账必须带 ``--trade-uid`` 以便幂等重试。

本模块只做参数解析、调用现有 service 与 JSON 序列化，不承载业务逻辑。
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
import sys
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

_ANALYSIS_RESULT_FIELDS = (
    "code",
    "name",
    "success",
    "error_message",
    "sentiment_score",
    "trend_prediction",
    "operation_advice",
    "decision_type",
    "action",
    "action_label",
    "confidence_level",
    "current_price",
    "change_pct",
    "analysis_summary",
    "model_used",
    "error_details",
)
_DASHBOARD_FIELDS = ("core_conclusion", "battle_plan")


class AgentCliError(Exception):
    """Structured CLI failure rendered as ``{"ok": false, "error": ...}``."""

    def __init__(
        self,
        error_type: str,
        message: str,
        exit_code: int = EXIT_USAGE_ERROR,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.exit_code = exit_code
        self.details = details


class _JsonArgumentParser(argparse.ArgumentParser):
    """Raise instead of printing usage to stderr and exiting, so stdout stays JSON."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise AgentCliError("usage_error", f"{self.prog}: {message}")


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------
def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


def _dump(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date {raw!r}, expected YYYY-MM-DD") from exc


def _split_codes(raw: str) -> List[str]:
    codes: List[str] = []
    for item in (raw or "").replace("，", ",").split(","):
        code = item.strip()
        if code and code not in codes:
            codes.append(code)
    return codes


def _require_confirmed(args: argparse.Namespace) -> None:
    if not getattr(args, "confirmed", False):
        raise AgentCliError(
            "confirmation_required",
            "持仓写操作需要用户确认成交后再执行，请在确认后追加 --confirmed",
        )


def _summarize_analysis_result(result: Any) -> Dict[str, Any]:
    item = {field: getattr(result, field, None) for field in _ANALYSIS_RESULT_FIELDS}
    dashboard = getattr(result, "dashboard", None)
    if isinstance(dashboard, dict):
        for key in _DASHBOARD_FIELDS:
            item[key] = dashboard.get(key)
    return item


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
def _require_codes(args: argparse.Namespace) -> List[str]:
    codes = _split_codes(args.stocks)
    if not codes:
        raise AgentCliError("validation_error", "--stocks 不能为空")
    return codes


def _reject_agent_pipeline(config: Any) -> None:
    from src.core.pipeline import resolve_agent_pipeline_reason

    reason = resolve_agent_pipeline_reason(config)
    if reason is not None:
        raise AgentCliError(
            "agent_pipeline_unsupported",
            f"当前配置会让个股分析走 Agent 链路（{reason}），外部 Agent 交接不支持该链路；"
            "请关闭 AGENT_MODE 并清空 AGENT_SKILLS 后重试",
        )


def _default_handoff_dir() -> Path:
    reports_dir = Path(__file__).resolve().parent.parent / "reports"
    return reports_dir / "agent_handoff" / datetime.now().strftime("%Y%m%d_%H%M%S")


def _cmd_context(args: argparse.Namespace) -> Dict[str, Any]:
    from src.config import get_config
    from src.llm.external_agent_backend import PromptCaptureBackend, generation_backend_override
    from src.services.analyzer_service import analyze_stocks

    codes = _require_codes(args)
    config = get_config()
    _reject_agent_pipeline(config)
    handoff_dir = Path(args.handoff_dir).expanduser() if args.handoff_dir else _default_handoff_dir()

    backend = PromptCaptureBackend(handoff_dir)
    with generation_backend_override(backend):
        results = analyze_stocks(codes, config=config, full_report=args.full)

    captured = {code.upper(): (code, paths) for code, paths in backend.captured.items()}
    stocks: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for result in results:
        entry = captured.pop(str(getattr(result, "code", "")).upper(), None)
        if entry is None:
            failed.append(_summarize_analysis_result(result))
        else:
            stocks.append({"code": entry[0], "name": getattr(result, "name", None), **entry[1]})
    stocks.extend({"code": code, "name": None, **paths} for code, paths in captured.values())
    returned_codes = {str(item["code"]).upper() for item in stocks + failed}
    missing = [code for code in codes if code.upper() not in returned_codes]

    if not stocks:
        raise AgentCliError(
            "context_failed",
            f"没有股票生成分析上下文: requested={codes}, missing={missing}",
            exit_code=EXIT_RUNTIME_ERROR,
            details={"failed": failed, "missing": missing},
        )
    stock_arg = ",".join(item["code"] for item in stocks)
    return {
        "requested": codes,
        "handoff_dir": str(handoff_dir),
        "stocks": stocks,
        "failed": failed,
        "missing": missing,
        "instructions": (
            "对每只股票：以 system_prompt_path 为系统指令、prompt_path 为输入完成分析，"
            "把一个符合其中输出格式要求的 JSON 对象写入 response_path，然后执行 next_command 入库。"
        ),
        "next_command": f"python -m src.agent_cli analyze --stocks {stock_arg} --responses {handoff_dir}",
    }


def _cmd_analyze(args: argparse.Namespace) -> Dict[str, Any]:
    from src.config import get_config
    from src.enums import ReportType
    from src.notification import NotificationService
    from src.services.analyzer_service import analyze_stocks

    codes = _require_codes(args)
    config = get_config()
    if args.responses:
        from src.llm.external_agent_backend import ResponseReplayBackend, generation_backend_override

        responses_dir = Path(args.responses).expanduser()
        if not responses_dir.is_dir():
            raise AgentCliError("validation_error", f"--responses 目录不存在: {responses_dir}")
        _reject_agent_pipeline(config)
        backend = ResponseReplayBackend(responses_dir, model_label=args.model_label)
        with generation_backend_override(backend):
            results = analyze_stocks(codes, config=config, full_report=args.full)
    else:
        results = analyze_stocks(codes, config=config, full_report=args.full)
    items = [_summarize_analysis_result(r) for r in results]
    returned_codes = {str(getattr(r, "code", "")).upper() for r in results}
    missing = [code for code in codes if code.upper() not in returned_codes]
    succeeded = [r for r in results if getattr(r, "success", True)]

    report_path: Optional[str] = None
    if succeeded:
        notifier = NotificationService()
        report_type = ReportType.FULL if args.full else ReportType.SIMPLE
        content = notifier.generate_aggregate_report(succeeded, report_type)
        filename = f"agent_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        report_path = notifier.save_report_to_file(content, filename=filename)

    if not succeeded:
        raise AgentCliError(
            "analysis_failed",
            f"没有股票分析成功: requested={codes}, missing={missing}, "
            f"errors={[i.get('error_message') for i in items]}",
            exit_code=EXIT_RUNTIME_ERROR,
            details={"results": items, "missing": missing},
        )
    return {
        "requested": codes,
        "results": items,
        "missing": missing,
        "report_path": report_path,
    }


def _cmd_market_review(args: argparse.Namespace) -> Dict[str, Any]:
    from src.config import get_config
    from src.services.analyzer_service import perform_market_review

    report = perform_market_review(config=get_config(), send_notification=False)
    if not report:
        raise AgentCliError("market_review_failed", "大盘复盘未产出报告", exit_code=EXIT_RUNTIME_ERROR)
    return {"report": report}


def _cmd_portfolio_accounts(args: argparse.Namespace) -> Dict[str, Any]:
    from src.services.portfolio_service import PortfolioService

    return {"accounts": PortfolioService().list_accounts(include_inactive=args.include_inactive)}


def _cmd_portfolio_account_create(args: argparse.Namespace) -> Dict[str, Any]:
    from src.services.portfolio_service import PortfolioService

    _require_confirmed(args)
    return PortfolioService().create_account(
        name=args.name,
        broker=args.broker,
        market=args.market,
        base_currency=args.base_currency,
    )


def _cmd_portfolio_snapshot(args: argparse.Namespace) -> Dict[str, Any]:
    from src.services.portfolio_service import PortfolioService

    return PortfolioService().get_portfolio_snapshot(
        account_id=args.account_id,
        as_of=args.as_of,
        cost_method=args.cost_method,
        include_realtime=not args.no_realtime,
    )


def _cmd_portfolio_risk(args: argparse.Namespace) -> Dict[str, Any]:
    from src.services.portfolio_risk_service import PortfolioRiskService

    return PortfolioRiskService().get_risk_report(
        account_id=args.account_id,
        as_of=args.as_of,
        cost_method=args.cost_method,
        include_realtime=not args.no_realtime,
    )


def _cmd_portfolio_trades(args: argparse.Namespace) -> Dict[str, Any]:
    from src.services.portfolio_service import PortfolioService

    return PortfolioService().list_trade_events(
        account_id=args.account_id,
        date_from=args.date_from,
        date_to=args.date_to,
        symbol=args.symbol,
        side=args.side,
        page=args.page,
        page_size=args.page_size,
    )


def _cmd_portfolio_trade_add(args: argparse.Namespace) -> Dict[str, Any]:
    from src.services.portfolio_service import PortfolioConflictError, PortfolioService

    _require_confirmed(args)
    try:
        return PortfolioService().record_trade(
            account_id=args.account_id,
            symbol=args.symbol,
            trade_date=args.trade_date,
            side=args.side,
            quantity=args.quantity,
            price=args.price,
            fee=args.fee,
            tax=args.tax,
            market=args.market,
            currency=args.currency,
            trade_uid=args.trade_uid,
            note=args.note,
        )
    except PortfolioConflictError as exc:
        raise AgentCliError(
            "already_recorded",
            f"trade_uid={args.trade_uid} 已存在，未重复写入；如果这是另一笔成交，请使用新的 trade_uid。({exc})",
        ) from exc


def _cmd_portfolio_cash_add(args: argparse.Namespace) -> Dict[str, Any]:
    from src.services.portfolio_service import PortfolioService

    _require_confirmed(args)
    return PortfolioService().record_cash_ledger(
        account_id=args.account_id,
        event_date=args.event_date,
        direction=args.direction,
        amount=args.amount,
        currency=args.currency,
        note=args.note,
    )


def _cmd_signal_latest(args: argparse.Namespace) -> Dict[str, Any]:
    from src.services.decision_signal_service import DecisionSignalService

    return DecisionSignalService().get_latest_active(
        stock_code=args.stock_code,
        market=args.market,
        limit=args.limit,
    )


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def _add_account_scope(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account-id", type=int, default=None, help="账户 ID，默认全部活跃账户")
    parser.add_argument("--as-of", type=_parse_date, default=None, help="截至日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--cost-method", default="fifo", choices=("fifo", "avg"))
    parser.add_argument("--no-realtime", action="store_true", help="不拉取实时行情，仅用历史收盘价")


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(
        prog="python -m src.agent_cli",
        description="DSA 面向外部 Agent 的 JSON 命令行入口",
    )
    parser.add_argument("--debug", action="store_true", help="stderr 输出 DEBUG 日志")
    commands = parser.add_subparsers(dest="command", required=True)

    analyze = commands.add_parser("analyze", help="分析一只或多只股票并生成报告")
    analyze.add_argument("--stocks", required=True, help="逗号分隔的股票代码，如 600519,hk00700,AAPL")
    analyze.add_argument("--full", action="store_true", help="生成完整报告")
    analyze.add_argument(
        "--responses",
        default=None,
        help="context 交接目录；用其中外部 Agent 写好的 <code>.response.json 代替模型生成",
    )
    analyze.add_argument(
        "--model-label",
        default="external_agent",
        help="配合 --responses，记录到报告与历史的 model_used",
    )
    analyze.set_defaults(handler=_cmd_analyze, command_name="analyze")

    context = commands.add_parser("context", help="只取数并生成分析提示词，交给外部 Agent 分析（不调用模型）")
    context.add_argument("--stocks", required=True, help="逗号分隔的股票代码，如 600519,hk00700,AAPL")
    context.add_argument("--full", action="store_true", help="按完整报告类型准备上下文")
    context.add_argument("--handoff-dir", default=None, help="交接目录，默认 reports/agent_handoff/<时间戳>")
    context.set_defaults(handler=_cmd_context, command_name="context")

    review = commands.add_parser("market-review", help="大盘复盘（不推送通知）")
    review.set_defaults(handler=_cmd_market_review, command_name="market-review")

    portfolio = commands.add_parser("portfolio", help="持仓账本与风险")
    portfolio_cmds = portfolio.add_subparsers(dest="portfolio_command", required=True)

    accounts = portfolio_cmds.add_parser("accounts", help="列出账户")
    accounts.add_argument("--include-inactive", action="store_true")
    accounts.set_defaults(handler=_cmd_portfolio_accounts, command_name="portfolio accounts")

    account_create = portfolio_cmds.add_parser("account-create", help="创建账户（需 --confirmed）")
    account_create.add_argument("--name", required=True)
    account_create.add_argument("--market", required=True, help="cn / hk / us 等")
    account_create.add_argument("--base-currency", required=True, help="CNY / HKD / USD 等")
    account_create.add_argument("--broker", default=None)
    account_create.add_argument("--confirmed", action="store_true")
    account_create.set_defaults(handler=_cmd_portfolio_account_create, command_name="portfolio account-create")

    snapshot = portfolio_cmds.add_parser("snapshot", help="持仓快照与盈亏")
    _add_account_scope(snapshot)
    snapshot.set_defaults(handler=_cmd_portfolio_snapshot, command_name="portfolio snapshot")

    risk = portfolio_cmds.add_parser("risk", help="持仓风险报告")
    _add_account_scope(risk)
    risk.set_defaults(handler=_cmd_portfolio_risk, command_name="portfolio risk")

    trades = portfolio_cmds.add_parser("trades", help="查询交易流水")
    trades.add_argument("--account-id", type=int, default=None)
    trades.add_argument("--date-from", type=_parse_date, default=None)
    trades.add_argument("--date-to", type=_parse_date, default=None)
    trades.add_argument("--symbol", default=None)
    trades.add_argument("--side", default=None, choices=("buy", "sell"))
    trades.add_argument("--page", type=int, default=1)
    trades.add_argument("--page-size", type=int, default=50)
    trades.set_defaults(handler=_cmd_portfolio_trades, command_name="portfolio trades")

    trade_add = portfolio_cmds.add_parser("trade-add", help="记录一笔已确认成交（需 --confirmed）")
    trade_add.add_argument("--account-id", type=int, required=True)
    trade_add.add_argument("--symbol", required=True)
    trade_add.add_argument("--trade-date", type=_parse_date, required=True)
    trade_add.add_argument("--side", required=True, choices=("buy", "sell"))
    trade_add.add_argument("--quantity", type=float, required=True)
    trade_add.add_argument("--price", type=float, required=True)
    trade_add.add_argument("--fee", type=float, default=0.0)
    trade_add.add_argument("--tax", type=float, default=0.0)
    trade_add.add_argument("--market", default=None)
    trade_add.add_argument("--currency", default=None)
    trade_add.add_argument("--trade-uid", required=True, help="成交唯一标识，用于重试去重")
    trade_add.add_argument("--note", default=None)
    trade_add.add_argument("--confirmed", action="store_true")
    trade_add.set_defaults(handler=_cmd_portfolio_trade_add, command_name="portfolio trade-add")

    cash_add = portfolio_cmds.add_parser("cash-add", help="记录资金转入/转出（需 --confirmed）")
    cash_add.add_argument("--account-id", type=int, required=True)
    cash_add.add_argument("--event-date", type=_parse_date, required=True)
    cash_add.add_argument("--direction", required=True, choices=("in", "out"))
    cash_add.add_argument("--amount", type=float, required=True)
    cash_add.add_argument("--currency", default=None)
    cash_add.add_argument("--note", default=None)
    cash_add.add_argument("--confirmed", action="store_true")
    cash_add.set_defaults(handler=_cmd_portfolio_cash_add, command_name="portfolio cash-add")

    signal = commands.add_parser("signal", help="决策信号")
    signal_cmds = signal.add_subparsers(dest="signal_command", required=True)
    latest = signal_cmds.add_parser("latest", help="某只股票最新 active 决策信号")
    latest.add_argument("stock_code")
    latest.add_argument("--market", default=None)
    latest.add_argument("--limit", type=int, default=1)
    latest.set_defaults(handler=_cmd_signal_latest, command_name="signal latest")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _setup_runtime(debug: bool) -> None:
    from src.config import get_config, setup_env
    from src.logging_config import setup_logging

    setup_env()
    config = get_config()
    try:
        setup_logging(log_prefix="agent_cli", debug=debug, log_dir=config.log_dir)
    except OSError as exc:
        logging.basicConfig(stream=sys.stderr, level=logging.DEBUG if debug else logging.INFO)
        logger.warning("文件日志初始化失败，降级为 stderr 日志: %s", exc)


def _classify_exception(exc: Exception) -> AgentCliError:
    from src.services.portfolio_service import PortfolioBusyError, PortfolioOversellError

    if isinstance(exc, AgentCliError):
        return exc
    if isinstance(exc, PortfolioOversellError):
        return AgentCliError("portfolio_oversell", str(exc))
    if isinstance(exc, PortfolioBusyError):
        return AgentCliError("portfolio_busy", str(exc), exit_code=EXIT_RUNTIME_ERROR)
    if isinstance(exc, ValueError):
        return AgentCliError("validation_error", str(exc))
    return AgentCliError("internal_error", f"{type(exc).__name__}: {exc}", exit_code=EXIT_RUNTIME_ERROR)


def run(
    argv: Optional[Sequence[str]] = None,
    *,
    stdout: Optional[TextIO] = None,
    setup_runtime: Callable[[bool], None] = _setup_runtime,
) -> int:
    """Run one CLI command and write exactly one JSON object to ``stdout``."""

    out = stdout if stdout is not None else sys.stdout
    command_name: Optional[str] = None
    # Everything else (logging handlers bound to sys.stdout, library prints,
    # progress bars) must land on stderr so the JSON on ``out`` stays parseable.
    with contextlib.redirect_stdout(sys.stderr):
        try:
            args = build_parser().parse_args(argv)
            command_name = args.command_name
            setup_runtime(args.debug)
            data = args.handler(args)
            payload: Dict[str, Any] = {"ok": True, "command": command_name, "data": data}
            exit_code = EXIT_OK
        except Exception as exc:  # noqa: BLE001 - every failure must become JSON
            error = _classify_exception(exc)
            if error.exit_code == EXIT_RUNTIME_ERROR:
                logger.error("agent_cli %s failed: %s", command_name, error.message, exc_info=exc)
            error_payload: Dict[str, Any] = {"type": error.error_type, "message": error.message}
            if error.details is not None:
                error_payload["details"] = error.details
            payload = {"ok": False, "command": command_name, "error": error_payload}
            exit_code = error.exit_code
    out.write(_dump(payload) + "\n")
    out.flush()
    return exit_code


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
