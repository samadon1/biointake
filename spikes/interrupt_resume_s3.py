"""Spike: the SAME interrupt/resume experiment as interrupt_resume_file.py, but with S3SessionManager.

BIOINTAKE_S3_BUCKET=... AWS_PROFILE=... python spikes/interrupt_resume_s3.py start
BIOINTAKE_S3_BUCKET=... AWS_PROFILE=... python spikes/interrupt_resume_s3.py resume
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from strands import Agent, tool
from strands.session import S3SessionManager
from strands.types.tools import ToolContext

sys.path.insert(0, str(Path(__file__).resolve().parent))
from interrupt_resume_file import (  # noqa: E402  # noqa: E402
    LOG_FILE,
    STATE,
    StandInModel,
    _load_pending,
    log_side_effect,
    upsert_pending_decision,
)

BUCKET = os.environ["BIOINTAKE_S3_BUCKET"]
PREFIX = os.environ.get("BIOINTAKE_S3_PREFIX", "spike-sessions/")
AGENT_ID = "biointake-spike"
SESSION_FILE = STATE / "s3-session-id.txt"


@tool(context=True)
def request_human_disposition(sample_id: str, issue: str, tool_context: ToolContext) -> dict[str, Any]:
    """Raise a human decision card and wait for the coordinator's choice."""
    session_id = SESSION_FILE.read_text().strip()
    issue_id = f"{session_id}:{sample_id}:{issue}"
    created = upsert_pending_decision(
        issue_id, {"sample_id": sample_id, "issue": issue, "options": ["QUARANTINE", "APPROVE_EXCEPTION"]}
    )
    log_side_effect(f"tool_entered pid={os.getpid()} created_pending={created}")
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


def build_agent(session_id: str) -> Agent:
    return Agent(
        model=StandInModel(),
        tools=[request_human_disposition],
        session_manager=S3SessionManager(
            session_id=session_id,
            bucket=BUCKET,
            prefix=PREFIX,
            region_name=os.environ.get("AWS_DEFAULT_REGION"),
        ),
        agent_id=AGENT_ID,
        callback_handler=None,
    )


def start() -> int:
    import shutil

    shutil.rmtree(STATE, ignore_errors=True)
    STATE.mkdir(exist_ok=True)
    session_id = f"spike-s3-{uuid.uuid4()}"
    SESSION_FILE.write_text(session_id)
    agent = build_agent(session_id)
    result = agent("Process intake case; BX-212 has a temperature excursion.")
    print(f"[start] pid={os.getpid()} session={session_id} stop_reason={result.stop_reason}")
    assert result.stop_reason == "interrupt"
    (STATE / "pending-interrupt.json").write_text(json.dumps([i.id for i in result.interrupts]))
    print(f"[start] interrupt id = {result.interrupts[0].id}")
    return 0


def resume() -> int:
    session_id = SESSION_FILE.read_text().strip()
    ids = json.loads((STATE / "pending-interrupt.json").read_text())
    agent = build_agent(session_id)
    print(
        f"[resume] pid={os.getpid()} restored S3 session {session_id}; interrupt active = {agent._interrupt_state.activated}"
    )
    result = agent([{"interruptResponse": {"interruptId": ids[0], "response": "QUARANTINE"}}])
    pending = _load_pending()
    attempts = LOG_FILE.read_text().count("tool_entered")
    applied = LOG_FILE.read_text().count("decision_applied=QUARANTINE")
    print(f"[resume] stop_reason={result.stop_reason} final={str(result).strip()[:160]}")
    print(
        f"[resume] tool_attempts={attempts} pending_decision_records={len(pending)} decisions_applied={applied}"
    )
    assert result.stop_reason == "end_turn" and len(pending) == 1 and applied == 1
    print(
        "[resume] OK, S3-backed session resumed the interrupt in a fresh process with one logical decision."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(start() if (sys.argv[1] if len(sys.argv) > 1 else "start") == "start" else resume())
