"""A routine does not ask which vault record to use."""


def failure_reason(result: object) -> str | None:
    if not isinstance(result, dict):
        return None
    if result.get("login") != "choose":
        return None
    return "vault_choice"
