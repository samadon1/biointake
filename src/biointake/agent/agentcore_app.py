"""AgentCore Runtime entrypoint for BioIntake.

Payload = InvocationEvent JSON (from the control API). The runtimeSessionId supplied by AgentCore is
the Strands session id (S3SessionManager), so an interrupt raised on one microVM resumes on another.
Staged uploads arrive as storage URIs (artifact_refs) and are resolved here, bytes never cross the
invocation payload.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from ..agent.events import InvocationEvent
from ..agent.runtime import run_event
from ..api.config import Settings, build_model, build_services, build_session_manager
from ..domain.commands import IncomingArtifact

BOOT_ID = str(uuid.uuid4())
app = BedrockAgentCoreApp()
log = app.logger

_MIME_BY_EXT = {
    ".json": "application/json",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
}


def _settings() -> Settings:
    s = Settings.from_env()
    s.backend = "aws"
    s.invoker = "local"
    s.profile = None  # the runtime uses its execution role
    return s


def _resolve_refs(event: InvocationEvent, storage: Any) -> InvocationEvent:
    if event.evidence is None or not event.evidence.artifact_refs:
        return event
    artifacts = []
    for ref in event.evidence.artifact_refs:
        name = ref.rsplit("/", 1)[-1]
        ext = os.path.splitext(name)[1].lower()
        artifacts.append(
            IncomingArtifact(
                filename=name,
                mime_type=_MIME_BY_EXT.get(ext, "application/octet-stream"),
                content=storage.get(ref),
            )
        )
    delivery = event.evidence.model_copy(update={"artifacts": tuple(artifacts)})
    return event.model_copy(update={"evidence": delivery})


@app.entrypoint
def invoke(payload: dict[str, Any], context: Any) -> dict[str, Any]:
    settings = _settings()
    event = InvocationEvent.model_validate(payload)
    runtime_session_id = getattr(context, "session_id", None)
    session_id = runtime_session_id or event.session_id
    if runtime_session_id and runtime_session_id != event.session_id:
        log.warning(
            "runtimeSessionId %s differs from event.session_id %s; using runtimeSessionId",
            runtime_session_id,
            event.session_id,
        )
        event = event.model_copy(update={"session_id": session_id})
    services = build_services(settings)
    event = _resolve_refs(event, services.storage)
    log.info(
        "BioIntake invocation boot_id=%s event=%s case=%s session=%s",
        BOOT_ID,
        event.event_type.value,
        event.case_id,
        session_id,
    )
    result = run_event(event, services, build_model(settings), build_session_manager(settings, session_id))
    body = result.model_copy(update={"boot_id": BOOT_ID}).model_dump(mode="json")
    body["runtime_session_id"] = runtime_session_id
    return body


if __name__ == "__main__":
    app.run()
