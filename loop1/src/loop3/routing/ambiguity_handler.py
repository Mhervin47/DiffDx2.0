from __future__ import annotations

import logging
from collections import defaultdict

from loop3.routing.map_loader import lookup_disease
from loop3.routing.routing_schema import AppointmentType, RoutingOption, UrgencyLevel
from loop3.routing.urgency_classifier import classify_urgency

_log = logging.getLogger(__name__)

_AMBIGUITY_RATIO = 1.5
_FALLBACK_SPECIALTY = "Internal Medicine"
_FALLBACK_URGENCY = UrgencyLevel.ROUTINE
_FALLBACK_APPOINTMENT = AppointmentType.ROUTINE
_FALLBACK_REASONING = "Your symptoms don't point clearly to one specialty, so we're starting with a general internal medicine evaluation."


_URGENCY_RANK = {"emergency": 0, "urgent": 1, "routine": 2}


def _aggregate_by_specialty(
    differential: list[tuple[str, float]],
    specialty_map: dict,
    final_confidence: float,
) -> list[tuple[str, float, dict]]:
    """
    Aggregate differential probability weights by specialty.
    Returns list of (specialty, total_weight, best_map_entry) sorted by weight descending.
    best_map_entry is the entry with the HIGHEST urgency among diseases in that specialty
    (ties broken by probability), so urgency classification is accurate.
    """
    weights: dict[str, float] = defaultdict(float)
    best_entry: dict[str, dict] = {}
    best_prob: dict[str, float] = {}

    for disease, prob in differential:
        entry = lookup_disease(disease, specialty_map)
        if entry is None:
            _log.warning("Disease %r not in map — assigning to fallback specialty.", disease)
            specialty = _FALLBACK_SPECIALTY
            entry = {
                "specialty": _FALLBACK_SPECIALTY,
                "urgency": "routine",
                "appointment_type": "routine_appointment",
                "reasoning": _FALLBACK_REASONING,
            }
        else:
            specialty = entry["specialty"]

        weights[specialty] += prob

        # Keep the highest-urgency entry per specialty (ties broken by probability)
        existing = best_entry.get(specialty)
        if existing is None:
            best_entry[specialty] = entry
            best_prob[specialty] = prob
        else:
            existing_rank = _URGENCY_RANK.get(existing["urgency"], 2)
            new_rank = _URGENCY_RANK.get(entry["urgency"], 2)
            if new_rank < existing_rank or (new_rank == existing_rank and prob > best_prob[specialty]):
                best_entry[specialty] = entry
                best_prob[specialty] = prob

    return sorted(
        [(sp, w, best_entry[sp]) for sp, w in weights.items()],
        key=lambda x: -x[1],
    )


def is_ambiguous(
    differential: list[tuple[str, float]],
    specialty_map: dict,
) -> bool:
    """
    Returns True if:
    - the top specialty's probability-weighted score is less than 1.5x the second, OR
    - the primary specialty is emergency and at least one other specialty is also emergency
      (the emergency-surfacing rule would add alternatives).
    """
    aggregated = _aggregate_by_specialty(differential, specialty_map, final_confidence=0.5)
    if len(aggregated) < 2:
        return False

    top_weight = aggregated[0][1]
    second_weight = aggregated[1][1]
    if top_weight < _AMBIGUITY_RATIO * second_weight:
        return True

    # Emergency-surfacing rule: multiple emergency specialties → ambiguous
    primary_urgency = aggregated[0][2].get("urgency", "routine")
    primary_specialty = aggregated[0][2].get("specialty", "")
    if primary_urgency == "emergency":
        for _, _, entry in aggregated[1:]:
            if (entry.get("urgency") == "emergency"
                    and entry.get("specialty") != primary_specialty):
                return True

    return False


def get_routing_options(
    differential: list[tuple[str, float]],
    specialty_map: dict,
    final_confidence: float,
    max_options: int = 3,
) -> list[RoutingOption]:
    """
    Aggregate differential probabilities by specialty and return ranked routing options.
    Returns at least 1 option. Returns 2-3 if ambiguous.
    """
    if not differential:
        return [_fallback_option()]

    aggregated = _aggregate_by_specialty(differential, specialty_map, final_confidence)

    ambiguous = (
        len(aggregated) >= 2
        and aggregated[0][1] < _AMBIGUITY_RATIO * aggregated[1][1]
    )
    n_options = min(max_options, len(aggregated)) if ambiguous else 1

    options: list[RoutingOption] = []
    for specialty, weight, entry in aggregated[:n_options]:
        raw_urgency = UrgencyLevel(entry["urgency"])
        if raw_urgency == UrgencyLevel.ROUTINE and final_confidence < 0.4:
            urgency = UrgencyLevel.URGENT
        else:
            urgency = raw_urgency
        appointment_type = _appointment_for_urgency(urgency, entry)
        options.append(RoutingOption(
            specialty=specialty,
            urgency=urgency,
            reasoning=entry["reasoning"],
            appointment_type=appointment_type,
            # LLM-generated differential probabilities aren't guaranteed to sum
            # to exactly 1.0 (observed as high as 1.05) — several diseases
            # summing into one specialty can then push the aggregated weight
            # past 1.0, which RoutingOption's schema (le=1.0) rejects outright.
            # Clamp at the boundary rather than letting real sessions 500.
            confidence_weight=min(round(weight, 4), 1.0),
        ))

    # Emergency-surfacing: always include additional emergency specialties alongside primary
    if options and options[0].urgency == UrgencyLevel.EMERGENCY:
        existing_specialties = {o.specialty for o in options}
        for specialty, weight, entry in aggregated[n_options:]:
            raw_urgency = UrgencyLevel(entry["urgency"])
            if raw_urgency == UrgencyLevel.ROUTINE and final_confidence < 0.4:
                urgency = UrgencyLevel.URGENT
            else:
                urgency = raw_urgency
            if urgency == UrgencyLevel.EMERGENCY and specialty not in existing_specialties:
                appointment_type = _appointment_for_urgency(urgency, entry)
                options.append(RoutingOption(
                    specialty=specialty,
                    urgency=urgency,
                    reasoning=entry["reasoning"],
                    appointment_type=appointment_type,
                    confidence_weight=min(round(weight, 4), 1.0),
                ))
                existing_specialties.add(specialty)

    return options if options else [_fallback_option()]


def _appointment_for_urgency(urgency: UrgencyLevel, entry: dict) -> AppointmentType:
    """
    Use the map's appointment_type when urgency matches; otherwise derive from urgency.
    Urgency bumps (e.g. routine → urgent) also bump the appointment type.
    """
    map_urgency = UrgencyLevel(entry.get("urgency", "routine"))
    if urgency == map_urgency:
        return AppointmentType(entry["appointment_type"])
    # Urgency was bumped up — derive appointment type from new urgency
    return {
        UrgencyLevel.EMERGENCY: AppointmentType.EMERGENCY,
        UrgencyLevel.URGENT: AppointmentType.URGENT,
        UrgencyLevel.ROUTINE: AppointmentType.ROUTINE,
    }[urgency]


def _fallback_option() -> RoutingOption:
    return RoutingOption(
        specialty=_FALLBACK_SPECIALTY,
        urgency=_FALLBACK_URGENCY,
        reasoning=_FALLBACK_REASONING,
        appointment_type=_FALLBACK_APPOINTMENT,
        confidence_weight=0.0,
    )
