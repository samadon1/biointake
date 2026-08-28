"""Credentials for the lab staff in tests, supplied the way a deployment supplies them.

Tests sign in with a real bearer token rather than bypassing authentication, so the thing under
test is the same code path a coordinator uses. The tokens are fixed here only because the test
process is also the one configuring the lab; nothing reads them from the repository.
"""

from __future__ import annotations

STAFF: dict[str, tuple[str, str]] = {
    "coordinator-ama-asante": ("Ama Asante (receiving coordinator)", "COORDINATOR"),
    "pi-kwame-osei": ("Kwame Osei (principal investigator)", "PRINCIPAL_INVESTIGATOR"),
    "qa-efua-boateng": ("Efua Boateng (QA reviewer)", "QA_REVIEWER"),
    "control-plane": ("BioIntake control plane", "SYSTEM"),
}

TOKENS: dict[str, str] = {user_id: f"bit_test_{user_id}" for user_id in STAFF}

USERS_SPEC = ";".join(
    f"{user_id}|{display_name}|{role}|{TOKENS[user_id]}" for user_id, (display_name, role) in STAFF.items()
)


def headers(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[user_id]}"}
