"""Print the tube labels for a shipment: Code 128 of the site accession, as a lab's printer makes them.

    uv run python scripts/generate_labels.py

Writes one SVG sheet into the example shipment. It is a build-time script, not part of the product,
so `python-barcode` stays out of the runtime dependencies: run this when the fixture changes and
commit what it produces.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import barcode
from barcode.writer import SVGWriter

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "example-shipment"
# Sized so the symbol survives being photographed or read off a screen: at the first attempt
# the modules were about a pixel wide and nothing decoded at all.
# Four across: the sheet ends up wider than it is tall, which is the shape of a screen someone
# points a camera at, and of a page someone prints.
COLUMNS = 4
CELL_W, CELL_H = 460, 210


def label(accession: str, sample_id: str) -> str:
    """One Code 128 symbol plus the two identifiers a person reads off the tube."""
    buf = io.BytesIO()
    barcode.get("code128", accession, writer=SVGWriter()).write(
        buf,
        options={
            "module_width": 0.45,
            "module_height": 18.0,
            "font_size": 0,
            "quiet_zone": 5.0,
            "write_text": False,
        },
    )
    symbol = buf.getvalue().decode()
    inner = symbol[symbol.index("<g") : symbol.rindex("</svg>")]
    return (
        f"<g>{inner}</g>"
        f'<text x="10" y="150" font-family="monospace" font-size="17" fill="#111">{sample_id}</text>'
        f'<text x="10" y="174" font-family="monospace" font-size="14" fill="#555">{accession}</text>'
    )


def main() -> int:
    rows = list(csv.DictReader((KIT / "6-accessions.csv").read_text().splitlines()))
    # A symbol needs clear space around it or a reader will not see where it ends. The last row was
    # ten pixels from the edge and those two labels would not decode at all.
    height = CELL_H * ((len(rows) + COLUMNS - 1) // COLUMNS) + 140
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CELL_W * COLUMNS}" height="{height}" '
        f'viewBox="0 0 {CELL_W * COLUMNS} {height}">',
        f'<rect width="{CELL_W * COLUMNS}" height="{height}" fill="#ffffff"/>',
        '<text x="16" y="30" font-family="sans-serif" font-size="15" fill="#111">'
        "SHIP-2026-0043, tube labels (synthetic)</text>",
    ]
    for i, row in enumerate(rows):
        x, y = (i % COLUMNS) * CELL_W + 16, (i // COLUMNS) * CELL_H + 50
        out.append(f'<g transform="translate({x} {y})">{label(row["accession"], row["sample_id"])}</g>')
    out.append("</svg>")
    (KIT / "7-tube-labels.svg").write_text("\n".join(out) + "\n")
    print(f"wrote {KIT / '7-tube-labels.svg'}, {len(rows)} labels")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
