"""Manifest + scanner-export parsing and identity reconciliation.

Labels arrive as a scanner export (what a handheld barcode scanner produces), not as images.
Exact identifier equality is the only thing that PASSES on its own. A near-match (e.g. `BX-2O7` vs
`BX-207`) is AMBIGUOUS until an admitted sender attestation confirms the correction.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from ..domain.enums import ReasonCode

REQUIRED_MANIFEST_COLUMNS = (
    "sample_id",
    "participant_reference",
    "specimen_type",
    "container_id",
    "collection_timestamp",
)
"""What a manifest must actually say. A row number and free-text notes are useful but not required: a site
coordinator exporting from a spreadsheet has the specimens, not our column names."""

OPTIONAL_MANIFEST_COLUMNS = ("row", "notes")

_CONFUSABLES = str.maketrans({"O": "0", "I": "1", "L": "1", "S": "5", "B": "8"})


class ManifestRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    row: int
    sample_id: str
    participant_reference: str
    specimen_type: str
    container_id: str
    collection_timestamp: datetime
    notes: str = ""


class ParsedManifest(BaseModel):
    """The result of reading a site's CSV.

    `ignored_columns` exists so that extra columns are *reported* rather than either rejecting the file or
    silently vanishing. A site that ships a `volume_ml` column deserves to be told we did not read it."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    rows: tuple[ManifestRow, ...] = ()
    problems: tuple[str, ...] = ()
    ignored_columns: tuple[str, ...] = ()


class ManifestParseError(ValueError):
    pass


def _normalise_header(name: str) -> str:
    """Case, surrounding space and hyphen-vs-underscore are formatting, not meaning.

    Deliberately mechanical: no guessing at synonyms. `Sample ID` is the same column as `sample_id`;
    `specimen` is not, and pretending otherwise would silently mis-map a site's data."""
    return name.strip().lower().replace("-", "_").replace(" ", "_")


def parse_manifest(data: bytes) -> ParsedManifest:
    """Read a site's manifest. Malformed rows are reported, never silently dropped."""
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    header = [h for h in (reader.fieldnames or ()) if h is not None]
    canonical = {_normalise_header(h): h for h in header}
    missing = [c for c in REQUIRED_MANIFEST_COLUMNS if c not in canonical]
    if missing:
        raise ManifestParseError(
            f"manifest is missing required column(s): {', '.join(missing)}"
            + (f" (found: {', '.join(header)})" if header else " (the file has no header row)")
        )
    known = set(REQUIRED_MANIFEST_COLUMNS) | set(OPTIONAL_MANIFEST_COLUMNS)
    ignored = tuple(canonical[k] for k in canonical if k not in known)

    rows: list[ManifestRow] = []
    problems: list[str] = []
    for offset, raw in enumerate(reader):
        line_no = offset + 2  # 1-based, past the header
        values = {k: (raw.get(src) or "").strip() for k, src in canonical.items() if k in known}
        # A site that numbers its own rows keeps its numbering; otherwise position is the row.
        if not values.get("row"):
            values["row"] = str(offset + 1)
        values.setdefault("notes", "")
        if not any(v for k, v in values.items() if k != "row"):
            continue  # a trailing blank line is not an error
        try:
            rows.append(ManifestRow.model_validate(values))
        except ValidationError as e:
            err = e.errors()[0]
            column = str(err["loc"][0]) if err["loc"] else "row"
            # Pydantic's own wording is written for the developer who wrote the model. A site coordinator
            # reading "input should be a valid datetime or date, input is too short" about a cell they left
            # blank learns nothing; an empty required cell is missing, whatever the type system calls it.
            if err["type"] == "missing" or not values.get(column):
                detail = "is required but empty"
            else:
                detail = f"is not readable: {err['msg'].lower()}"
            problems.append(f"line {line_no}: {column} {detail}")
    return ParsedManifest(rows=tuple(rows), problems=tuple(problems), ignored_columns=ignored)


class ScannedLabel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    barcode: str
    sample_id: str
    container_id: str
    scanned_at: datetime
    readable: bool = True


class ScannerExport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    scanner_id: str
    exported_at: datetime
    scans: tuple[ScannedLabel, ...]


def parse_scanner_export(data: bytes) -> ScannerExport:
    return ScannerExport.model_validate(json.loads(data))


def normalize_identifier(value: str) -> str:
    return value.strip().upper().translate(_CONFUSABLES)


def is_near_match(a: str, b: str) -> bool:
    """True when identifiers differ ONLY by confusable glyphs (O/0, I/1, L/1, S/5, B/8).

    Deliberately NOT edit distance: with sequential identifiers (BX-207, BX-208) a one-character
    edit is always ambiguous, and ambiguity must never be resolved by guessing.
    """
    if a == b:
        return False
    return normalize_identifier(a) == normalize_identifier(b)


class LinkResult(BaseModel):
    """How one scanned label relates to the manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    sample_id: str
    barcode: str
    manifest_row: int | None
    exact: bool
    near_match_value: str | None = None
    duplicate: bool = False
    reason_codes: tuple[ReasonCode, ...] = ()


def link_labels_to_manifest(rows: list[ManifestRow], export: ScannerExport) -> list[LinkResult]:
    by_exact = {r.sample_id: r for r in rows}
    barcode_counts: dict[str, int] = {}
    for s in export.scans:
        barcode_counts[s.barcode] = barcode_counts.get(s.barcode, 0) + 1
    # Exact matches claim their rows first so a near-match can never steal an exactly-matched row.
    claimed_rows: set[int] = {by_exact[s.sample_id].row for s in export.scans if s.sample_id in by_exact}
    results: list[LinkResult] = []
    for scan in export.scans:
        codes: list[ReasonCode] = []
        dup = barcode_counts[scan.barcode] > 1
        if dup:
            codes.append(ReasonCode.LABEL_DUPLICATE)
        if not scan.readable:
            codes.append(ReasonCode.LABEL_UNREADABLE)
        row = by_exact.get(scan.sample_id)
        if row is not None:
            results.append(
                LinkResult(
                    sample_id=scan.sample_id,
                    barcode=scan.barcode,
                    manifest_row=row.row,
                    exact=True,
                    duplicate=dup,
                    reason_codes=tuple(codes),
                )
            )
            continue
        near = [r for r in rows if r.row not in claimed_rows and is_near_match(r.sample_id, scan.sample_id)]
        if len(near) == 1:
            claimed_rows.add(near[0].row)
            codes.append(ReasonCode.MANIFEST_IDENTIFIER_NEAR_MATCH)
            results.append(
                LinkResult(
                    sample_id=scan.sample_id,
                    barcode=scan.barcode,
                    manifest_row=near[0].row,
                    exact=False,
                    near_match_value=near[0].sample_id,
                    duplicate=dup,
                    reason_codes=tuple(codes),
                )
            )
            continue
        codes.append(ReasonCode.MANIFEST_ROW_MISSING)
        results.append(
            LinkResult(
                sample_id=scan.sample_id,
                barcode=scan.barcode,
                manifest_row=None,
                exact=False,
                duplicate=dup,
                reason_codes=tuple(codes),
            )
        )
    return results


def manifest_row_lookup(rows: list[ManifestRow]) -> dict[int, ManifestRow]:
    return {r.row: r for r in rows}


def manifest_to_dicts(rows: list[ManifestRow]) -> list[dict[str, Any]]:
    return [r.model_dump(mode="json") for r in rows]
