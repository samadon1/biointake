"""Deterministic stand-in models for model-independent trajectory tests.

`StandInModel` is a real Strands `Model`: the event loop, tool executor, hooks, interventions,
session manager and interrupts are all the genuine article, only the token generator is substituted.
A `Policy` maps the conversation so far (`View`) to the next assistant turn.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from strands.models.model import Model


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class Turn:
    tool_calls: list[ToolCall] = field(default_factory=list)
    text: str = "Done."


class View:
    """Read-only helpers over the Strands message list."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self._names: dict[str, str] = {}
        for m in messages:
            if m.get("role") == "assistant":
                for block in m.get("content", []):
                    if "toolUse" in block:
                        self._names[block["toolUse"]["toolUseId"]] = block["toolUse"]["name"]

    def prompt_text(self) -> str:
        for m in reversed(self.messages):
            if m.get("role") == "user":
                texts = [b["text"] for b in m.get("content", []) if "text" in b]
                if texts:
                    return "\n".join(texts)
        return ""

    def event_type(self) -> str:
        m = re.search(r"EVENT: (\w+)", self.prompt_text())
        return m.group(1) if m else ""

    def results_since_prompt(self) -> list[tuple[str, dict[str, Any]]]:
        """(tool_name, body) for every tool result after the most recent text prompt."""
        out: list[tuple[str, dict[str, Any]]] = []
        for m in self.messages:
            if m.get("role") != "user":
                continue
            content = m.get("content", [])
            if any("text" in b for b in content):
                out = []
                continue
            for b in content:
                if "toolResult" in b:
                    tr = b["toolResult"]
                    body: dict[str, Any] = {}
                    for c in tr.get("content", []):
                        if "json" in c:
                            body = dict(c["json"])
                        elif "text" in c and not body:
                            body = {"status": tr.get("status"), "summary": c["text"]}
                    out.append((self._names.get(tr["toolUseId"], "?"), body))
        return out

    def latest(self, name: str) -> dict[str, Any] | None:
        for n, body in reversed(self.results_since_prompt()):
            if n == name:
                return body
        return None

    def called(self, name: str) -> bool:
        return any(n == name for n, _ in self.results_since_prompt())

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.results_since_prompt() if n == name)

    def fenced_texts(self) -> list[str]:
        return re.findall(
            r"\[(?:manifest row [^\]]*|sender message)\] (.*?)\n<<< END UNTRUSTED",
            self.prompt_text(),
            flags=re.S,
        )

    def contacts(self) -> list[dict[str, Any]]:
        m = re.search(
            r"VERIFIED CONTACTS \(choose by contact_id only\):\n(\[.*?\])\n", self.prompt_text(), flags=re.S
        )
        return json.loads(m.group(1)) if m else []


Policy = Callable[[View], Turn]

VERIFICATION_SEQUENCE = [
    "inspect_manifest_and_labels",
    "evaluate_temperature_logs",
    "verify_consent_and_protocol",
    "check_chain_of_custody",
    "reconcile_lims_records",
]


class StandInModel(Model):
    def __init__(self, policy: Policy) -> None:
        self._policy = policy
        self._cfg: dict[str, Any] = {}
        self._n = 0
        self.turns: list[Turn] = []

    def update_config(self, **cfg: Any) -> None:
        self._cfg.update(cfg)

    def get_config(self) -> dict[str, Any]:
        return self._cfg

    async def structured_output(self, output_model, prompt, system_prompt=None, **kw):  # type: ignore[no-untyped-def]
        yield {"output": output_model()}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):  # type: ignore[no-untyped-def]
        turn = self._policy(View(messages))
        self.turns.append(turn)
        yield {"messageStart": {"role": "assistant"}}
        if turn.tool_calls:
            for call in turn.tool_calls:
                self._n += 1
                tid = f"tooluse_{self._n:04d}"
                yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": tid, "name": call.name}}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(call.input)}}}}
                yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": turn.text}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            }
        }


# ----------------------------------------------------------------------------------------------
_CORRECTION = re.compile(r"[Rr]ow (\d+) of the manifest is (BX-\d{3})")
_ROW_VALUE = re.compile(r"reads '([^']+)'")


def _snapshot(view: View) -> dict[str, Any]:
    body = view.latest("get_case_snapshot")
    return dict(body["data"]) if body else {}


def _samples_in(snapshot: dict[str, Any], state: str) -> list[str]:
    return [s["sample_id"] for s in snapshot.get("samples", []) if s.get("state") == state]


def canonical_policy(view: View) -> Turn:  # noqa: C901
    """The behaviour the real model is expected to reproduce for SHIP-DEMO-001."""
    results = view.results_since_prompt()
    if not results:
        return Turn(tool_calls=[ToolCall("get_case_snapshot")])
    snap = _snapshot(view)
    event = view.event_type()
    last_name, last_body = results[-1]

    # after a human decision has been applied → finalize
    if last_name == "request_human_disposition" and last_body.get("success"):
        return Turn(tool_calls=[ToolCall("finalize_intake")])
    if last_name == "finalize_intake":
        return Turn(text=f"Case finalized: {last_body.get('summary')}")

    if event == "EVIDENCE_RECEIVED":
        if not view.called("admit_and_reverify_received_evidence"):
            corrections = []
            for text in view.fenced_texts():
                for row, sid in _CORRECTION.findall(text):
                    # the tentative manifest value is visible in the snapshot blockers
                    manifest_value = None
                    for s in snap.get("samples", []):
                        if s["sample_id"] == sid:
                            for b in s.get("blockers", []):
                                m = _ROW_VALUE.search(str(b.get("observed", "")))
                                if m:
                                    manifest_value = m.group(1)
                    if manifest_value:
                        corrections.append(
                            {
                                "manifest_row": int(row),
                                "manifest_value": manifest_value,
                                "corrected_value": sid,
                                "sender_statement": text[:200],
                            }
                        )
            return Turn(
                tool_calls=[
                    ToolCall("admit_and_reverify_received_evidence", {"proposed_corrections": corrections})
                ]
            )
        admitted = view.latest("admit_and_reverify_received_evidence") or {}
        reverified = admitted.get("data", {}).get("reverified_sample_ids", [])
        if reverified and not view.called("commit_dispositions"):
            return Turn(
                tool_calls=[
                    ToolCall(
                        "commit_dispositions",
                        {
                            "requests": [
                                {
                                    "sample_id": s,
                                    "requested": "ACCEPT",
                                    "rationale": "re-verified after evidence",
                                }
                                for s in reverified
                            ]
                        },
                    )
                ]
            )
        needs_human = _samples_in(snap, "NEEDS_HUMAN_DECISION")
        # Raise a card whenever one is needed and none is outstanding, not merely the first time. A
        # quarantine reopened weeks later needs a fresh decision, and "have I called this tool before?"
        # is not a question a real model would ask itself.
        if needs_human and not snap.get("pending_decisions"):
            return Turn(
                tool_calls=[
                    ToolCall(
                        "request_human_disposition",
                        {
                            "sample_id": needs_human[0],
                            "proposed_options": ["QUARANTINE", "REJECT", "APPROVE_EXCEPTION"],
                        },
                    )
                ]
            )
        return Turn(text="Evidence admitted and dispositions committed.")

    # CASE_READY (and RETRY_REQUESTED)
    if not all(view.called(n) for n in VERIFICATION_SEQUENCE):
        return Turn(tool_calls=[ToolCall(n) for n in VERIFICATION_SEQUENCE])  # five tools in ONE turn
    commits = view.count("commit_dispositions")
    if commits == 0:
        pending = _samples_in(snap, "PENDING")
        return Turn(
            tool_calls=[
                ToolCall(
                    "commit_dispositions",
                    {
                        "requests": [
                            {"sample_id": s, "requested": "ACCEPT", "rationale": "all checks available"}
                            for s in pending
                        ]
                    },
                )
            ]
        )
    first_commit = view.latest("commit_dispositions") or {}
    outcomes = first_commit.get("data", {}).get("outcomes", {})
    collisions = [
        sid
        for sid, o in outcomes.items()
        if o.get("status") == "denied" and "BARCODE_COLLISION" in o.get("reason_codes", [])
    ]
    if collisions and commits == 1:
        return Turn(
            tool_calls=[
                ToolCall(
                    "commit_dispositions",
                    {
                        "requests": [
                            {
                                "sample_id": s,
                                "requested": "QUARANTINE",
                                "rationale": "barcode collision with an existing record",
                            }
                            for s in collisions
                        ]
                    },
                )
            ]
        )
    seen: dict[str, dict[str, Any]] = {}
    for n, body in results:
        if n == "commit_dispositions":
            for u in body.get("data", {}).get("unresolved_requirements", []):
                seen[f"{u['requirement_type']}:{u['sample_id']}"] = u
    unresolved = list(seen.values())
    if unresolved and not view.called("create_evidence_request"):
        keys = sorted({f"{u['requirement_type']}:{u['sample_id']}" for u in unresolved})
        # choose the coordinator named in the manifest notes, from the VERIFIED list only
        contacts = view.contacts()
        recipient = contacts[0]["contact_id"] if contacts else ""
        for text in view.fenced_texts():
            for c in contacts:
                surname = (
                    c["display_name"].split(" ")[1].strip("()")
                    if " " in c["display_name"]
                    else c["display_name"]
                )
                if surname and surname in text:
                    recipient = c["contact_id"]
        draft = (
            f"Hello; we have shipment {snap.get('shipment_id', '')} on the receiving bench and cannot "
            f"verify {len(keys)} item(s) from what came in the box. They are listed above. Could you "
            "upload them using the secure link? Everything else is fine and nothing is being held up "
            "beyond these. Thank you."
        )
        return Turn(
            tool_calls=[
                ToolCall(
                    "create_evidence_request",
                    {
                        "recipient_contact_id": recipient,
                        "requirement_keys": keys,
                        "draft_message": draft,
                        "grouping_rationale": "same site coordinator; one message avoids duplicate requests",
                    },
                )
            ]
        )
    # A sample can reach NEEDS_HUMAN_DECISION on this path too, most obviously when a quarantine has been
    # reopened and re-verified, which happens long after the original evidence round is over.
    needs_human = _samples_in(snap, "NEEDS_HUMAN_DECISION")
    if needs_human and not snap.get("pending_decisions"):
        return Turn(
            tool_calls=[
                ToolCall(
                    "request_human_disposition",
                    {
                        "sample_id": needs_human[0],
                        "proposed_options": ["QUARANTINE", "REJECT", "APPROVE_EXCEPTION"],
                    },
                )
            ]
        )
    return Turn(text="Initial verification complete; waiting for the sender's evidence.")
