"""Agent construction. One agent, explicit sequential tool execution, fail-closed policy handler."""

from __future__ import annotations

from typing import Any

from strands import Agent
from strands.session.session_manager import SessionManager
from strands.tools.executors import SequentialToolExecutor

from .hooks import AuditHookProvider
from .interventions import BioIntakePolicyHandler
from .prompt import SYSTEM_PROMPT
from .tools import ALL_TOOLS

AGENT_ID = "biointake"


def build_agent(
    model: Any, session_manager: SessionManager | None, *, trace_attributes: dict[str, Any] | None = None
) -> Agent:
    return Agent(
        model=model,
        tools=ALL_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        session_manager=session_manager,
        interventions=[BioIntakePolicyHandler()],
        hooks=[AuditHookProvider()],
        tool_executor=SequentialToolExecutor(),  # later tools must see earlier tools' committed state
        agent_id=AGENT_ID,
        callback_handler=None,
        trace_attributes=trace_attributes or {},
    )
