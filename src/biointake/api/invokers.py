"""How the control API runs the agent: in-process (local) or through AgentCore Runtime (deployed)."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from ..agent.events import InvocationEvent, RunResult
from ..agent.runtime import run_event
from ..services.intake import IntakeService
from .config import Settings, build_model, build_session_manager


class AgentInvoker(ABC):
    @abstractmethod
    def invoke(self, event: InvocationEvent) -> RunResult: ...


class LocalInvoker(AgentInvoker):
    """Runs the real Strands loop inside the API process (memory or AWS-backed repositories)."""

    def __init__(self, settings: Settings, services: IntakeService) -> None:
        self._settings = settings
        self._services = services

    def invoke(self, event: InvocationEvent) -> RunResult:
        return run_event(
            event,
            self._services,
            build_model(self._settings),
            build_session_manager(self._settings, event.session_id),
        )


class AgentCoreInvoker(AgentInvoker):
    """Invokes the deployed BioIntake runtime with the same runtimeSessionId as the Strands session."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = settings.boto_session().client("bedrock-agentcore")

    def invoke(self, event: InvocationEvent) -> RunResult:
        if event.evidence is not None and event.evidence.artifacts:
            raise ValueError(
                "stage uploads to storage and pass artifact_refs; raw bytes cannot cross the AgentCore payload"
            )
        payload = event.model_dump(mode="json")
        r = self._client.invoke_agent_runtime(
            agentRuntimeArn=self._settings.runtime_arn,
            runtimeSessionId=event.session_id,
            payload=json.dumps(payload).encode(),
            contentType="application/json",
            accept="application/json",
        )
        raw = r["response"].read() if hasattr(r.get("response"), "read") else r.get("response")
        text = raw.decode() if isinstance(raw, bytes | bytearray) else str(raw)
        body: dict[str, Any] = json.loads(text)
        if "error" in body and "stop_reason" not in body:
            raise RuntimeError(f"runtime error: {body['error']}")
        return RunResult.model_validate({k: v for k, v in body.items() if k in RunResult.model_fields})
