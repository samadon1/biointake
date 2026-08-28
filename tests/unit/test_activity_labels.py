"""Every audit event the server can write has a readable name in the console.

An unlabelled one falls back to the raw enum, which is both unfriendly and too long for the column
it sits in. STAGING_BATCH_COMMITTED overflowed into the summary beside it, and eight others were one
timeline away from doing the same.
"""

from __future__ import annotations

import re
from pathlib import Path

from biointake.domain.enums import AuditEventType

WORKSPACE = Path(__file__).resolve().parents[2] / "web" / "src" / "components" / "case-workspace.tsx"


def labelled() -> set[str]:
    source = WORKSPACE.read_text()
    block = source[source.index("const DOMAIN_LABELS") : source.index("/** Phases group")]
    return set(re.findall(r"^\s{2}([A-Z_]+):", block, re.M))


def test_every_audit_event_has_a_readable_label():
    missing = sorted(e.value for e in AuditEventType if e.value not in labelled())
    assert missing == [], f"these would render as raw enum names: {missing}"


def test_no_label_is_wider_than_its_column():
    """The column is w-40, ten rem. In the console's monospace that is about twenty-six characters."""
    source = WORKSPACE.read_text()
    block = source[source.index("const DOMAIN_LABELS") : source.index("/** Phases group")]
    too_long = [v for v in re.findall(r':\s*"([^"]+)"', block) if len(v) > 26]
    assert too_long == [], f"these would be truncated: {too_long}"
