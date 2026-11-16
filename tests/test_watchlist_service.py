"""Exercise shared watchlist state with real temporary config and SQLite writes."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from api.v1.endpoints.stocks import add_to_watchlist, remove_from_watchlist
from api.v1.schemas.history import WatchlistRequest
from src.config import Config
from src.core.config_manager import ConfigManager
from src.services.system_config_service import SystemConfigService
from src.services.watchlist_service import WatchlistConflictError, WatchlistService
from src.storage import DatabaseManager


@pytest.fixture
def service(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("STOCK_LIST=600519,00700\n", encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(env))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "watchlist.db"))
    monkeypatch.setenv("STOCK_LIST", "600519,00700")
    Config.reset_instance()
    DatabaseManager.reset_instance()
    service = WatchlistService(SystemConfigService(ConfigManager(env)))
    yield service
    DatabaseManager.reset_instance()
    Config.reset_instance()


def decision(symbol, action="add", **overrides):
    return {"symbol": symbol, "action": action, "evaluation": "complete",
            "reason": "事件与公司业务关联已核实，等待价格条件", "evidence": ["https://example.com/announcement"],
            **overrides}


def review(service, *decisions, run_id="round-1", **overrides):
    return {"run_id": run_id, "version": service.list()["version"], "decisions": list(decisions), **overrides}


def test_legacy_manual_watches_and_aliases_are_protected(service):
    result = service.apply_review(review(service, decision("HK00700", "remove"), decision("600519", "remove")))
    assert result["stock_codes"] == ["600519", "00700"]
    assert all(change["reason"] == "manual_watch_protected" for change in result["changes"])
    assert all(item["protected"] for item in result["items"])


def test_auto_round_is_durable_idempotent_and_shared_with_web(service):
    payload = review(service, decision("aapl"), decision("MSFT", "reject"))
    first = service.apply_review(payload)
    assert first["stock_codes"] == ["600519", "00700", "AAPL"]
    assert service.list()["items"][-1]["source"] == "auto"
    assert service.apply_review(payload)["already_recorded"]
    assert len(service.history()["items"]) == 1
    with pytest.raises(ValueError, match="different content"):
        service.apply_review({**payload, "decisions": []})
    assert service.history()["items"][0]["review"]["decisions"][1]["action"] == "reject"
    # Clicking add in Web takes ownership of an existing auto watch.
    add_to_watchlist(WatchlistRequest(stock_code="AAPL"), service=service.config)
    result = service.apply_review(review(service, decision("AAPL", "remove"), run_id="round-2"))
    assert result["changes"][0]["reason"] == "manual_watch_protected"


def test_manual_remove_blocks_readd_until_user_adds_again(service):
    service.apply_review(review(service, decision("AAPL")))
    remove_from_watchlist(WatchlistRequest(stock_code="aapl"), service=service.config)
    result = service.apply_review(review(service, decision("AAPL"), run_id="round-2"))
    assert "AAPL" not in result["stock_codes"]
    assert result["changes"][0]["reason"] == "manually_removed"
    service.change("AAPL", action="add")
    assert service.list()["items"][-1]["protected"]


def test_failed_or_deferred_analysis_never_changes_membership(service):
    service.apply_review(review(service, decision("AAPL")))
    result = service.apply_review(review(service, decision("AAPL", "remove", evaluation="incomplete"),
                                        decision("MSFT", evaluation="incomplete"),
                                        decision("NVDA", "defer"), run_id="round-2"))
    assert result["stock_codes"] == ["600519", "00700", "AAPL"]
    assert not any(change["applied"] for change in result["changes"])


def test_stale_reviews_cannot_overwrite_user_changes(service):
    payload = review(service, decision("AAPL"))
    service.change("MSFT", action="add")
    with pytest.raises(WatchlistConflictError):
        service.apply_review(payload)
    assert service.list()["stock_codes"] == ["600519", "00700", "MSFT"]
    assert len(service.history()["items"]) == 1


def test_gold_watch_is_persistent_and_never_enters_stock_jobs(service):
    service.change("Au99.99", action="add")
    result = service.list()
    assert result["stock_codes"] == ["600519", "00700"]
    assert result["gold_codes"] == ["AU9999"]
    assert result["items"][-1]["asset_type"] == "gold"
    assert Config.get_instance().stock_list == ["600519", "00700"]
    assert "AU" not in service.config._manager.env_path.read_text()
    service.change("AU9999", action="remove")
    assert service.list()["gold_codes"] == []


def test_auto_limits_cooldown_and_keep(service):
    result = service.apply_review(review(service, decision("AAPL"), decision("MSFT")), max_auto_items=1)
    assert result["changes"][1]["reason"] == "auto_watch_limit"
    service.apply_review(review(service, decision("AAPL", "keep", review_after="2026-12-01"), run_id="keep"))
    assert service.list()["items"][-1]["review_after"] == "2026-12-01"
    service.apply_review(review(service, decision("AAPL", "remove"), run_id="remove"))
    result = service.apply_review(review(service, decision("AAPL"), run_id="readd"))
    assert result["changes"][0]["reason"] == "readd_cooldown"


@pytest.mark.parametrize("decisions", [
    [decision("AAPL", evidence=[])], [decision("invalid/code")],
    [decision("00700"), decision("HK00700")],
    [decision("AAPL", evaluation="failed")], [decision("AAPL", evidence=["file:///tmp/news"])],
    [decision("AAPL", review_after="tomorrow")], [decision("AAPL", action="buy")],
])
def test_invalid_round_is_rejected_before_any_write(service, decisions):
    before = service.list()
    with pytest.raises(ValueError):
        service.apply_review(review(service, *decisions))
    assert service.list() == before
    assert service.history()["items"] == []


def test_settings_removal_is_treated_as_manual(service):
    service.apply_review(review(service, decision("AAPL")))
    config = service.config.get_config(include_schema=False)
    service.config.update(config_version=config["config_version"], items=[{"key": "STOCK_LIST", "value": "600519"}])
    result = service.apply_review(review(service, decision("AAPL"), run_id="round-2"))
    assert result["changes"][0]["reason"] == "manually_removed"
    assert result["stock_codes"] == ["600519"]


def test_review_history_contains_no_ledger_events(service):
    service.apply_review(review(service, decision("AAPL")))
    from src.storage import PortfolioTrade
    with service.repo.db.get_session() as session:
        assert session.query(PortfolioTrade).count() == 0
    assert json.loads(service.repo.history()[0].request_json)["decisions"]


def test_concurrent_reviews_preserve_the_winning_update(service):
    version = service.list()["version"]
    ready = threading.Barrier(2)

    def apply(symbol):
        ready.wait(timeout=5)
        try:
            service.apply_review({"run_id": symbol, "version": version, "decisions": [decision(symbol)]})
            return "saved"
        except WatchlistConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(apply, ["AAPL", "MSFT"])) == ["conflict", "saved"]
    assert len(service.list()["stock_codes"]) == 3
    assert len(service.history()["items"]) == 1


def test_config_write_without_journal_is_recovered_as_protected(service, monkeypatch):
    def fail_save(*args, **kwargs):
        raise OSError("journal write failed")

    with monkeypatch.context() as m:
        m.setattr(service.repo, "save", fail_save)
        with pytest.raises(OSError, match="journal"):
            service.apply_review(review(service, decision("AAPL")))
    # The file and database are separate stores: preserve membership conservatively.
    result = service.list()
    assert "AAPL" in result["stock_codes"]
    assert result["items"][-1]["protected"]
    assert service.history()["items"] == []
    result = service.apply_review(review(service, decision("AAPL", "remove"), run_id="recovery"))
    assert result["changes"][0]["reason"] == "manual_watch_protected"
