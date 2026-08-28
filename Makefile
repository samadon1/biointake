.PHONY: eval-live install lint typecheck test test-hardening test-agent demo-deterministic demo-agent-local spike-interrupt-file fixtures aws-readiness aws-provision build-wheel deploy-runtime e2e-local e2e-aws e2e-agentcore api-dev check
PROFILE ?= biointake-hack
MODEL ?= anthropic:claude-sonnet-4-5-20250929
RUNS ?= 3
REGION ?= us-east-1
# Your AWS account, read from the profile so a clone needs no editing.
ACCOUNT ?= $(shell aws sts get-caller-identity --profile $(PROFILE) --query Account --output text 2>/dev/null)
RUNTIME_ARN ?= $(shell cat .local/biointake-runtime-arn.txt 2>/dev/null)

install:
	uv sync --group dev

lint:
	uv run ruff check src tests scripts spikes
	uv run ruff format --check src tests scripts spikes

format:
	uv run ruff format src tests scripts spikes
	uv run ruff check --fix src tests scripts spikes

typecheck:
	uv run mypy

test:
	uv run pytest

test-hardening:
	uv run pytest tests/hardening

test-agent:
	uv run pytest tests/agent

demo-agent-local:
	uv run python scripts/run_agent_demo.py

# Does a live model stay inside the bounds? Needs ANTHROPIC_API_KEY in the environment or .env.
eval-live:
	uv run python scripts/eval_live_model.py --model $(MODEL) --runs $(RUNS)

fixtures:
	uv run python scripts/generate_fixture.py

demo-deterministic:
	uv run python scripts/run_deterministic_demo.py

# docs/architecture.png, rendered from the SVG at 2x. Chrome does the rendering because the diagram
# embeds AWS's own icon SVGs, and the lighter converters drop them. Regenerate when the SVG changes.
architecture-png:
	"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu \
	  --hide-scrollbars --default-background-color=FFFFFFFF --force-device-scale-factor=2 \
	  --window-size=1240,870 --screenshot=docs/architecture.png \
	  "file://$$(pwd)/docs/architecture.svg"
	@echo "wrote docs/architecture.png"


spike-interrupt-file:
	uv run python spikes/interrupt_resume_file.py start
	uv run python spikes/interrupt_resume_file.py resume

build-wheel:
	rm -rf deploy/biointakeruntime/app/biointake/wheels && uv build --wheel -o deploy/biointakeruntime/app/biointake/wheels

deploy-runtime: build-wheel
	cd deploy/biointakeruntime && AWS_PROFILE=$(PROFILE) npx -y @aws/agentcore deploy --yes

aws-provision:
	uv run python scripts/aws_provision.py --profile $(PROFILE) --region $(REGION)

e2e-local:
	uv run python scripts/e2e_flow.py --backend memory --invoker local

e2e-aws:
	uv run python scripts/e2e_flow.py --backend aws --invoker local --profile $(PROFILE) --region $(REGION) --table biointake-demo --bucket biointake-demo-$(ACCOUNT)

e2e-agentcore:
	uv run python scripts/e2e_flow.py --backend aws --invoker agentcore --profile $(PROFILE) --region $(REGION) --table biointake-demo --bucket biointake-demo-$(ACCOUNT) --runtime-arn $(RUNTIME_ARN)

api-dev:
	BIOINTAKE_BACKEND=memory BIOINTAKE_INVOKER=local uv run uvicorn biointake.api.app:create_app --factory --port 8000 --reload

deploy-api:
	uv run python scripts/deploy_apprunner.py --profile $(PROFILE) --region $(REGION) --secret-users $(USERS_SECRET)

aws-readiness:
	uv run python scripts/aws_readiness.py --profile $(PROFILE) --region $(REGION)

check: lint typecheck test
