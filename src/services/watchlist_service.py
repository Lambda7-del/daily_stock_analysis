"""Shared watchlist operations for Web and external agents.

STOCK_LIST remains the stock membership source. The journal adds provenance,
review decisions and gold watches without feeding gold to stock-only jobs.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from data_provider.base import normalize_stock_code
from src.repositories.watchlist_repo import WatchlistRepository
from src.services.gold_quote_service import GOLD_LEDGER_SYMBOL, is_gold_symbol
from src.services.stock_list_parser import split_stock_list
from src.services.system_config_service import SystemConfigService


_STOCK_CODE_RE = re.compile(
    r"^(?:\d{6}|(?:SH|SZ|BJ)\d{6}|\d{6}\.(?:SH|SZ|SS|BJ)"
    r"|\d{1,5}\.HK|HK\d{1,5}|\d{5}|\d{4,5}\.T"
    r"|\d{6}\.(?:KS|KQ)|\d{4,6}\.(?:TW|TWO)|[A-Z]{1,5}(?:\.(?:US|[A-Z]))?)$",
    re.IGNORECASE,
)


class WatchlistConflictError(ValueError):
    """The caller must reread the watchlist before reviewing changes again."""


def watchlist_key(code: str) -> str:
    if is_gold_symbol(code):
        return GOLD_LEDGER_SYMBOL
    normalized = normalize_stock_code(code.strip()).upper()
    return f"HK{normalized}" if re.fullmatch(r"\d{5}", normalized) else normalized


def validate_watchlist_code(code: str, *, allow_gold=True) -> str:
    if not isinstance(code, str) or not code.strip():
        raise ValueError("代码不能为空")
    if allow_gold and is_gold_symbol(code):
        return GOLD_LEDGER_SYMBOL
    if not _STOCK_CODE_RE.fullmatch(code.strip()):
        raise ValueError(f"'{code}' 不是合法的股票代码格式")
    return watchlist_key(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class WatchlistService:
    def __init__(self, config_service=None, repository=None):
        self.config = config_service or SystemConfigService()
        self.repo = repository or WatchlistRepository()

    def _read(self, session):
        config = self.config.get_config(include_schema=False)
        codes = split_stock_list(next(
            (str(item.get("value", "")) for item in config["items"] if item["key"] == "STOCK_LIST"), ""
        ))
        latest = self.repo.latest(session)
        previous = json.loads(latest.result_json) if latest else {}
        entries = {item["symbol"]: item for item in previous.get("items", []) + previous.get("inactive", [])}
        present = {watchlist_key(code) for code in codes}
        # Legacy/config edits have no review timestamp; do not invent one on every read.
        now = config.get("updated_at")
        for code in codes:
            key = watchlist_key(code)
            if key not in entries or not entries[key]["active"]:
                entries[key] = self._entry(key, now)
        for key, item in entries.items():
            if item["asset_type"] != "gold" and item["active"] and key not in present:
                # Changes made through Settings/.env are manual, never auto removals.
                item.update(active=False, blocked=True, source="manual", changed_at=now)
        return config["config_version"], codes, entries, latest.id if latest else 0

    @staticmethod
    def _entry(symbol, now):
        return {
            "symbol": symbol, "asset_type": "gold" if is_gold_symbol(symbol) else "stock",
            "active": True, "source": "manual", "protected": True, "blocked": False,
            "reason": "用户关注", "evidence": [], "reviewed_at": None, "review_after": None,
            "changed_at": now,
        }

    @staticmethod
    def _view(config_version, codes, entries, revision):
        version = hashlib.sha256(_json([config_version, codes, revision]).encode()).hexdigest()
        return {
            "version": version,
            "stock_codes": [code for code in codes if not is_gold_symbol(code)],
            "gold_codes": [key for key, item in entries.items() if item["active"] and item["asset_type"] == "gold"],
            "items": [item for item in entries.values() if item["active"]],
            "inactive": [item for item in entries.values() if not item["active"]],
        }

    def list(self):
        with self.repo.session() as session:
            return self._view(*self._read(session))

    def change(self, symbol, *, action, reason="用户指令"):
        """Explicit user actions protect additions and block automatic re-adds."""
        key = validate_watchlist_code(symbol)
        if action not in {"add", "remove"}:
            raise ValueError("action must be add or remove")
        with self.repo.session(write=True) as session:
            config_version, codes, entries, _ = self._read(session)
            now = datetime.now().isoformat()
            item = entries.setdefault(key, self._entry(key, now))
            item.update(active=action == "add", source="manual", protected=True,
                        blocked=action == "remove", reason=reason, changed_at=now)
            request = {"symbol": key, "action": action, "reason": reason, "source": "manual"}
            return self._save(session, config_version, codes, entries,
                              f"manual-{uuid.uuid4().hex}", request, [request], display_symbol=symbol.strip())

    @staticmethod
    def _validate_review(payload):
        if not isinstance(payload, dict):
            raise ValueError("review must be a JSON object")
        if set(payload) - {"run_id", "version", "news_summary", "decisions"}:
            raise ValueError("unknown review fields")
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 64 or run_id.startswith("manual-"):
            raise ValueError("run_id must be a nonempty unique id of at most 64 characters")
        if not isinstance(payload.get("version"), str) or not payload["version"]:
            raise ValueError("version is required; read watchlist list first")
        decisions = payload.get("decisions")
        if not isinstance(decisions, list) or len(decisions) > 100:
            raise ValueError("decisions must be a list of at most 100 items")
        seen = set()
        for item in decisions:
            if not isinstance(item, dict) or set(item) - {
                "symbol", "action", "evaluation", "reason", "evidence", "review_after",
            }:
                raise ValueError("invalid decision fields")
            symbol = validate_watchlist_code(item.get("symbol"))
            if symbol in seen:
                raise ValueError(f"duplicate decision: {symbol}")
            seen.add(symbol)
            if item.get("action") not in {"add", "keep", "remove", "reject", "defer"}:
                raise ValueError("action must be add, keep, remove, reject or defer")
            if item.get("evaluation") not in {"complete", "incomplete"}:
                raise ValueError("evaluation must be complete or incomplete")
            if not isinstance(item.get("reason"), str) or not item["reason"].strip():
                raise ValueError("every decision requires a reason")
            evidence = item.get("evidence", [])
            if not isinstance(evidence, list) or any(
                not isinstance(url, str) or urlsplit(url).scheme not in {"http", "https"}
                or not urlsplit(url).netloc for url in evidence
            ):
                raise ValueError("evidence must be a list of http(s) source URLs")
            if item["action"] == "add" and item["evaluation"] == "complete" and not evidence:
                raise ValueError("adding an automatic watch requires evidence")
            if item.get("review_after") is not None:
                if not isinstance(item["review_after"], str):
                    raise ValueError("review_after must be an ISO date string")
                date.fromisoformat(item["review_after"])

    def apply_review(self, payload, *, max_auto_items=20, cooldown_days=3):
        """Apply only explicitly completed decisions, without touching the ledger."""
        self._validate_review(payload)
        if not 1 <= max_auto_items <= 100 or not 0 <= cooldown_days <= 365:
            raise ValueError("max_auto_items must be 1-100; cooldown_days must be 0-365")
        request = {**payload, "max_auto_items": max_auto_items, "cooldown_days": cooldown_days}
        with self.repo.session(write=True) as session:
            existing = self.repo.get(session, payload["run_id"])
            if existing:
                if existing.request_json != _json(request):
                    raise ValueError("run_id already used with different content")
                return {**json.loads(existing.result_json), "already_recorded": True}
            config_version, codes, entries, revision = self._read(session)
            if payload["version"] != self._view(config_version, codes, entries, revision)["version"]:
                raise WatchlistConflictError("关注已变化，请重新读取列表并复核决定")
            now = datetime.now()
            changes = []
            for decision in payload["decisions"]:
                key = validate_watchlist_code(decision["symbol"])
                action = decision["action"]
                item = entries.get(key)
                skipped = None
                if decision["evaluation"] != "complete":
                    skipped = "incomplete_evaluation"
                elif action in {"reject", "defer"}:
                    skipped = "candidate_only"
                elif item and item["active"] and item["protected"]:
                    skipped = "manual_watch_protected"
                elif action == "add" and item and item["blocked"]:
                    skipped = "manually_removed"
                elif action == "add" and (not item or not item["active"]):
                    if item and now - datetime.fromisoformat(item["changed_at"]) < timedelta(days=cooldown_days):
                        skipped = "readd_cooldown"
                    elif sum(i["active"] and i["source"] == "auto" for i in entries.values()) >= max_auto_items:
                        skipped = "auto_watch_limit"
                elif action in {"keep", "remove"} and (not item or not item["active"]):
                    skipped = "not_watched"
                if skipped:
                    changes.append({"symbol": key, "action": action, "applied": False, "reason": skipped})
                    continue
                if item is None:
                    item = entries[key] = self._entry(key, now.isoformat())
                    item.update(source="auto", protected=False)
                was_active = item["active"]
                item.update(active=action != "remove", reason=decision["reason"],
                            reviewed_at=now.isoformat(), review_after=decision.get("review_after"))
                if decision.get("evidence"):
                    item["evidence"] = decision["evidence"]
                if item["active"] != was_active:
                    item["changed_at"] = now.isoformat()
                changes.append({"symbol": key, "action": action, "applied": True, "reason": decision["reason"]})
            return self._save(session, config_version, codes, entries, payload["run_id"], request, changes)

    def _save(self, session, config_version, codes, entries, run_id, request, changes, display_symbol=None):
        # Keep legacy spellings/order; remove all aliases of a removed symbol.
        stock_codes = [code for code in codes if not is_gold_symbol(code) and entries[watchlist_key(code)]["active"]]
        present = {watchlist_key(code) for code in stock_codes}
        stock_codes.extend(
            display_symbol if display_symbol and watchlist_key(display_symbol) == key else key
            for key, item in entries.items()
            if item["active"] and item["asset_type"] == "stock" and key not in present
        )
        if stock_codes != codes:
            updated = self.config.update(config_version=config_version,
                                         items=[{"key": "STOCK_LIST", "value": ",".join(stock_codes)}],
                                         mask_token="******", reload_now=True)
            config_version = updated["config_version"]
        row = self.repo.save(session, run_id=run_id, request_json=_json(request), result_json="{}")
        result = {**self._view(config_version, stock_codes, entries, row.id),
                  "run_id": run_id, "changes": changes, "already_recorded": False}
        row.result_json = _json(result)
        return result

    def history(self, limit=20):
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1-100")
        return {"items": [{"run_id": row.run_id, "created_at": row.created_at.isoformat(),
                           "review": json.loads(row.request_json),
                           "changes": json.loads(row.result_json)["changes"]}
                          for row in self.repo.history(limit)]}
