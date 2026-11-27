"""Deterministic Source-of-Truth resolver (SILICIUM-SPEC-001 section 4.1).

Given competing *claims* about one fact, the resolver picks a winner without any
model involvement:

* Lower level number wins (1 Law > 2 Policy > 3 Live Commerce > 4 Catalog >
  5 Docs > 6 Support > 7 History > 8 AI Inference).
* Staleness thresholds come from ``source_of_truth.json`` (300s live commerce,
  86400s catalog, 30 days docs/support). A claim older than its threshold, or with
  an unknown observation time, is never allowed to win.
* ``block_on_stale_live_data``: when the highest-authority claim present is a stale
  Level 3 (live commerce) or Level 4 (catalog) claim, the resolver *blocks* instead
  of silently falling back to lower-authority data (spec 4.1 and 7.1).
* Level 7 (history) and Level 8 (AI inference) winners are never authoritative and
  are flagged so callers must label them and, when authority is required, escalate.
* Every disagreement between claims is recorded as a discrepancy and, when an audit
  logger is attached, written to the append-only audit trail for ops review.
* Policy claims (Level 2) only exist for policies that the Founder approved in
  ``03_BUSINESS_CONTEXT/policies/policy_index.json``. A missing or invalid approval
  is a *policy gap* and escalates (``escalate_on_policy_gap``).

The resolver is pure and deterministic: the result does not depend on the order in
which claims are supplied.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from silicium_loader import REPO_ROOT, load_module

_sv = load_module("01_ARCHITECTURE/validators/schema_validator.py", "silicium_schema_validator")

SOURCE_OF_TRUTH_PATH = REPO_ROOT / "04_SOURCE_OF_TRUTH" / "source_of_truth.json"
FOUNDER_CONTEXT_PATH = REPO_ROOT / "02_FOUNDER_CONTEXT" / "founder_context.json"
POLICY_INDEX_PATH = REPO_ROOT / "03_BUSINESS_CONTEXT" / "policies" / "policy_index.json"

FOUNDER_PRINCIPAL = "founder:abdulrahman"

# Levels where stale data must block (spec 4.1: live commerce 5 min; spec 7.1: catalog 24 h halts).
BLOCKING_STALE_LEVELS = frozenset({3, 4})
# Levels with an observation time where the newest same-level claim may break a tie.
RECENCY_TIEBREAK_LEVELS = frozenset({3, 4, 5, 6})
# Levels that are never authoritative for current price, stock, specs or policy.
NON_AUTHORITATIVE_LEVELS = frozenset({7, 8})
# Tolerated clock skew for claims observed slightly "in the future".
FUTURE_TOLERANCE_SECONDS = 5.0

EPISTEMIC_LABELS = {
    1: "verified_fact",
    2: "verified_fact",
    3: "retrieved_live_data",
    4: "retrieved_live_data",
    5: "verified_fact",
    6: "verified_fact",
    7: "historical_reference",
    8: "inference",
}

ORDER_FACT_FIELDS = (
    "status",
    "fulfillment_status",
    "payment_method",
    "total_egp",
    "hours_since_created",
    "city",
)


class TruthResolverError(Exception):
    """Base error for the Source-of-Truth layer."""


class InvalidClaimError(TruthResolverError, ValueError):
    """Raised when a claim is malformed (bad level, naive timestamp, mixed fact keys...)."""


class StaleLiveDataError(TruthResolverError, PermissionError):
    """Raised when evidence is too old to act on."""

    def __init__(self, message: str, stale: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.stale = stale


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise InvalidClaimError(f"{label} must be timezone-aware")
    return value


def _parse_ts(value: str | datetime | None, label: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _require_aware(value, label)
    try:
        return _require_aware(datetime.fromisoformat(value), label)
    except (TypeError, ValueError) as exc:
        raise InvalidClaimError(f"{label} is not a valid ISO-8601 timestamp: {value!r}") from exc


def _normalize(value: Any) -> Any:
    """Normalise numbers so 1450 and 1450.0 compare equal; recurse into containers."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        as_float = float(value)
        return int(as_float) if as_float.is_integer() else as_float
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def _value_key(value: Any) -> str:
    return _sv.canonical_json(_normalize(value))


@dataclass(frozen=True)
class Claim:
    """One observation about one fact, from one source, at one authority level."""

    fact_key: str
    value: Any
    level: int
    source: str
    observed_at: datetime | None
    verification: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.fact_key, str) or not self.fact_key.strip():
            raise InvalidClaimError("fact_key must be a non-empty string")
        if isinstance(self.level, bool) or not isinstance(self.level, int) or not 1 <= self.level <= 8:
            raise InvalidClaimError(f"level must be an integer 1..8, got {self.level!r}")
        if not isinstance(self.source, str) or not self.source.strip():
            raise InvalidClaimError("source must be a non-empty string")
        if self.observed_at is not None:
            _require_aware(self.observed_at, "observed_at")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Claim":
        return cls(
            fact_key=data["fact_key"],
            value=data.get("value"),
            level=data["level"],
            source=data["source"],
            observed_at=_parse_ts(data.get("observed_at"), "observed_at"),
            verification=data.get("verification"),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_key": self.fact_key,
            "value": self.value,
            "level": self.level,
            "source": self.source,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "verification": self.verification,
            "metadata": dict(self.metadata),
        }


def _sort_key(claim: Claim) -> tuple[Any, ...]:
    """Deterministic order: level asc, newest first, then source and value for stable ties."""
    ts = claim.observed_at.timestamp() if claim.observed_at else float("-inf")
    return (claim.level, -ts, claim.source, _value_key(claim.value))


class TruthResolver:
    """Resolves competing claims according to the 8-level Source-of-Truth hierarchy."""

    def __init__(
        self,
        *,
        truth_config: dict[str, Any] | None = None,
        founder_context: dict[str, Any] | None = None,
        policy_index: dict[str, Any] | None = None,
        audit_logger: Any | None = None,
        environment: str = "dev",
    ) -> None:
        self.validator = _sv.CanonicalSchemaValidator()
        config = truth_config or json.loads(SOURCE_OF_TRUTH_PATH.read_text(encoding="utf-8"))
        self.validator.validate("03_source_of_truth.schema.json", config)
        self.levels: dict[int, dict[str, Any]] = {lvl["level"]: lvl for lvl in config["levels"]}
        if sorted(self.levels) != list(range(1, 9)):
            raise TruthResolverError("source_of_truth.json must define exactly levels 1..8")
        self.conflict_policy: dict[str, Any] = config["conflict_resolution_policy"]

        founder = founder_context or json.loads(FOUNDER_CONTEXT_PATH.read_text(encoding="utf-8"))
        self.escalation_rules: dict[str, bool] = dict(founder.get("escalation_rules", {}))

        if policy_index is None:
            policy_index = json.loads(POLICY_INDEX_PATH.read_text(encoding="utf-8")) if POLICY_INDEX_PATH.exists() else {"policies": []}
        self.policy_index = policy_index

        self.audit = audit_logger
        self.environment = environment
        self.discrepancies: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ freshness

    def threshold(self, level: int) -> int | None:
        return self.levels[level]["staleness_threshold_seconds"]

    def freshness(
        self,
        claim: Claim,
        *,
        now: datetime | None = None,
        max_age_seconds: dict[int, int] | None = None,
    ) -> dict[str, Any]:
        """Return freshness facts for one claim. Overrides can only tighten a threshold."""
        current = _require_aware(now or _utc_now(), "now")
        threshold = self.threshold(claim.level)
        if threshold is not None and max_age_seconds and claim.level in max_age_seconds:
            threshold = min(threshold, max_age_seconds[claim.level])

        base = {"level": claim.level, "threshold_seconds": threshold, "age_seconds": None, "stale": False, "reason": None}

        if claim.level == 8:  # ephemeral, task-scoped, never authoritative
            return {**base, "ephemeral": True}
        if threshold is None:  # statutory law, policy, history: governed by approval, not age
            return base
        if claim.observed_at is None:
            return {**base, "stale": True, "reason": "unknown_observation_time"}

        age = (current - claim.observed_at).total_seconds()
        if age < -FUTURE_TOLERANCE_SECONDS:
            return {**base, "age_seconds": age, "stale": True, "reason": "observed_in_future"}
        age = max(age, 0.0)
        stale = age > threshold
        return {**base, "age_seconds": age, "stale": stale, "reason": "older_than_threshold" if stale else None}

    def assert_fresh(
        self,
        claims: Iterable[Claim],
        *,
        now: datetime | None = None,
        max_age_seconds: dict[int, int] | None = None,
    ) -> list[dict[str, Any]]:
        """Raise ``StaleLiveDataError`` unless every claim is within its freshness threshold.

        Used by the Tool Gateway as an execution precondition for evidence-backed actions.
        """
        current = now or _utc_now()
        records: list[dict[str, Any]] = []
        stale: list[dict[str, Any]] = []
        for claim in claims:
            fresh = self.freshness(claim, now=current, max_age_seconds=max_age_seconds)
            record = {"fact_key": claim.fact_key, "source": claim.source, **fresh}
            records.append(record)
            if fresh["stale"]:
                stale.append(record)
        if stale:
            detail = "; ".join(
                f"{s['fact_key']} (L{s['level']}, age={s['age_seconds']}, limit={s['threshold_seconds']}, {s['reason']})" for s in stale
            )
            raise StaleLiveDataError(f"STALE_LIVE_DATA: {detail}", stale)
        return records

    # ------------------------------------------------------------------ resolution

    def resolve(
        self,
        fact_key: str,
        claims: Iterable[Claim],
        *,
        now: datetime | None = None,
        max_age_seconds: dict[int, int] | None = None,
        require_authoritative: bool = True,
        task_id: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        current = _require_aware(now or _utc_now(), "now")
        claim_list = sorted(claims, key=_sort_key)
        for claim in claim_list:
            if claim.fact_key != fact_key:
                raise InvalidClaimError(f"claim for '{claim.fact_key}' supplied while resolving '{fact_key}'")

        considered = [
            {"claim": c.to_dict(), "freshness": self.freshness(c, now=current, max_age_seconds=max_age_seconds)}
            for c in claim_list
        ]
        fresh_claims = [c for c, info in zip(claim_list, considered) if not info["freshness"]["stale"]]
        stale_claims = [c for c, info in zip(claim_list, considered) if info["freshness"]["stale"]]
        stale_info = {id(c): info["freshness"] for c, info in zip(claim_list, considered)}

        result: dict[str, Any] = {
            "fact_key": fact_key,
            "status": None,
            "authoritative": False,
            "value": None,
            "epistemic_label": None,
            "winning_claim": None,
            "freshness": None,
            "considered": considered,
            "excluded": [],
            "discrepancies": [],
            "blocked": None,
            "escalation": {"required": False, "reasons": [], "required_human_decision": None},
            "resolved_at": current.isoformat(),
        }

        if not claim_list:
            self._escalate(result, "uncertainty", f"No evidence available for '{fact_key}'.")
            result["status"] = "no_evidence"
            return self._finish(result, started, task_id, agent)

        # 1) Block on stale live/catalog data that would have outranked every fresh claim.
        best_fresh_level = min((c.level for c in fresh_claims), default=None)
        if self.conflict_policy.get("block_on_stale_live_data", True):
            blocking = [
                c for c in stale_claims
                if c.level in BLOCKING_STALE_LEVELS and (best_fresh_level is None or c.level < best_fresh_level)
            ]
            if blocking:
                return self._blocked(result, blocking, claim_list, stale_info, current, started, task_id, agent)

        # 2) Stale claims below the blocking levels are excluded (they cannot win).
        for c in stale_claims:
            result["excluded"].append({"claim": c.to_dict(), "reason": f"stale:{stale_info[id(c)]['reason']}"})

        if not fresh_claims:
            self._escalate(result, "uncertainty", f"Every claim for '{fact_key}' is stale or unusable.")
            result["status"] = "no_evidence"
            self._record_stale_discrepancies(result, None, stale_claims, current, task_id, agent)
            return self._finish(result, started, task_id, agent)

        # 3) Pick the lowest level; resolve same-level disagreement deterministically or escalate.
        top_level = fresh_claims[0].level
        top_claims = [c for c in fresh_claims if c.level == top_level]
        winner, how = self._pick_within_level(top_level, top_claims)

        if winner is None:
            result["status"] = "unresolved_conflict"
            self._record_conflict_discrepancies(result, top_claims[0], top_claims, "unresolved", current, task_id, agent)
            self._escalate(
                result, "source_conflict",
                f"Sources at Level {top_level} disagree on '{fact_key}' and cannot be ordered deterministically.",
            )
            return self._finish(result, started, task_id, agent)

        if how == "newest_wins":
            self._record_conflict_discrepancies(result, winner, top_claims, "newest_wins", current, task_id, agent)

        # 4) Record every other disagreement against the winner.
        for c in fresh_claims:
            if c.level != winner.level and _value_key(c.value) != _value_key(winner.value):
                self._add_discrepancy(
                    result, "cross_level_conflict", winner, c, "lower_level_number_wins", current, task_id, agent,
                    needs_review=winner.level <= 6 and c.level <= 6,
                )
        self._record_stale_discrepancies(result, winner, stale_claims, current, task_id, agent)

        # 5) Describe the outcome.
        result["winning_claim"] = winner.to_dict()
        result["value"] = winner.value
        result["freshness"] = self.freshness(winner, now=current, max_age_seconds=max_age_seconds)
        result["epistemic_label"] = EPISTEMIC_LABELS[winner.level]
        if winner.level == 8:
            result["status"] = "inference_only"
        elif winner.level == 7:
            result["status"] = "historical_only"
        else:
            result["status"] = "resolved"
            result["authoritative"] = True
        if winner.level in NON_AUTHORITATIVE_LEVELS and require_authoritative:
            self._escalate(
                result, "uncertainty",
                f"Only Level {winner.level} ({self.levels[winner.level]['label']}) evidence exists for '{fact_key}'; "
                "it must be labelled and cannot be stated as current fact.",
            )
        return self._finish(result, started, task_id, agent)

    def resolve_many(
        self,
        claims: Iterable[Claim],
        *,
        now: datetime | None = None,
        max_age_seconds: dict[int, int] | None = None,
        require_authoritative: bool = True,
        task_id: str | None = None,
        agent: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        grouped: dict[str, list[Claim]] = {}
        for claim in claims:
            grouped.setdefault(claim.fact_key, []).append(claim)
        return {
            key: self.resolve(
                key, grouped[key], now=now, max_age_seconds=max_age_seconds,
                require_authoritative=require_authoritative, task_id=task_id, agent=agent,
            )
            for key in sorted(grouped)
        }

    # ------------------------------------------------------------------ policy (Level 2)

    def policy_claims(self) -> list[Claim]:
        """Level 2 claims from Founder-approved policies only. Invalid approvals are rejected and logged."""
        claims: list[Claim] = []
        for policy in self.policy_index.get("policies", []):
            if policy.get("status") != "approved":
                continue
            valid = policy.get("approved_by") == FOUNDER_PRINCIPAL and bool(policy.get("approved_at"))
            approved_at = None
            if valid:
                try:
                    approved_at = _parse_ts(policy["approved_at"], "approved_at")
                except InvalidClaimError:
                    valid = False
            if not valid:
                self._log_event(
                    action="truth_invalid_policy_approval", outcome="blocked", risk="high",
                    resource=policy.get("policy_id", "unknown"),
                    inputs={"policy_id": policy.get("policy_id"), "approved_by": policy.get("approved_by")},
                    outputs={"reason": "approval must come from founder:abdulrahman with a valid timestamp"},
                    task_id=None, agent=None, started=time.perf_counter(),
                )
                continue
            for key, value in sorted(policy.get("machine_values", {}).items()):
                claims.append(
                    Claim(
                        fact_key=f"policy:{policy['policy_id']}:{key}",
                        value=value,
                        level=2,
                        source=f"policy:{policy['policy_id']}@{policy.get('version')}",
                        observed_at=approved_at,
                        verification="founder_approved",
                    )
                )
        return claims

    def policy_gaps(self) -> list[dict[str, Any]]:
        """Policies that are not yet authoritative, with the decisions the Founder still owes."""
        approved = {c.source.split("@")[0].removeprefix("policy:") for c in self.policy_claims()}
        gaps = []
        for policy in self.policy_index.get("policies", []):
            if policy["policy_id"] not in approved and policy.get("status") != "approved":
                gaps.append({"policy_id": policy["policy_id"], "status": policy.get("status"), "open_decisions": list(policy.get("open_decisions", []))})
        return gaps

    def resolve_policy(
        self,
        policy_id: str,
        key: str,
        *,
        extra_claims: Iterable[Claim] = (),
        now: datetime | None = None,
        task_id: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a policy value. Without an approved policy this is a *policy gap* and escalates."""
        fact_key = f"policy:{policy_id}:{key}"
        claims = [c for c in self.policy_claims() if c.fact_key == fact_key]
        claims.extend(extra_claims)
        policy_level_claims = [c for c in claims if c.level == 2]
        result = self.resolve(fact_key, claims, now=now, task_id=task_id, agent=agent)
        if not policy_level_claims:
            self._escalate(result, "policy_gap", f"No Founder-approved value for {fact_key}; Tier 0 approval of {policy_id} is required.")
            result["policy_gap"] = True
        return result

    # ------------------------------------------------------------------ claim builders

    def claims_from_product_truth(self, record: dict[str, Any], *, level: int = 4, source: str = "shopify_catalog") -> list[Claim]:
        """Catalog claims from a schema-valid ``04_product_truth`` record (observed at ``last_verified``)."""
        self.validator.validate("04_product_truth.schema.json", record)
        observed = _parse_ts(record["last_verified"], "last_verified")
        pid = record["product_id"]
        meta = {"verification_source": record["verification_source"]}
        claims = [
            Claim(f"product:{pid}:price_egp", record["price_egp"], level, source, observed, record["verification_source"], meta),
            Claim(f"product:{pid}:stock_status", record["stock_status"], level, source, observed, record["verification_source"], meta),
        ]
        if record.get("stock_quantity") is not None:
            claims.append(Claim(f"product:{pid}:stock_quantity", record["stock_quantity"], level, source, observed, record["verification_source"], meta))
        if record.get("warranty_terms") is not None:
            claims.append(Claim(f"product:{pid}:warranty_terms", record["warranty_terms"], level, source, observed, record["verification_source"], meta))
        for spec in record["specifications"]:
            claims.append(
                Claim(f"product:{pid}:spec:{spec['key']}", spec["value"], level, source, observed, spec["source"], {"unit": spec.get("unit")})
            )
        return claims

    def claims_from_order_result(self, execution_result: dict[str, Any], *, level: int = 3) -> list[Claim]:
        """Level 3 claims from a successful ``shopify.orders.get`` execution result."""
        self.validator.validate("10_execution_result.schema.json", execution_result)
        if execution_result["tool_name"] != "shopify.orders.get":
            raise InvalidClaimError(f"unsupported tool for order claims: {execution_result['tool_name']}")
        if execution_result["status"] != "success" or not execution_result.get("outputs"):
            raise InvalidClaimError("order claims require a successful execution result with outputs")
        out = execution_result["outputs"]
        observed = _parse_ts(execution_result["executed_at"], "executed_at")
        source = f"tool:{execution_result['tool_name']}:{execution_result['execution_id']}"
        return [
            Claim(f"order:{out['order_id']}:{name}", out[name], level, source, observed, execution_result.get("verification_method"))
            for name in ORDER_FACT_FIELDS
            if name in out
        ]

    # ------------------------------------------------------------------ internals

    def _pick_within_level(self, level: int, claims: list[Claim]) -> tuple[Claim | None, str | None]:
        if len({_value_key(c.value) for c in claims}) == 1:
            return sorted(claims, key=_sort_key)[0], "agreement"
        if level in RECENCY_TIEBREAK_LEVELS:
            newest = max(c.observed_at for c in claims if c.observed_at is not None)
            newest_claims = [c for c in claims if c.observed_at == newest]
            if len({_value_key(c.value) for c in newest_claims}) == 1:
                return sorted(newest_claims, key=_sort_key)[0], "newest_wins"
        return None, "unresolved"

    def _blocked(
        self, result: dict[str, Any], blocking: list[Claim], claim_list: list[Claim],
        stale_info: dict[int, dict[str, Any]], current: datetime, started: float,
        task_id: str | None, agent: str | None,
    ) -> dict[str, Any]:
        top = blocking[0]
        info = stale_info[id(top)]
        result["status"] = "blocked_stale_live_data" if top.level == 3 else "blocked_stale_catalog"
        result["blocked"] = {
            "levels": sorted({c.level for c in blocking}),
            "required_action": (
                f"Refresh Level {top.level} data from '{top.source}' "
                f"(age={info['age_seconds']}s, limit={info['threshold_seconds']}s, reason={info['reason']})."
            ),
        }
        for c in claim_list:
            result["excluded"].append({"claim": c.to_dict(), "reason": "blocked_pending_refresh"})
        others = [c for c in claim_list if c is not top and _value_key(c.value) != _value_key(top.value)]
        for other in others:
            self._add_discrepancy(result, "stale_authoritative_source", top, other, "blocked_pending_refresh", current, task_id, agent, needs_review=True)
        self._escalate(result, "uncertainty", result["blocked"]["required_action"])
        return self._finish(result, started, task_id, agent)

    def _record_stale_discrepancies(
        self, result: dict[str, Any], winner: Claim | None, stale_claims: list[Claim],
        current: datetime, task_id: str | None, agent: str | None,
    ) -> None:
        for stale in stale_claims:
            if winner is None or _value_key(stale.value) != _value_key(winner.value):
                self._add_discrepancy(
                    result, "stale_excluded", winner or stale, stale, "stale_claims_cannot_win", current, task_id, agent, needs_review=False,
                )

    def _record_conflict_discrepancies(
        self, result: dict[str, Any], winner: Claim, group: list[Claim], resolution: str,
        current: datetime, task_id: str | None, agent: str | None,
    ) -> None:
        for c in group:
            if c is not winner and _value_key(c.value) != _value_key(winner.value):
                self._add_discrepancy(result, "same_level_conflict", winner, c, resolution, current, task_id, agent, needs_review=True)

    def _add_discrepancy(
        self, result: dict[str, Any], kind: str, winner: Claim, loser: Claim, resolution: str,
        current: datetime, task_id: str | None, agent: str | None, *, needs_review: bool,
    ) -> None:
        record = {
            "discrepancy_id": f"DSC-{current.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}",
            "fact_key": result["fact_key"],
            "type": kind,
            "resolution": resolution,
            "winning": {"level": winner.level, "source": winner.source, "value": winner.value},
            "losing": {"level": loser.level, "source": loser.source, "value": loser.value},
            "needs_review": needs_review,
            "detected_at": current.isoformat(),
        }
        result["discrepancies"].append(record)
        self.discrepancies.append(record)

    def _escalate(self, result: dict[str, Any], reason: str, human_decision: str) -> None:
        rule_name = f"escalate_on_{reason}"
        if not self.escalation_rules.get(rule_name, True):
            return
        esc = result["escalation"]
        esc["required"] = True
        if reason not in esc["reasons"]:
            esc["reasons"].append(reason)
        esc["required_human_decision"] = human_decision if not esc["required_human_decision"] else f"{esc['required_human_decision']} | {human_decision}"

    def _finish(self, result: dict[str, Any], started: float, task_id: str | None, agent: str | None) -> dict[str, Any]:
        if self.conflict_policy.get("log_discrepancy", True):
            for record in result["discrepancies"]:
                self._log_event(
                    action="truth_discrepancy",
                    outcome="success",
                    risk="medium" if record["needs_review"] else "low",
                    resource=record["fact_key"],
                    inputs={"type": record["type"], "resolution": record["resolution"], "losing": record["losing"]},
                    outputs={"discrepancy_id": record["discrepancy_id"], "winning": record["winning"]},
                    task_id=task_id, agent=agent, started=started,
                )
        if result["status"] in ("blocked_stale_live_data", "blocked_stale_catalog", "unresolved_conflict", "no_evidence"):
            self._log_event(
                action="truth_resolve", outcome="blocked", risk="medium", resource=result["fact_key"],
                inputs={"fact_key": result["fact_key"]},
                outputs={"status": result["status"], "escalation": result["escalation"]["reasons"], "blocked": result["blocked"]},
                task_id=task_id, agent=agent, started=started,
            )
        return result

    def _log_event(
        self, *, action: str, outcome: str, risk: str, resource: str, inputs: dict[str, Any], outputs: dict[str, Any],
        task_id: str | None, agent: str | None, started: float,
    ) -> None:
        if self.audit is None:
            return
        self.audit.log_audit(
            environment=self.environment,
            task_id=task_id,
            agent=agent or "truth_resolver",
            action=action,
            tool="truth_resolver",
            domain="source_of_truth",
            resource=resource,
            inputs=inputs,
            outputs=outputs,
            risk_level=risk,
            autonomy_level=0,
            outcome=outcome,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
