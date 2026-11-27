"""Phase 1 / Layer 1: Brand Bible and baseline policies stay aligned with frozen context and code."""

from __future__ import annotations

import json
import re

from silicium_loader import REPO_ROOT, load_module

_audit = load_module("15_OBSERVABILITY/audit_logger.py", "silicium_audit_logger")
_ops = load_module("05_AGENTS/operations/operations_agent.py", "silicium_operations_agent")

BC_DIR = REPO_ROOT / "03_BUSINESS_CONTEXT"
POLICIES_DIR = BC_DIR / "policies"
BRAND_BIBLE = BC_DIR / "BRAND_BIBLE.md"

EXPECTED_POLICY_IDS = {
    "POL-WARRANTY-RETURNS-001",
    "POL-PAYMENTS-001",
    "POL-SHIPPING-COURIER-001",
    "POL-DATA-MINIMIZATION-001",
}
RULE_PREFIX = {
    "POL-WARRANTY-RETURNS-001": "WR",
    "POL-PAYMENTS-001": "PAY",
    "POL-SHIPPING-COURIER-001": "SHP",
    "POL-DATA-MINIMIZATION-001": "DM",
}


def _business_context() -> dict:
    return json.loads((BC_DIR / "business_context.json").read_text(encoding="utf-8"))


def _index() -> dict:
    return json.loads((POLICIES_DIR / "policy_index.json").read_text(encoding="utf-8"))


def _policy_text(entry: dict) -> str:
    return (POLICIES_DIR / entry["path"]).read_text(encoding="utf-8")


def _header(text: str, label: str) -> str:
    match = re.search(rf"^- \*\*{re.escape(label)}:\*\* (.+)$", text, flags=re.MULTILINE)
    assert match, f"missing header '{label}'"
    return match.group(1).strip()


# ---------------------------------------------------------------- Brand Bible


def test_brand_bible_matches_business_context_voice_and_pillars() -> None:
    text = BRAND_BIBLE.read_text(encoding="utf-8")
    ctx = _business_context()["brand_positioning"]
    for attr in ctx["voice_attributes"]:
        assert f"| {attr} |" in text, f"voice attribute missing from Brand Bible: {attr}"
    for pillar in ctx["core_pillars"]:
        assert pillar in text, f"core pillar missing from Brand Bible: {pillar}"
    assert ctx["differentiation_rule"] in text


def test_brand_bible_language_rules_cover_ar_eg_en_bilingual() -> None:
    text = BRAND_BIBLE.read_text(encoding="utf-8")
    lp = _business_context()["language_policy"]
    assert lp["customer_facing_default"] == "ar-EG"
    for token in ("`ar-EG`", "`en`", "`bilingual`", "3.1", "3.2", "3.3"):
        assert token in text
    # Internal artifacts are English; customer-facing default is Egyptian Arabic.
    assert "always English" in text
    assert "Customer-facing" in text and "`ar-EG`" in text


def test_brand_bible_hard_bans_map_to_every_prohibited_practice() -> None:
    text = BRAND_BIBLE.read_text(encoding="utf-8")
    practices = _business_context()["prohibited_practices"]
    keywords = [
        "fake urgency",
        "empty hype",
        "invented or extrapolated",
        "fake reviews",
        "tested a product",
        "boilerplate",
        "promises of refunds",
    ]
    # Adding a prohibited practice to business_context.json must force a Brand Bible update.
    assert len(practices) == len(keywords)
    lowered = text.lower()
    for kw in keywords:
        assert kw in lowered, f"Brand Bible does not cover banned pattern: {kw}"
    assert [f"BAN-0{i}" in text for i in range(1, len(practices) + 1)] == [True] * len(practices)


def test_brand_bible_declares_proposed_status_and_truth_rules() -> None:
    text = BRAND_BIBLE.read_text(encoding="utf-8")
    assert _header(text, "Status").startswith("PROPOSED")
    for marker in ("manufacturer_claim", "silicium_verified", "24 hours", "Creative memory is not product truth"):
        assert marker in text


# -------------------------------------------------------------------- Policies


def test_policy_index_lists_expected_policies_and_files_exist() -> None:
    idx = _index()
    ids = {p["policy_id"] for p in idx["policies"]}
    assert ids == EXPECTED_POLICY_IDS
    assert len(ids) == len(idx["policies"])  # unique
    assert idx["authority_level"] == 2
    assert idx["owner"] == "founder:abdulrahman"
    for entry in idx["policies"]:
        assert (POLICIES_DIR / entry["path"]).is_file()


def test_policy_markdown_headers_match_index() -> None:
    for entry in _index()["policies"]:
        text = _policy_text(entry)
        assert _header(text, "Policy ID") == entry["policy_id"]
        assert _header(text, "Version") == entry["version"]
        assert _header(text, "Status").lower() == entry["status"].lower()


def test_no_policy_is_approved_without_founder_signature() -> None:
    for entry in _index()["policies"]:
        if entry["status"] == "approved":
            assert entry["approved_by"] == "founder:abdulrahman"
            assert entry["approved_at"]
        else:
            assert entry["status"] == "proposed"
            assert entry["approved_by"] is None and entry["approved_at"] is None


def test_policy_rule_ids_are_unique_and_prefixed() -> None:
    seen: set[str] = set()
    for entry in _index()["policies"]:
        prefix = RULE_PREFIX[entry["policy_id"]]
        rules = re.findall(rf"^\| ({prefix}-\d{{2}}) \|", _policy_text(entry), flags=re.MULTILINE)
        assert rules, f"no rules found for {entry['policy_id']}"
        assert len(rules) == len(set(rules))
        assert seen.isdisjoint(rules)
        seen.update(rules)


def test_open_decisions_are_declared_and_unset_while_proposed() -> None:
    for entry in _index()["policies"]:
        text = _policy_text(entry)
        for od in entry["open_decisions"]:
            row = re.search(rf"^\| {od} \|.*\| (\S+) \|$", text, flags=re.MULTILINE)
            assert row, f"open decision {od} not declared in {entry['path']}"
            assert row.group(1) == "UNSET"
        declared = set(re.findall(r"^\| (OD-[A-Z]+-\d+) \|", text, flags=re.MULTILINE))
        assert declared == set(entry["open_decisions"])


def test_policies_do_not_invent_return_windows_or_fees() -> None:
    warranty = (POLICIES_DIR / "warranty_returns_policy.md").read_text(encoding="utf-8")
    assert not re.search(r"\b\d+\s*(?:-|\s)?days?\b", warranty, flags=re.IGNORECASE)
    payments = (POLICIES_DIR / "payments_policy.md").read_text(encoding="utf-8")
    assert not re.search(r"\b\d+\s*(?:EGP|%)", payments)


def test_machine_values_align_with_frozen_context_and_code() -> None:
    by_id = {p["policy_id"]: p for p in _index()["policies"]}
    bc = _business_context()

    pay = by_id["POL-PAYMENTS-001"]["machine_values"]
    assert pay["accepted_payment_methods"] == bc["payment_methods"]
    assert pay["currency"] == bc["currency"]

    ship = by_id["POL-SHIPPING-COURIER-001"]["machine_values"]
    assert ship["sla_unfulfilled_hours"] == _ops.OperationsAgent.SLA_UNFULFILLED_HOURS

    dm = by_id["POL-DATA-MINIMIZATION-001"]["machine_values"]
    assert set(dm["audit_redaction_keys"]) == set(_audit.REDACT_KEYS)
    model_policy = dm["model_call_data_policy"]
    assert model_policy["default_data_class"] == "internal"
    assert model_policy["data_classes"] == ["public", "internal", "restricted"]
    assert model_policy["free_endpoint_max_data_class"] == "public"
    assert model_policy["restricted_endpoint_tier"] == "local"
    policy_text = _policy_text(by_id["POL-DATA-MINIMIZATION-001"])
    assert "| DM-11 |" in policy_text and _header(policy_text, "Status") == "PROPOSED"


def test_business_context_docs_contain_no_pii_or_secrets() -> None:
    files = [BRAND_BIBLE, *POLICIES_DIR.glob("*")]
    phone = re.compile(r"(?:\+?20|0)1[0-25]\d{8}")
    email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
    secret = re.compile(r"(?:sk-|shpat_|xox[bp]-|AKIA)[A-Za-z0-9_-]{8,}")
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not phone.search(text), path.name
        assert not email.search(text), path.name
        assert not secret.search(text), path.name
