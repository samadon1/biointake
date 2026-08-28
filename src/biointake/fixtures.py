"""Loads a fixture directory into a ShipmentPackage (what the intake API will receive)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from .domain.models import LimsRecord, SiteContact
from .domain.policies import ProtocolPolicy
from .services.lims_demo import parse_lims_records


class ContainerInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    container_id: str
    logger_id: str


class ShipmentInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    shipment_id: str
    protocol_id: str
    protocol_version: str
    sender_site_id: str
    sender_site_name: str
    receiving_facility: str
    received_at: datetime
    expected_sample_count: int
    containers: tuple[ContainerInfo, ...]
    data_classification: str = "SYNTHETIC"
    notes: str = ""


class ShipmentPackage(BaseModel):
    """Everything present when the shipment arrives. `later` holds evidence that arrives afterwards."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)
    shipment: ShipmentInfo
    policy: ProtocolPolicy
    manifest_csv: bytes
    scanner_export_json: bytes
    temperature_logs: dict[str, bytes]  # logger_id → csv bytes
    consent_records_json: bytes
    custody_log_json: bytes
    protocol_json: bytes
    contacts: tuple[SiteContact, ...]
    lims_records: tuple[LimsRecord, ...]
    later: dict[str, bytes] = Field(default_factory=dict)  # filename → bytes


def load_package(root: Path) -> ShipmentPackage:
    shipment = ShipmentInfo.model_validate_json((root / "shipment.json").read_text())
    policy = ProtocolPolicy.from_path(root / "protocol" / f"{shipment.protocol_id}.json")
    logs = {p.stem: p.read_bytes() for p in sorted((root / "temperature").glob("*.csv"))}
    contacts = tuple(
        SiteContact.model_validate(c)
        for c in json.loads((root / "contacts" / "site-contacts.json").read_text())
    )
    later_dir = root / "consent" / "received-later"
    later = {p.name: p.read_bytes() for p in sorted(later_dir.glob("*"))} if later_dir.exists() else {}
    return ShipmentPackage(
        shipment=shipment,
        policy=policy,
        manifest_csv=(root / "manifest.csv").read_bytes(),
        scanner_export_json=(root / "labels" / "scanner-export.json").read_bytes(),
        temperature_logs=logs,
        consent_records_json=(root / "consent" / "initial" / "consent-records.json").read_bytes(),
        custody_log_json=(root / "custody" / "chain-of-custody.json").read_bytes(),
        protocol_json=(root / "protocol" / f"{shipment.protocol_id}.json").read_bytes(),
        contacts=contacts,
        lims_records=tuple(parse_lims_records((root / "lims" / "initial-records.json").read_bytes())),
        later=later,
    )


DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "shipment_001"
