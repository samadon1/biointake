#!/usr/bin/env python3
"""Generate docs/architecture.svg.

Drawn in the idiom AWS actually uses in its Architecture Blog, which is much plainer than a product
diagram: a white canvas, official service icons at full size with the service name centred underneath and
nothing else, thin dashed rectangles for logical groupings, orthogonal black connectors with small solid
arrowheads, and numbered dark badges sitting on the lines. There are no cards, no tinted panels, no prose
inside the boxes. The walkthrough lives in the surrounding text, as it does in their posts.

Kept as a generator rather than a hand-edited SVG so that when the architecture changes, the diff is a
change to the description rather than to several hundred path coordinates.
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.sax.saxutils import escape

ICONS = Path(__file__).parent / "icons" / "aws"

W, H = 1240, 870
FONT = "'Amazon Ember','Helvetica Neue',Helvetica,Arial,sans-serif"
INK = "#232f3e"
LABEL = "#16191f"
NOTE = "#5f6b7a"
WIRE = "#232f3e"
DASH = "#879196"
NONAWS = "#5b6b80"

out: list[str] = []
add = out.append


def text(x, y, s, size=12, fill=LABEL, weight="400", anchor="middle", style=""):
    add(f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{size}" fill="{fill}" font-weight="{weight}" '
        f'text-anchor="{anchor}"{style}>{escape(s)}</text>')


def icon(name: str, cx: float, top: float, size: float) -> None:
    """Place an official AWS icon, centred horizontally, geometry untouched.

    AWS publish these for exactly this purpose and ask that they not be altered, so the root element is
    rewritten only to position and scale, so the viewBox and every path inside are the file as shipped.
    """
    raw = (ICONS / name).read_text()
    m = re.search(r"<svg[^>]*>", raw)
    vb = re.search(r'viewBox="([^"]+)"', m.group(0)) if m else None
    if m is None or vb is None:
        raise ValueError(f"{name}: expected an <svg> root with a viewBox")
    add(f'<svg x="{cx - size/2}" y="{top}" width="{size}" height="{size}" viewBox="{vb.group(1)}">{raw[m.end():]}')


def service(cx, top, name, label, size=64, caption=None, above=None):
    """An icon with its service name beneath it. The whole vocabulary of an AWS diagram."""
    if above:
        text(cx, top - 10, above, size=11.5, fill=NOTE)
    icon(name, cx, top, size)
    y = top + size + 16
    for line in label.split("\n"):
        text(cx, y, line, size=12)
        y += 15
    if caption:
        for line in caption.split("\n"):
            text(cx, y, line, size=11, fill=NOTE)
            y += 14


def plain(cx, top, w, h, label, caption=None, dashed=False, colour=NONAWS):
    """A component that is not an AWS service: a plain outlined box, as AWS draws third-party pieces."""
    d = ' stroke-dasharray="4 3"' if dashed else ""
    add(f'<rect x="{cx - w/2}" y="{top}" width="{w}" height="{h}" rx="2" fill="#ffffff" stroke="{colour}" '
        f'stroke-width="1.4"{d}/>')
    y = top + h / 2 + (0 if caption is None else -6)
    for line in label.split("\n"):
        text(cx, y, line, size=12, weight="600", fill=colour)
        y += 14
    if caption:
        for line in caption.split("\n"):
            text(cx, y + 2, line, size=10.5, fill=NOTE)
            y += 12


def region(x, y, w, h, label, stroke=DASH, dashed=True, label_anchor="start"):
    d = ' stroke-dasharray="5 4"' if dashed else ""
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="2" fill="none" stroke="{stroke}" stroke-width="1.2"{d}/>')
    if label_anchor == "middle":
        text(x + w / 2, y + 18, label, size=12, weight="600", fill=INK)
    else:
        text(x + 12, y + 18, label, size=12, weight="600", fill=INK, anchor="start")


def wire(pts, dashed=False):
    """Orthogonal connector. AWS diagrams turn at right angles; they do not curve."""
    d = ' stroke-dasharray="5 4"' if dashed else ""
    path = "M" + " L".join(f"{x} {y}" for x, y in pts)
    add(f'<path d="{path}" fill="none" stroke="{WIRE}" stroke-width="1.3" marker-end="url(#h)"{d}/>')


def badge(x, y, n):
    add(f'<circle cx="{x}" cy="{y}" r="11.5" fill="#3f4c5c"/>')
    add(f'<text x="{x}" y="{y+4.2}" font-family="{FONT}" font-size="12" font-weight="700" fill="#ffffff" '
        f'text-anchor="middle">{n}</text>')


def note(x, y, lines, anchor="middle"):
    for i, line in enumerate(lines):
        text(x, y + i * 14, line, size=11, fill=NOTE, anchor=anchor)


# ---------------------------------------------------------------------------------- canvas
add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
    f'aria-label="BioIntake architecture: two groups of users reach a Next.js front end and a FastAPI control '
    f'API, both on AWS App Runner, which invoke a Strands agent on Amazon Bedrock AgentCore; a '
    f'deterministic policy engine sits '
    f'between the agent and DynamoDB, Amazon S3 and the LIMS, and alone authorises acceptance. Staff '
    f'credentials come from AWS Secrets Manager and evidence requests go out through Amazon SES.">')
add(f'<defs><marker id="h" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" '
    f'orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="{WIRE}"/></marker></defs>')
add(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')

text(40, 34, "BioIntake architecture", size=16, weight="700", anchor="start")
text(40, 54, "Autonomous biospecimen intake. The agent can chase what is missing. Only the policy engine can accept a specimen.",
     size=12, fill=NOTE, anchor="start")

# ---------------------------------------------------------------------------------- people
service(74, 176, "Res_User_48_Light.svg", "Sending site\ncoordinator", size=48)
note(74, 284, ["Announces the shipment,", "uploads the manifest,", "answers questions"])

service(74, 470, "Res_Users_48_Light.svg", "Receiving lab", size=48)
note(74, 564, ["Coordinator, principal", "investigator, QA reviewer"])

# ---------------------------------------------------------------------------------- AWS
icon("AWS-Cloud-logo_32.svg", 163, 80, 20)
text(181, 95, "AWS Cloud", size=12, weight="600", fill=INK, anchor="start")
add(f'<rect x="140" y="70" width="880" height="682" rx="2" fill="none" stroke="{INK}" stroke-width="1.2"/>')
region(158, 114, 844, 622, "us-east-1", label_anchor="start")

# The control API sits between the two front ends it serves, so neither connector has to be routed
# through the other's box. An arrow crossing a component is the fastest way to make a reader distrust
# a diagram.
region(176, 140, 240, 400, "")
icon("Arch_AWS-App-Runner_64.svg", 194, 148, 18)
text(210, 163, "AWS App Runner", size=12, weight="600", fill=INK, anchor="start")

plain(296, 176, 190, 62, "Sender portal", "Next.js. One link per request.")
plain(296, 306, 190, 96, "Control API",
      "FastAPI. Manifests, receipt,\nscanning, batch commit,\nevidence and decisions.")
plain(296, 470, 190, 62, "Operations console", "Next.js. Queue, bench,\ncase workspace, decisions.")

service(580, 190, "Arch_Amazon-Bedrock-AgentCore_64.svg", "Amazon Bedrock\nAgentCore Runtime",
        caption="One microVM per session.\nStrands agent, 11 tools.\nHooks and interventions.")

service(840, 190, "Arch_Amazon-Bedrock_64.svg", "Amazon Bedrock",
        caption="Reads documents and\ndrafts messages.\nIt does not decide.")

# The gate is on acceptance specifically. Ordinary intake writes go through the same shared library but not
# through this decision, so the box names what it actually guards.
plain(740, 396, 500, 74, "Deterministic policy engine",
      "Checks and DispositionEngine. The agent can ask for a disposition.\n"
      "Only this engine grants one, and the model never returns ALLOWED.",
      colour="#b4441f")

service(248, 578, "Arch_AWS-Secrets-Manager_64.svg", "AWS Secrets Manager",
        caption="Staff tokens. Only the hash\nis ever stored; the service\nrefuses to start without them.")

service(400, 578, "Arch_Amazon-Simple-Email-Service_64.svg", "Amazon SES",
        caption="Carries an evidence request\nto the sending site, with the\nsingle-use link it answers on.")

region(490, 502, 500, 170, "Records", label_anchor="start")

service(610, 540, "Arch_Amazon-DynamoDB_64.svg", "Amazon DynamoDB", caption="One table, indexed by case")
service(860, 540, "Arch_Amazon-Simple-Storage-Service_64.svg", "Amazon S3",
        caption="Artifacts and\nagent sessions")

# The lab's LIMS is the lab's own system, so it sits outside our account.
plain(1110, 540, 150, 62, "LIMS", "The lab's own\nsystem of record")
text(1110, 622, "Outside our account", size=10.5, fill=NOTE)

# ---------------------------------------------------------------------------------- wires
wire([(100, 200), (199, 200)])                                          # coordinator to portal
wire([(100, 494), (199, 494)])                                          # lab to console
wire([(296, 238), (296, 302)])                                          # portal to API
wire([(296, 470), (296, 406)])                                          # console to API
wire([(391, 380), (450, 380), (450, 600), (486, 600)])                  # API writes the intake records
wire([(391, 330), (440, 330), (440, 222), (544, 222)])                  # API invokes the runtime
wire([(612, 222), (806, 222)])                                          # runtime to Bedrock
wire([(548, 240), (462, 240), (462, 366), (393, 366)], dashed=True)     # runtime back to API
wire([(580, 336), (580, 392)])                                          # runtime to the policy engine
wire([(610, 470), (610, 534)])                                          # engine to DynamoDB
wire([(860, 470), (860, 534)])                                          # engine to S3
wire([(990, 433), (1110, 433), (1110, 534)])                            # engine to the lab's LIMS
wire([(248, 572), (248, 542)], dashed=True)                             # secrets read at start-up
wire([(400, 542), (400, 572)])                                          # the API sends through SES

# One number per arrow. Repeating a number is right where several arrows are genuinely one step, which is
# how AWS label a fan-out; it is wrong where two arrows are different moments, as steps 2 and 4 are.
# 119 is the true centre of the gutter between the person icon (ends at 98) and the cloud boundary (140).
# At 122 they were 12.5px from the icon and 6.5px from the border, which reads as drifting right.
for bx, by, bn in [
    (119, 200, 1),    # coordinator reaches the portal
    (296, 270, 2),    # portal calls the API
    (119, 494, 3),    # lab reaches the console
    (296, 438, 4),    # console calls the API
    (450, 500, 5),    # the API writes announcement, receipt, scans and the committed batch
    (440, 276, 6),    # the API invokes the runtime
    (710, 222, 7),    # the runtime calls the model
    (580, 364, 8),    # the runtime asks the engine
    (610, 484, 9),    # the engine writes what it decided, to each of the three
    (860, 484, 9),
    (1110, 484, 9),
    (462, 300, 10),   # the runtime hands back and the case waits
]:
    badge(bx, by, bn)

note(740, 782, ["The agent's session is stored in Amazon S3, so a decision that arrives three days later"])
note(740, 796, ["resumes the same case, on whichever microVM picks it up."])

# ---------------------------------------------------------------------------------- footer
add(f'<line x1="40" y1="822" x2="{W-40}" y2="822" stroke="#e3e6ea" stroke-width="1"/>')
text(40, 844, "AWS Architecture Icons \u00a9 Amazon Web Services, used unmodified.", size=10.5, fill=NOTE, anchor="start")

add("</svg>")
Path("docs/architecture.svg").write_text("\n".join(out) + "\n")
print("wrote docs/architecture.svg")
