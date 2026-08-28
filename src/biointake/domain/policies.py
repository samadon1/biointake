"""Versioned protocol policy. The canonical form is structured data; prose renderings derive from it."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from .enums import ActorRole, CheckCategory, HumanOption


class TemperatureRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    min_c: float
    max_c: float
    tolerance_minutes: float  # cumulative minutes out of range that are still acceptable
    max_gap_minutes: float  # a logging gap longer than this makes the log unusable
    exception_allowed: bool
    exception_roles: tuple[ActorRole, ...]
    clause: str


class ConsentRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    min_version: int
    required_scope: str
    clause: str


class CustodyRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    required_events: tuple[str, ...]  # ordered
    clause: str


class ProtocolPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    policy_id: str
    version: str
    protocol_id: str
    title: str
    allowed_specimen_types: tuple[str, ...]
    required_checks: tuple[CheckCategory, ...]
    temperature: TemperatureRule
    consent: ConsentRule
    custody: CustodyRule
    quarantine_roles: tuple[ActorRole, ...]
    reject_roles: tuple[ActorRole, ...]

    def roles_for(self, option: HumanOption) -> tuple[ActorRole, ...]:
        if option is HumanOption.APPROVE_EXCEPTION:
            return self.temperature.exception_roles if self.temperature.exception_allowed else ()
        if option is HumanOption.QUARANTINE:
            return self.quarantine_roles
        return self.reject_roles

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_path(cls, path: Path) -> ProtocolPolicy:
        return cls.model_validate_json(path.read_text())

    def render_markdown(self) -> str:
        t, c, k = self.temperature, self.consent, self.custody
        lines = [
            f"# {self.title}",
            "",
            f"Policy `{self.policy_id}` version `{self.version}` for protocol `{self.protocol_id}`.",
            "",
            f"- Allowed specimen types: {', '.join(self.allowed_specimen_types)}",
            f"- Required checks: {', '.join(x.value for x in self.required_checks)}",
            f"- Transport temperature: {t.min_c}–{t.max_c} °C, cumulative tolerance {t.tolerance_minutes} min; {t.clause}",
            f"- Consent: version ≥ {c.min_version}, scope `{c.required_scope}`; {c.clause}",
            f"- Custody events (ordered): {' → '.join(k.required_events)}; {k.clause}",
            f"- Quarantine may be directed by: {', '.join(r.value for r in self.quarantine_roles)}",
            f"- Temperature exception may be approved by: {', '.join(r.value for r in t.exception_roles) or 'nobody'}",
            "",
            "All data in this demonstration is synthetic.",
        ]
        return "\n".join(lines) + "\n"


def default_policy() -> ProtocolPolicy:
    """PROTO-042 v3, the single protocol supported by the MVP."""
    return ProtocolPolicy(
        policy_id="POLICY-PROTO-042",
        version="3.0.0",
        protocol_id="PROTO-042",
        title="PROTO-042 Research Plasma Intake Policy",
        allowed_specimen_types=("PLASMA",),
        required_checks=tuple(CheckCategory),
        temperature=TemperatureRule(
            min_c=2.0,
            max_c=8.0,
            tolerance_minutes=10.0,
            max_gap_minutes=30.0,
            exception_allowed=True,
            exception_roles=(ActorRole.PRINCIPAL_INVESTIGATOR,),
            clause="§7.3, a cumulative excursion above tolerance requires a documented principal-investigator disposition.",
        ),
        consent=ConsentRule(
            min_version=3,
            required_scope="RESEARCH_PLASMA",
            clause="§4.1, consent addendum v3 or later is required for plasma research use.",
        ),
        custody=CustodyRule(
            required_events=("COLLECTED", "PACKED", "SHIPPED", "RECEIVED"),
            clause="§5.2, every handoff must be recorded with actor and timestamp, in order.",
        ),
        quarantine_roles=(
            ActorRole.COORDINATOR,
            ActorRole.PRINCIPAL_INVESTIGATOR,
            ActorRole.QA_REVIEWER,
        ),
        reject_roles=(ActorRole.PRINCIPAL_INVESTIGATOR,),
    )
