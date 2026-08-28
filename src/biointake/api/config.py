"""Settings + service wiring for local (memory/File sessions) and AWS (DynamoDB/S3/S3 sessions) backends."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ..clock import Clock, SteppingClock, utc_now
from ..domain.policies import default_policy
from ..fixtures import DEFAULT_FIXTURE_DIR, load_package
from ..repositories.interfaces import Repository
from ..repositories.memory import InMemoryRepository
from ..services.delivery import MessageDelivery, RecordedDelivery, SesDelivery
from ..services.intake import IntakeService
from ..services.lims_demo import DemoLims, RepositoryLimsStore
from ..storage.interfaces import ArtifactStorage
from ..storage.local import MemoryArtifactStorage

Backend = Literal["memory", "aws"]
Invoker = Literal["local", "agentcore"]


@dataclass
class Settings:
    backend: Backend = "memory"
    invoker: Invoker = "local"
    model_id: str = "offline"
    region: str = "us-east-1"
    profile: str | None = None
    ddb_table: str = "biointake-demo"
    s3_bucket: str = ""
    runtime_arn: str = ""
    session_dir: Path = field(default_factory=lambda: Path(".local") / "api-sessions")
    fixture_dir: Path = field(default_factory=lambda: DEFAULT_FIXTURE_DIR)
    deterministic_clock: bool = False
    users_spec: str = ""  # "user_id|Display Name|ROLE|token" entries separated by ";"
    # Offers the staff of this deployment on the sign-in screen, one click each, with their real
    # tokens. For a deployment whose whole point is being looked at: a reviewer who cannot get past
    # the front door has been told nothing. Off unless asked for, because a lab must never ship a
    # panel that hands out its principal investigator's credential.
    demo_sign_in: bool = False
    delivery: str = "recorded"  # "recorded" files the message; "ses" sends it
    mail_from: str = ""  # the verified SES address the lab sends from
    ses_configuration_set: str = ""
    portal_base_url: str = ""  # e.g. https://intake.example.org, the origin the sender's link points at

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            backend=os.environ.get("BIOINTAKE_BACKEND", "memory"),  # type: ignore[arg-type]
            invoker=os.environ.get("BIOINTAKE_INVOKER", "local"),  # type: ignore[arg-type]
            model_id=os.environ.get("BIOINTAKE_MODEL_ID", "offline"),
            region=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")),
            profile=os.environ.get("BIOINTAKE_AWS_PROFILE") or None,
            ddb_table=os.environ.get("BIOINTAKE_DDB_TABLE", "biointake-demo"),
            s3_bucket=os.environ.get("BIOINTAKE_S3_BUCKET", ""),
            runtime_arn=os.environ.get("BIOINTAKE_RUNTIME_ARN", ""),
            session_dir=Path(os.environ.get("BIOINTAKE_SESSION_DIR", ".local/api-sessions")),
            users_spec=os.environ.get("BIOINTAKE_USERS", ""),
            demo_sign_in=os.environ.get("BIOINTAKE_DEMO_SIGN_IN", "") == "1",
            delivery=os.environ.get("BIOINTAKE_DELIVERY", "recorded"),
            mail_from=os.environ.get("BIOINTAKE_MAIL_FROM", ""),
            ses_configuration_set=os.environ.get("BIOINTAKE_SES_CONFIGURATION_SET", ""),
            portal_base_url=os.environ.get("BIOINTAKE_PORTAL_BASE_URL", ""),
            # The packaged fixtures live outside the wheel, so a container has to be told where they
            # were copied to. DEFAULT_FIXTURE_DIR is resolved from the source tree and is wrong there.
            fixture_dir=Path(os.environ.get("BIOINTAKE_FIXTURE_DIR", str(DEFAULT_FIXTURE_DIR))),
        )

    def boto_session(self) -> Any:
        import boto3

        return boto3.Session(profile_name=self.profile, region_name=self.region)


def make_clock(settings: Settings) -> Clock:
    return (
        SteppingClock(datetime(2026, 8, 26, 16, 0, tzinfo=UTC)) if settings.deterministic_clock else utc_now
    )


def build_repository(settings: Settings, clock: Clock) -> Repository:
    if settings.backend == "aws":
        from ..repositories.dynamodb import DynamoDBRepository

        return DynamoDBRepository(settings.ddb_table, session=settings.boto_session(), clock=clock)
    return InMemoryRepository(clock)


def build_storage(settings: Settings) -> ArtifactStorage:
    if settings.backend == "aws":
        from ..storage.s3 import S3ArtifactStorage

        return S3ArtifactStorage(settings.s3_bucket, session=settings.boto_session())
    return MemoryArtifactStorage()


def build_services(
    settings: Settings,
    *,
    repo: Repository | None = None,
    storage: ArtifactStorage | None = None,
    clock: Clock | None = None,
) -> IntakeService:
    clock = clock or make_clock(settings)
    repo = repo or build_repository(settings, clock)
    storage = storage or build_storage(settings)
    lims = DemoLims.from_store(RepositoryLimsStore(repo))
    return IntakeService(
        repo,
        storage,
        default_policy(),
        clock,
        lims=lims,
        delivery=build_delivery(settings),
        portal_base_url=settings.portal_base_url,
    )


def build_delivery(settings: Settings) -> MessageDelivery:
    """How a request reaches the sending site. Filed by default; a lab evaluating BioIntake should
    not have it emailing real people until it says so."""
    if settings.delivery != "ses":
        return RecordedDelivery()
    if not settings.mail_from:
        raise RuntimeError("BIOINTAKE_DELIVERY=ses needs BIOINTAKE_MAIL_FROM, a verified SES address")
    return SesDelivery(settings.mail_from, settings.boto_session(), settings.ses_configuration_set)


def _anthropic_key_from_store() -> str:
    """Fetch the key from a parameter store, for runtimes configured with a name rather than a value.

    The AgentCore runtime takes its environment from `agentcore.json`, which is a file in this
    repository, so the key cannot be passed that way. A parameter name can: it is not a credential,
    and the runtime's own role decides whether it may read what the name points at.

    SSM rather than Secrets Manager because this is one short string that is read once at start-up,
    which is what Parameter Store is for, and it costs nothing.
    """
    import os

    name = os.environ.get("ANTHROPIC_API_KEY_PARAMETER")
    if not name:
        return ""
    import boto3

    client = boto3.Session().client("ssm")
    return str(client.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]).strip()


def build_model(settings: Settings) -> Any:
    """Pick the token generator.

    Three ways in, chosen by BIOINTAKE_MODEL_ID alone:

      offline             the deterministic stand-in used by every offline test
      anthropic:<model>   the Anthropic API directly, keyed from ANTHROPIC_API_KEY
      <anything else>     Amazon Bedrock, using the ambient AWS credentials

    The Anthropic route exists because the agent's design has never been driven by a live model, and this
    account's Bedrock quotas are zero. What is being evaluated is whether a real model stays inside the
    bounds the tools and the policy engine impose, and that is true of any competent model; the provider
    is incidental. The hackathon requires Strands, which is the layer above all three of these.
    """
    if settings.model_id == "offline":
        from ..agent.testing import StandInModel, canonical_policy

        return StandInModel(canonical_policy)

    if settings.model_id == "scripted":
        # The old name for "offline". Without this it falls through to Bedrock, which takes any
        # string as a model id and then fails on first invocation with nothing to point at.
        raise RuntimeError('BIOINTAKE_MODEL_ID="scripted" is now "offline". Update your .env.')

    if settings.model_id.startswith("anthropic:"):
        import os

        from strands.models.anthropic import AnthropicModel

        key = os.environ.get("ANTHROPIC_API_KEY") or _anthropic_key_from_store()
        if not key:
            raise RuntimeError(
                "BIOINTAKE_MODEL_ID asks for the Anthropic API but no key was found. Set "
                "ANTHROPIC_API_KEY (a gitignored .env will do), or ANTHROPIC_API_KEY_PARAMETER "
                "naming an SSM parameter. Never in the repository."
            )
        return AnthropicModel(
            client_args={"api_key": key},
            model_id=settings.model_id.removeprefix("anthropic:"),
            max_tokens=4096,
        )

    from strands.models.bedrock import BedrockModel

    return BedrockModel(model_id=settings.model_id, boto_session=settings.boto_session())


def build_session_manager(settings: Settings, session_id: str) -> Any:
    if settings.backend == "aws":
        from strands.session import S3SessionManager

        return S3SessionManager(
            session_id=session_id,
            bucket=settings.s3_bucket,
            prefix="sessions/",
            boto_session=settings.boto_session(),
        )
    from strands.session import FileSessionManager

    settings.session_dir.mkdir(parents=True, exist_ok=True)
    return FileSessionManager(session_id=session_id, storage_dir=str(settings.session_dir))


def demo_package(settings: Settings) -> Any:
    return load_package(settings.fixture_dir)
