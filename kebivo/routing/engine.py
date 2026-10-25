"""Deterministic, side-effect-free routing evaluation."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class DestinationInput:
    id: str
    enabled: bool = True
    delivery_mode: Literal["new_groups_only", "every_occurrence"] = "new_groups_only"


@dataclass(frozen=True)
class RuleInput:
    id: str
    name: str
    priority: int
    created_at: datetime
    enabled: bool
    event_types: tuple[str, ...]
    match_mode: Literal["any", "all"]
    tags: tuple[str, ...]
    destination_ids: tuple[str, ...]
    match_behavior: Literal["continue", "stop"]


@dataclass(frozen=True)
class EvaluatedRule:
    rule_id: str
    status: Literal["matched", "not_matched", "skipped_disabled", "not_evaluated_stopped"]
    reason: str


@dataclass(frozen=True)
class RoutingMatchInput:
    destination_id: str
    via: Literal["rule", "fallback"]
    outcome: Literal[
        "delivery_created",
        "duplicate_destination",
        "destination_disabled",
        "fallback_skipped_disabled",
        "suppressed_duplicate",
    ]
    rule_id: str | None = None


@dataclass(frozen=True)
class Evaluation:
    evaluated_rules: tuple[EvaluatedRule, ...]
    stopped_by_rule_id: str | None
    destinations: tuple[RoutingMatchInput, ...]
    fallback_applied: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)


def evaluate(
    event_type: str,
    tags: set[str] | frozenset[str],
    rules: list[RuleInput] | tuple[RuleInput, ...],
    destinations: list[DestinationInput] | tuple[DestinationInput, ...],
    fallback_destination_id: str | None = None,
    *,
    initial: bool = True,
    triggered_reopen: bool = False,
    previously_delivered: frozenset[str] = frozenset(),
) -> Evaluation:
    """Route an event using only supplied immutable facts."""
    by_id = {destination.id: destination for destination in destinations}
    present = set(tags)
    evaluated: list[EvaluatedRule] = []
    matches: list[RoutingMatchInput] = []
    warnings: list[str] = []
    seen: set[str] = set()
    matched_enabled = False
    stopped: str | None = None
    for rule in sorted(rules, key=lambda item: (item.priority, item.created_at, item.id)):
        if stopped is not None:
            evaluated.append(
                EvaluatedRule(rule.id, "not_evaluated_stopped", f"Stopped by rule {stopped}")
            )
            continue
        if not rule.enabled:
            evaluated.append(EvaluatedRule(rule.id, "skipped_disabled", "Rule is disabled"))
            continue
        if event_type not in rule.event_types:
            evaluated.append(EvaluatedRule(rule.id, "not_matched", "Event type does not match"))
            continue
        tag_match = not rule.tags or (
            any(tag in present for tag in rule.tags)
            if rule.match_mode == "any"
            else all(tag in present for tag in rule.tags)
        )
        if not tag_match:
            evaluated.append(EvaluatedRule(rule.id, "not_matched", "Tags do not match"))
            continue
        matched_enabled = True
        evaluated.append(EvaluatedRule(rule.id, "matched", "Event type and tags match"))
        enabled_count = 0
        for destination_id in rule.destination_ids:
            destination = by_id.get(destination_id)
            if destination is None or not destination.enabled:
                outcome: Literal[
                    "delivery_created",
                    "duplicate_destination",
                    "destination_disabled",
                    "fallback_skipped_disabled",
                    "suppressed_duplicate",
                ] = "destination_disabled"
            elif destination_id in seen:
                outcome = "duplicate_destination"
            elif destination.delivery_mode == "new_groups_only" and not (
                initial or triggered_reopen or destination_id not in previously_delivered
            ):
                outcome = "suppressed_duplicate"
                seen.add(destination_id)
            else:
                outcome = "delivery_created"
                enabled_count += 1
                seen.add(destination_id)
            matches.append(RoutingMatchInput(destination_id, "rule", outcome, rule.id))
        if enabled_count == 0 and all(
            item.outcome == "destination_disabled" for item in matches if item.rule_id == rule.id
        ):
            warnings.append(
                f"Rule {rule.id} matched but all its destinations are disabled; "
                "fallback not applied"
            )
        if rule.match_behavior == "stop":
            stopped = rule.id
    fallback_applied = not matched_enabled and fallback_destination_id is not None
    if fallback_applied and fallback_destination_id is not None:
        destination = by_id.get(fallback_destination_id)
        fallback_outcome: Literal[
            "delivery_created", "fallback_skipped_disabled", "suppressed_duplicate"
        ] = (
            "delivery_created"
            if destination and destination.enabled
            else "fallback_skipped_disabled"
        )
        if (
            fallback_outcome == "delivery_created"
            and destination is not None
            and destination.delivery_mode == "new_groups_only"
            and not (
                initial or triggered_reopen or fallback_destination_id not in previously_delivered
            )
        ):
            fallback_outcome = "suppressed_duplicate"
        matches.append(RoutingMatchInput(fallback_destination_id, "fallback", fallback_outcome))
    return Evaluation(tuple(evaluated), stopped, tuple(matches), fallback_applied, tuple(warnings))
