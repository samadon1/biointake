"""AgentCore entrypoint shim, the real app lives in the packaged `biointake` wheel."""

from biointake.agent.agentcore_app import app  # noqa: F401

if __name__ == "__main__":
    app.run()
