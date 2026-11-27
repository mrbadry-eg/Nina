"""Phase 1 / Layer 2: deterministic Source-of-Truth resolver."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from silicium_loader import REPO_ROOT, load_module

_tr = load_module("04_SOURCE_OF_TRUTH/truth_resolver.py", "silicium_truth_resolver")
_audit = load_module("15_OBSERVABILITY/audit_logger.py", "silicium_audit_logger")
_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")

NOW = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
Claim = _tr.Claim


def _c(key, value, level, source, age_s=0, **kw):
    obs = None if age_s is None else NOW - timedelta(seconds=age_s)
    return Claim(key, value, level, source, obs, **kw)


def _resolver(tmp_path: Path | None = None, **kw):
    audit = None
    if tmp_path is not None:
        audit = _audit.AuditLogger(audit_path=tmp_path / "audit.jsonl", incidents_dir=tmp_path / "inc")
    return _tr.TruthResolver(audit_logger=audit, **kw), audit


def _audit_events(tmp_path: Path) -> list[dict]:
    p = tmp_path / "audit.jsonl"
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def _product(**over):
    rec = {
        "product_id": "PROD-MIC-USB01", "shopify_product_id": "gid://shopify/Product/1", "name": "Creator USB Condenser Microphone",
        "brand": "Acme", "category": "Microphones", "sku": "SI-MIC-USB01", "price_egp": 1450, "stock_status": "in_stock",
        "stock_quantity": 12, "warranty_terms": None,
        "specifications": [{"key": "connection", "value": "USB-C", "unit": None, "value_type": "string", "source": "manufacturer_claim"}],
        "verification_source": "manufacturer_claim", "catalog_quality_score": 0.9, "last_verified": (NOW - timedelta(hours=1)).isoformat(),
    }
    rec.update(over)
    return rec


# ------------------------------------------------------------------ config


def test_levels_and_thresholds_loaded_from_source_of_truth_json() -> None:
    r, _ = _resolver()
    assert sorted(r.levels) == list(range(1, 9))
    assert r.threshold(3) == 300 and r.threshold(4) == 86400
    assert r.threshold(1) is None and r.threshold(2) is None and r.threshold(7) is None
    assert r.conflict_policy["lower_level_number_wins"] is True


def test_invalid_truth_config_is_rejected() -> None:
    bad = json.loads((REPO_ROOT / "04_SOURCE_OF_TRUTH" / "source_of_truth.json").read_text(encoding="utf-8"))
    bad["conflict_resolution_policy"]["log_discrepancy"] = False  # schema pins this to true
    with pytest.raises(_sv.SchemaValidationError):
        _tr.TruthResolver(truth_config=bad)


# ------------------------------------------------------------------ precedence


def test_lower_level_number_wins_for_every_adjacent_pair() -> None:
    r, _ = _resolver()
    for hi in range(1, 7):
        lo = hi + 1
        age = 0  # fresh for every level
        res = r.resolve("k", [_c("k", "low", lo, "b", age), _c("k", "high", hi, "a", age)], now=NOW, require_authoritative=False)
        assert res["value"] == "high" and res["winning_claim"]["level"] == hi, (hi, lo)
        assert len(res["discrepancies"]) == 1


def test_spec_trap_stale_creative_memory_price_never_beats_catalog() -> None:
    r, _ = _resolver()
    claims = _tr.TruthResolver().claims_from_product_truth(_product())
    price = [c for c in claims if c.fact_key.endswith("price_egp")]
    creative = _c("product:PROD-MIC-USB01:price_egp", 1200, 7, "creative_memory", age_s=90 * 86400)
    res = r.resolve("product:PROD-MIC-USB01:price_egp", price + [creative], now=NOW)
    assert res["status"] == "resolved" and res["value"] == 1450
    assert res["discrepancies"][0]["losing"]["level"] == 7
    assert res["epistemic_label"] == "retrieved_live_data"


def test_result_is_independent_of_input_order() -> None:
    r, _ = _resolver()
    claims = [
        _c("k", 1, 3, "shopify", 10), _c("k", 2, 4, "catalog", 100), _c("k", 3, 7, "history", None),
        _c("k", 4, 8, "ai", 0), _c("k", 1, 3, "shopify-b", 20),
    ]
    baseline = r.resolve("k", claims, now=NOW)
    for seed in range(8):
        shuffled = claims[:]
        random.Random(seed).shuffle(shuffled)
        again = r.resolve("k", shuffled, now=NOW)
        assert again["value"] == baseline["value"]
        assert again["winning_claim"]["source"] == baseline["winning_claim"]["source"]
        assert again["status"] == baseline["status"]
        assert [d["type"] for d in again["discrepancies"]] == [d["type"] for d in baseline["discrepancies"]]


def test_numeric_equality_ignores_int_float_representation() -> None:
    r, _ = _resolver()
    res = r.resolve("k", [_c("k", 1450, 3, "a", 1), _c("k", 1450.0, 4, "b", 1)], now=NOW)
    assert res["discrepancies"] == []


# ------------------------------------------------------------------ staleness


@pytest.mark.parametrize("age,stale", [(299, False), (300, False), (301, True)])
def test_live_commerce_staleness_boundary_is_300_seconds(age: int, stale: bool) -> None:
    r, _ = _resolver()
    f = r.freshness(_c("k", 1, 3, "shopify", age), now=NOW)
    assert f["stale"] is stale and f["threshold_seconds"] == 300


def test_catalog_staleness_boundary_is_86400_seconds() -> None:
    r, _ = _resolver()
    assert r.freshness(_c("k", 1, 4, "cat", 86400), now=NOW)["stale"] is False
    assert r.freshness(_c("k", 1, 4, "cat", 86401), now=NOW)["stale"] is True


def test_unknown_observation_time_and_future_timestamps_fail_closed() -> None:
    r, _ = _resolver()
    assert r.freshness(_c("k", 1, 3, "s", None), now=NOW)["reason"] == "unknown_observation_time"
    assert r.freshness(_c("k", 1, 3, "s", -60), now=NOW)["reason"] == "observed_in_future"
    assert r.freshness(_c("k", 1, 3, "s", -2), now=NOW)["stale"] is False  # within clock-skew tolerance


def test_stale_live_data_blocks_instead_of_falling_back(tmp_path: Path) -> None:
    r, _ = _resolver(tmp_path)
    claims = [_c("stock", 0, 3, "shopify", 400), _c("stock", 5, 4, "catalog", 3600), _c("stock", 9, 7, "history", None)]
    res = r.resolve("stock", claims, now=NOW, task_id="TASK-20261125-TRU1")
    assert res["status"] == "blocked_stale_live_data"
    assert res["value"] is None and res["authoritative"] is False
    assert res["blocked"]["levels"] == [3] and "Refresh Level 3" in res["blocked"]["required_action"]
    assert res["escalation"]["required"] is True
    events = _audit_events(tmp_path)
    assert any(e["action"] == "truth_resolve" and e["outcome"] == "blocked" for e in events)


def test_stale_catalog_blocks_and_does_not_fall_back_to_history() -> None:
    r, _ = _resolver()
    res = r.resolve("p", [_c("p", 1450, 4, "cat", 90000), _c("p", 1200, 7, "creative", None)], now=NOW)
    assert res["status"] == "blocked_stale_catalog" and res["value"] is None


def test_fresh_higher_authority_claim_overrides_stale_live_claim() -> None:
    r, _ = _resolver()
    res = r.resolve("k", [_c("k", "policy", 2, "pol", 0), _c("k", "live", 3, "shopify", 999)], now=NOW)
    assert res["status"] == "resolved" and res["value"] == "policy"
    assert [d["type"] for d in res["discrepancies"]] == ["stale_excluded"]


def test_stale_docs_are_excluded_not_blocking() -> None:
    r, _ = _resolver()
    month = 31 * 86400
    res = r.resolve("k", [_c("k", "old-doc", 5, "git", month), _c("k", "hist", 7, "log", None)], now=NOW, require_authoritative=False)
    assert res["status"] == "historical_only" and res["value"] == "hist"
    assert res["excluded"] and res["excluded"][0]["reason"].startswith("stale")


def test_max_age_override_can_only_tighten() -> None:
    r, _ = _resolver()
    c = _c("k", 1, 3, "s", 100)
    assert r.freshness(c, now=NOW)["stale"] is False
    assert r.freshness(c, now=NOW, max_age_seconds={3: 60})["stale"] is True
    assert r.freshness(c, now=NOW, max_age_seconds={3: 10_000})["threshold_seconds"] == 300


def test_assert_fresh_raises_with_details_and_passes_when_fresh() -> None:
    r, _ = _resolver()
    assert r.assert_fresh([_c("a", 1, 3, "s", 10)], now=NOW)[0]["stale"] is False
    with pytest.raises(_tr.StaleLiveDataError) as exc:
        r.assert_fresh([_c("a", 1, 3, "s", 10), _c("b", 2, 3, "s", 500)], now=NOW)
    assert "STALE_LIVE_DATA" in str(exc.value) and exc.value.stale[0]["fact_key"] == "b"


# ------------------------------------------------------------------ non-authoritative + conflicts


def test_ai_inference_and_history_are_never_authoritative() -> None:
    r, _ = _resolver()
    ai = r.resolve("k", [_c("k", "guess", 8, "model", 0)], now=NOW)
    assert ai["status"] == "inference_only" and ai["authoritative"] is False
    assert ai["epistemic_label"] == "inference" and ai["escalation"]["required"] is True
    hist = r.resolve("k", [_c("k", "old", 7, "log", None)], now=NOW)
    assert hist["status"] == "historical_only" and hist["authoritative"] is False
    relaxed = r.resolve("k", [_c("k", "old", 7, "log", None)], now=NOW, require_authoritative=False)
    assert relaxed["escalation"]["required"] is False


def test_ai_inference_loses_to_any_other_level() -> None:
    r, _ = _resolver()
    res = r.resolve("k", [_c("k", "ai", 8, "model", 0), _c("k", "doc", 5, "git", 10)], now=NOW)
    assert res["value"] == "doc" and res["authoritative"] is True


def test_same_level_conflict_newest_wins_on_timestamped_levels() -> None:
    r, _ = _resolver()
    res = r.resolve("k", [_c("k", "old", 3, "read-1", 120), _c("k", "new", 3, "read-2", 10)], now=NOW)
    assert res["value"] == "new" and res["status"] == "resolved"
    d = res["discrepancies"][0]
    assert d["type"] == "same_level_conflict" and d["resolution"] == "newest_wins"


def test_same_level_conflict_with_equal_timestamps_is_unresolved_and_escalates() -> None:
    r, _ = _resolver()
    res = r.resolve("k", [_c("k", 1, 3, "a", 10), _c("k", 2, 3, "b", 10)], now=NOW)
    assert res["status"] == "unresolved_conflict" and res["value"] is None
    assert "source_conflict" in res["escalation"]["reasons"]


def test_same_level_conflict_on_law_or_policy_is_unresolved() -> None:
    r, _ = _resolver()
    res = r.resolve("k", [_c("k", "x", 2, "p1", 5), _c("k", "y", 2, "p2", 1)], now=NOW)
    assert res["status"] == "unresolved_conflict"


def test_no_evidence_escalates() -> None:
    r, _ = _resolver()
    res = r.resolve("k", [], now=NOW)
    assert res["status"] == "no_evidence" and res["escalation"]["required"] is True


def test_mixed_fact_keys_and_malformed_claims_are_rejected() -> None:
    r, _ = _resolver()
    with pytest.raises(_tr.InvalidClaimError):
        r.resolve("a", [_c("b", 1, 3, "s", 0)], now=NOW)
    with pytest.raises(_tr.InvalidClaimError):
        Claim("k", 1, 9, "s", NOW)
    with pytest.raises(_tr.InvalidClaimError):
        Claim("k", 1, 3, "s", datetime(2026, 1, 1))  # naive timestamp


def test_discrepancies_are_written_to_audit_trail_and_schema_valid(tmp_path: Path) -> None:
    r, _ = _resolver(tmp_path)
    r.resolve("k", [_c("k", "live", 3, "shopify", 5), _c("k", "doc", 5, "git", 5)], now=NOW, task_id="TASK-20261125-TRU2", agent="master-control-plane")
    events = _audit_events(tmp_path)
    assert len(events) == 1
    ev = events[0]
    assert ev["action"] == "truth_discrepancy" and ev["agent"] == "master-control-plane" and ev["task_id"] == "TASK-20261125-TRU2"
    _sv.CanonicalSchemaValidator().validate("17_audit_event.schema.json", ev)
    assert r.discrepancies and r.discrepancies[0]["fact_key"] == "k"


# ------------------------------------------------------------------ policy (Level 2)


def test_shipped_policies_are_proposed_so_policy_values_are_a_gap_that_escalates() -> None:
    r, _ = _resolver()
    assert r.policy_claims() == []
    gap_ids = {g["policy_id"] for g in r.policy_gaps()}
    assert "POL-WARRANTY-RETURNS-001" in gap_ids and len(gap_ids) == 4
    res = r.resolve_policy("POL-WARRANTY-RETURNS-001", "return_window_days", now=NOW)
    assert res["policy_gap"] is True and "policy_gap" in res["escalation"]["reasons"]
    assert res["status"] == "no_evidence"


def _approved_index(**over):
    entry = {
        "policy_id": "POL-SHIPPING-COURIER-001", "version": "1.0.0", "status": "approved",
        "approved_by": "founder:abdulrahman", "approved_at": (NOW - timedelta(days=2)).isoformat(),
        "machine_values": {"sla_unfulfilled_hours": 48}, "open_decisions": [],
    }
    entry.update(over)
    return {"policies": [entry]}


def test_approved_policy_becomes_level_2_and_overrides_live_store_text() -> None:
    r, _ = _resolver(policy_index=_approved_index())
    res = r.resolve_policy(
        "POL-SHIPPING-COURIER-001", "sla_unfulfilled_hours", now=NOW,
        extra_claims=[_c("policy:POL-SHIPPING-COURIER-001:sla_unfulfilled_hours", 72, 3, "shopify_store_text", 30)],
    )
    assert res["value"] == 48 and res["winning_claim"]["level"] == 2 and res["authoritative"] is True
    assert "policy_gap" not in res["escalation"]["reasons"]
    assert res["discrepancies"][0]["losing"]["level"] == 3


@pytest.mark.parametrize("bad", [{"approved_by": "agent.engineering"}, {"approved_by": None}, {"approved_at": None}, {"approved_at": "yesterday"}])
def test_invalid_policy_approval_is_rejected_and_logged(bad: dict, tmp_path: Path) -> None:
    r, _ = _resolver(tmp_path, policy_index=_approved_index(**bad))
    assert r.policy_claims() == []
    assert any(e["action"] == "truth_invalid_policy_approval" and e["outcome"] == "blocked" for e in _audit_events(tmp_path))
    assert r.resolve_policy("POL-SHIPPING-COURIER-001", "sla_unfulfilled_hours", now=NOW)["policy_gap"] is True


# ------------------------------------------------------------------ claim builders


def test_product_truth_claims_carry_provenance_and_observation_time() -> None:
    r, _ = _resolver()
    claims = r.claims_from_product_truth(_product())
    keys = {c.fact_key for c in claims}
    assert {"product:PROD-MIC-USB01:price_egp", "product:PROD-MIC-USB01:stock_status", "product:PROD-MIC-USB01:stock_quantity", "product:PROD-MIC-USB01:spec:connection"} <= keys
    assert "product:PROD-MIC-USB01:warranty_terms" not in keys  # null warranty is never turned into a claim
    spec = next(c for c in claims if c.fact_key.endswith("spec:connection"))
    assert spec.verification == "manufacturer_claim" and spec.level == 4
    assert all(c.observed_at is not None for c in claims)


def test_product_truth_claims_reject_schema_invalid_records() -> None:
    r, _ = _resolver()
    with pytest.raises(_sv.SchemaValidationError):
        r.claims_from_product_truth(_product(price_egp=-5))


def test_order_claims_from_gateway_result_require_success_and_use_executed_at(tmp_path: Path) -> None:
    gw_mod = load_module("08_TOOLS_MCP/tool_gateway.py", "silicium_tool_gateway")
    audit = _audit.AuditLogger(audit_path=tmp_path / "audit.jsonl", incidents_dir=tmp_path / "inc")
    gw = gw_mod.ToolGateway(audit_logger=audit)
    ok = gw.invoke(task_id="TASK-20261125-TRC1", agent_id="agent.operations", environment="dev", tool_name="shopify.orders.get", inputs={"order_id": "ORD-1042"})
    r, _ = _resolver()
    claims = r.claims_from_order_result(ok)
    by_key = {c.fact_key: c for c in claims}
    assert by_key["order:ORD-1042:fulfillment_status"].value == "unfulfilled" and by_key["order:ORD-1042:fulfillment_status"].level == 3
    assert by_key["order:ORD-1042:hours_since_created"].value == 68
    assert claims[0].observed_at.isoformat() == ok["executed_at"]
    missing = gw.invoke(task_id="TASK-20261125-TRC2", agent_id="agent.operations", environment="dev", tool_name="shopify.orders.get", inputs={"order_id": "ORD-9999"})
    with pytest.raises(_tr.InvalidClaimError):
        r.claims_from_order_result(missing)
