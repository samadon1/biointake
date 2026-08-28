from __future__ import annotations

from datetime import UTC, datetime

import pytest

from biointake.domain.enums import ReasonCode
from biointake.services.manifest import (
    ManifestParseError,
    ScannedLabel,
    ScannerExport,
    is_near_match,
    link_labels_to_manifest,
    parse_manifest,
)

HEADER = "row,sample_id,participant_reference,specimen_type,container_id,collection_timestamp,notes\n"
T = datetime(2026, 8, 24, tzinfo=UTC)


def scans(*ids: str, dup: str | None = None) -> ScannerExport:
    labels = [ScannedLabel(barcode=f"BC-{i}", sample_id=i, container_id="BOX-1", scanned_at=T) for i in ids]
    if dup:
        labels.append(ScannedLabel(barcode=f"BC-{dup}", sample_id=dup, container_id="BOX-1", scanned_at=T))
    return ScannerExport(scanner_id="s", exported_at=T, scans=tuple(labels))


def test_missing_columns_raise():
    with pytest.raises(ManifestParseError) as e:
        parse_manifest(b"row,sample_id\n1,BX-1\n")
    # The site has to be told which columns, not merely that the file was wrong.
    assert "participant_reference" in str(e.value)


def test_a_sites_own_spreadsheet_shape_is_accepted():
    """`row` and `notes` are ours, not the site's; header case and spacing are formatting, not meaning.

    A coordinator exporting from a spreadsheet has the specimens, not our column names."""
    parsed = parse_manifest(
        b"Sample ID,Participant Reference,Specimen Type,Container-ID,collection_timestamp,Volume mL\n"
        b"BX-1,P1,PLASMA,BOX-1,2026-08-24T09:00:00+00:00,2.5\n"
        b"BX-2,P2,PLASMA,BOX-1,2026-08-24T09:05:00+00:00,2.5\n"
        b"\n"
    )
    assert [r.sample_id for r in parsed.rows] == ["BX-1", "BX-2"]
    assert [r.row for r in parsed.rows] == [1, 2]  # position becomes the row number
    assert not parsed.problems  # the trailing blank line is not an error
    assert parsed.ignored_columns == ("Volume mL",)  # reported, never silently dropped


def test_a_sites_own_row_numbering_is_kept():
    parsed = parse_manifest((HEADER + "7,BX-1,P1,PLASMA,BOX-1,2026-08-24T09:00:00+00:00,\n").encode())
    assert parsed.rows[0].row == 7


def test_a_missing_required_value_names_the_column():
    parsed = parse_manifest(
        b"sample_id,participant_reference,specimen_type,container_id,collection_timestamp\n"
        b"BX-1,P1,PLASMA,BOX-1,\n"
    )
    assert not parsed.rows
    assert "collection_timestamp" in parsed.problems[0] and "line 2" in parsed.problems[0]


def test_malformed_rows_are_reported_not_dropped():
    parsed = parse_manifest(
        (
            HEADER
            + "1,BX-1,P1,PLASMA,BOX-1,2026-08-24T09:00:00+00:00,\n"
            + "x,BX-2,P2,PLASMA,BOX-1,not-a-date,\n"
        ).encode()
    )
    assert [r.sample_id for r in parsed.rows] == ["BX-1"]
    assert len(parsed.problems) == 1 and "line 3" in parsed.problems[0]


def test_near_match_is_confusable_glyphs_only():
    assert is_near_match("BX-2O7", "BX-207")
    assert is_near_match("bx-2o7", "BX-207")
    assert not is_near_match("BX-208", "BX-207")  # edit distance 1 is NOT a near match
    assert not is_near_match("BX-207", "BX-207")


def test_exact_matches_claim_rows_before_near_matches():
    rows = parse_manifest(
        (
            HEADER
            + "1,BX-2O7,P7,PLASMA,BOX-1,2026-08-24T09:00:00+00:00,\n2,BX-208,P8,PLASMA,BOX-1,2026-08-24T09:00:00+00:00,\n"
        ).encode()
    ).rows
    links = {lk.sample_id: lk for lk in link_labels_to_manifest(rows, scans("BX-207", "BX-208"))}
    assert links["BX-208"].exact and links["BX-208"].manifest_row == 2
    assert not links["BX-207"].exact and links["BX-207"].manifest_row == 1
    assert links["BX-207"].near_match_value == "BX-2O7"
    assert ReasonCode.MANIFEST_IDENTIFIER_NEAR_MATCH in links["BX-207"].reason_codes


def test_unmatched_label_has_no_row():
    rows = parse_manifest((HEADER + "1,BX-201,P1,PLASMA,BOX-1,2026-08-24T09:00:00+00:00,\n").encode()).rows
    [lk] = link_labels_to_manifest(rows, scans("BX-999"))
    assert lk.manifest_row is None and ReasonCode.MANIFEST_ROW_MISSING in lk.reason_codes


def test_duplicate_labels_are_flagged():
    rows = parse_manifest((HEADER + "1,BX-201,P1,PLASMA,BOX-1,2026-08-24T09:00:00+00:00,\n").encode()).rows
    links = link_labels_to_manifest(rows, scans("BX-201", dup="BX-201"))
    assert all(lk.duplicate for lk in links)
