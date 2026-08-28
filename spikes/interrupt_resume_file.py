"""Spike: a Strands tool interrupt survives process teardown and resumes from FileSessionManager.

Run as two separate processes:
    python spikes/interrupt_resume_file.py start
    python spikes/interrupt_resume_file.py resume

Proves (with the offline stand-in, no Bedrock credentials needed):
  1. `tool_context.interrupt()` inside a @tool stops the loop with stop_reason == "interrupt".
  2. Interrupt state is persisted by the session manager.
  3. A *fresh* Agent object in a *new process*, built with the same session_id + agent_id, restores
     the interrupt and resumes the tool with the human response.
  4. Code before the interrupt call re-executes on resume (documented Strands behaviour), so the
     pre-interrupt side effect is an idempotent upsert, the pending-decision record is written ONCE.

This is isolated from the BioIntake production agent (which is not authorized in Phase 1A).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from strands import Agent, tool
from strands.models.model import Model
from strands.session import FileSessionManager
from strands.types.tools import ToolContext

HERE = Path(__file__).resolve().parent
SESSIONS = HERE / ".sessions"
STATE = HERE / ".state"
SESSION_ID = "spike-case-bx-212"
AGENT_ID = "biointake-spike"
PENDING_FILE = STATE / "pending-decisions.json"
LOG_FILE = STATE / "side-effects.log"


# --- a tiny persisted "domain store" for the spike -------------------------------------------
def _load_pending() -> dict[str, Any]:
    return json.loads(PENDING_FILE.read_text()) if PENDING_FILE.exists() else {}


def upsert_pending_decision(issue_id: str, card: dict[str, Any]) -> bool:
    """Idempotent: returns True only when the record is created for the first time."""
    STATE.mkdir(exist_ok=True)
    pending = _load_pending()
    created = issue_id not in pending
    if created:
        pending[issue_id] = {**card, "upsert_count": 1}
    else:
        pending[issue_id]["upsert_count"] += 1  # counted, so duplication would be visible
    PENDING_FILE.write_text(json.dumps(pending, indent=2))
    return created


def log_side_effect(line: str) -> None:
    STATE.mkdir(exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


# --- scripted model: turn 1 calls the tool, turn 2 summarises ---------------------------------
class StandInModel(Model):
    def __init__(self) -> None:
        self._cfg: dict[str, Any] = {}

    def update_config(self, **cfg: Any) -> None:
        self._cfg.update(cfg)

    def get_config(self) -> dict[str, Any]:
        return self._cfg

    async def structured_output(self, output_model, prompt, system_prompt=None, **kw):  # type: ignore[no-untyped-def]
        yield {"output": output_model()}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kw):  # type: ignore[no-untyped-def]
        last = messages[-1]
        has_tool_result = any("toolResult" in c for c in last.get("content", []))
        yield {"messageStart": {"role": "assistant"}}
        if not has_tool_result:
            yield {
                "contentBlockStart": {
                    "start": {"toolUse": {"toolUseId": "tooluse_bx212", "name": "request_human_disposition"}}
                }
            }
            yield {
                "contentBlockDelta": {
                    "delta": {
                        "toolUse": {
                            "input": json.dumps({"sample_id": "BX-212", "issue": "TEMPERATURE_EXCURSION"})
                        }
                    }
                }
            }
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            result = last["content"][0]["toolResult"]["content"]
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": "CASE RESUMED: " + json.dumps(result)}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            }
        }


# --- the interrupting tool ---------------------------------------------------------------------
@tool(context=True)
def request_human_disposition(sample_id: str, issue: str, tool_context: ToolContext) -> dict[str, Any]:
    """Raise a human decision card and wait for the coordinator's choice."""
    issue_id = f"{SESSION_ID}:{sample_id}:{issue}"
    # Everything before interrupt() re-runs on resume → reads + ONE idempotent upsert only.
    created = upsert_pending_decision(
        issue_id, {"sample_id": sample_id, "issue": issue, "options": ["QUARANTINE", "APPROVE_EXCEPTION"]}
    )
    log_side_effect(f"tool_entered pid={id(tool_context.agent)} created_pending={created}")
    decision = tool_context.interrupt(
        "human_disposition",
        reason={"issue_id": issue_id, "sample_id": sample_id, "options": ["QUARANTINE", "APPROVE_EXCEPTION"]},
    )
    log_side_effect(f"decision_applied={decision}")
    return {
        "status": "success",
        "content": [
            {"json": {"issue_id": issue_id, "decision": decision, "pending_created_this_run": created}}
        ],
    }


def build_agent() -> Agent:
    return Agent(
        model=StandInModel(),
        tools=[request_human_disposition],
        session_manager=FileSessionManager(session_id=SESSION_ID, storage_dir=str(SESSIONS)),
        agent_id=AGENT_ID,
        callback_handler=None,
    )


def start() -> int:
    import shutil

    shutil.rmtree(SESSIONS, ignore_errors=True)
    shutil.rmtree(STATE, ignore_errors=True)
    agent = build_agent()
    result = agent("Process intake case; BX-212 has a temperature excursion.")
    print(f"[start] stop_reason = {result.stop_reason}")
    assert result.stop_reason == "interrupt", "expected the tool to interrupt"
    for it in result.interrupts:
        print(f"[start] interrupt id = {it.id}")
        print(f"[start] interrupt reason = {json.dumps(it.reason)}")
    (STATE / "pending-interrupt.json").write_text(json.dumps([i.id for i in result.interrupts]))
    print(f"[start] pending decisions on disk: {json.dumps(_load_pending())}")
    print("[start] agent object discarded; process exiting.")
    return 0


def resume() -> int:
    ids = json.loads((STATE / "pending-interrupt.json").read_text())
    agent = build_agent()  # brand-new object, brand-new process
    print(f"[resume] restored session {SESSION_ID}; interrupt active = {agent._interrupt_state.activated}")
    result = agent([{"interruptResponse": {"interruptId": ids[0], "response": "QUARANTINE"}}])
    print(f"[resume] stop_reason = {result.stop_reason}")
    print(f"[resume] final text = {str(result).strip()}")
    pending = _load_pending()
    counts = {k: v["upsert_count"] for k, v in pending.items()}
    print(f"[resume] pending-decision upsert counts = {counts}")
    print("[resume] side-effect log:\n" + LOG_FILE.read_text())
    assert result.stop_reason == "end_turn"
    assert all(v["upsert_count"] == 2 for v in pending.values()), "tool body ran twice (expected) …"
    assert len(pending) == 1, "… but the pending-decision RECORD must exist exactly once"
    attempts = LOG_FILE.read_text().count("tool_entered")
    applied = LOG_FILE.read_text().count("decision_applied=QUARANTINE")
    assert applied == 1
    print(
        f"[resume] tool_attempts={attempts} pending_decision_records={len(pending)} decisions_applied={applied}"
    )
    print("[resume] OK, one pending-decision record, one applied decision, resumed in a fresh process.")
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "start"
    raise SystemExit(start() if mode == "start" else resume())
