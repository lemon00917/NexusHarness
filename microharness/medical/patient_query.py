"""Patient identity predicates for medical-record database queries."""

from __future__ import annotations

from dataclasses import dataclass


class MissingPatientIdentityError(ValueError):
    """Raised when a medical-record query has no patient identity at all."""


@dataclass(frozen=True)
class PatientWhereClause:
    """Strict and compatibility predicates that always retain patient scope."""

    strict_where: str
    fallback_where: str | None
    strict_fields: tuple[str, ...]
    fallback_fields: tuple[str, ...]


def _sql_string(value: object) -> str:
    """Return a safely quoted SQL string for clients without bind support."""
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    return "'" + text.replace("'", "''") + "'"


def _identity_predicate(field: str, value: object) -> str:
    return f"{field} = {_sql_string(value)}"


def build_patient_where_clause(
    *,
    register_no: str = "",
    visit_no: str = "",
    global_patient_id: str = "",
    global_visit_id: str = "",
) -> PatientWhereClause:
    """Build patient-scoped predicates without ever falling back to ``1=1``.

    Registration and visit identifiers remain the compatibility fallback when
    both local and global identifiers are supplied. If only global identifiers
    are available, they remain mandatory and no broader fallback is generated.
    """
    local_parts: list[tuple[str, str]] = []
    global_parts: list[tuple[str, str]] = []

    if str(register_no or "").strip():
        local_parts.append(("registerno", _identity_predicate("registerno", register_no)))
    if str(visit_no or "").strip():
        local_parts.append(("visitnumber", _identity_predicate("visitnumber", visit_no)))
    if str(global_patient_id or "").strip():
        global_parts.append(
            ("papat_relpatientid", _identity_predicate("papat_relpatientid", global_patient_id))
        )
    if str(global_visit_id or "").strip():
        global_parts.append(
            ("paadm_relvisitnumber", _identity_predicate("paadm_relvisitnumber", global_visit_id))
        )

    all_parts = local_parts + global_parts
    if not all_parts:
        raise MissingPatientIdentityError("medical-record query requires patient or visit identity")

    strict_where = " AND ".join(predicate for _, predicate in all_parts)
    fallback_where = None
    fallback_fields: tuple[str, ...] = ()
    if local_parts and global_parts:
        fallback_where = " AND ".join(predicate for _, predicate in local_parts)
        fallback_fields = tuple(field for field, _ in local_parts)

    return PatientWhereClause(
        strict_where=strict_where,
        fallback_where=fallback_where,
        strict_fields=tuple(field for field, _ in all_parts),
        fallback_fields=fallback_fields,
    )
