# The BioIntake control API, as App Runner runs it.
#
# Two stages so the runtime image carries no build tooling and no source tree: the wheel is built
# once, installed into a virtualenv, and that virtualenv is all that ships.

# Pulled from AWS's public mirror of Docker Hub rather than Docker Hub itself: an anonymous pull
# from a CodeBuild worker shares an IP with everyone else's and gets 429 Too Many Requests.
FROM public.ecr.aws/docker/library/python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.28 /uv /usr/local/bin/uv
WORKDIR /src
# UV_PROJECT_ENVIRONMENT is how `uv sync` is told where the environment lives. Setting VIRTUAL_ENV
# instead does not work: sync writes to the project's own .venv and the image ends up with the
# package installed and none of its dependencies.
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv

# Dependencies resolve from the lockfile before the source is copied, so editing code does not
# re-resolve the world on every build.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY fixtures ./fixtures
# --no-editable matters: without it the project is installed as a .pth pointing back at /src, which
# does not exist in the runtime stage, and the image imports nothing.
RUN uv sync --frozen --no-dev --no-editable

FROM public.ecr.aws/docker/library/python:3.12-slim AS runtime
# App Runner runs the container as whatever the image says; nothing here needs root.
RUN useradd --create-home --uid 10001 biointake
COPY --from=build --chown=biointake:biointake /opt/venv /opt/venv
COPY --from=build --chown=biointake:biointake /src/fixtures /app/fixtures
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BIOINTAKE_FIXTURE_DIR=/app/fixtures/shipment_001
USER biointake
WORKDIR /app

# App Runner's default port. /health answers only once the repository is reachable.
EXPOSE 8000
CMD ["uvicorn", "biointake.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
