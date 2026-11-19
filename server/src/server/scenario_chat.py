_LEADS = (
    "сохранить правила в сценарий",
    "сохрани правила в сценарий",
    "сохранить задачу в сценарий",
    "сохрани задачу в сценарий",
    "сохранить в сценарий",
    "сохрани в сценарий",
    "сохранить правила",
    "сохрани правила",
)
_SEPARATORS = ":.—-;,."

PROPOSAL_CODE = "scenario_proposal"
CONFIRMED_CODE = "scenario_confirmed"


def proposed_scenario(text: str) -> str | None:
    stripped = text.strip()
    lowered = stripped.casefold()
    for lead in _LEADS:
        if not lowered.startswith(lead):
            continue
        if len(lowered) == len(lead):
            return None
        if lowered[len(lead)].isalnum() or lowered[len(lead)] == "_":
            return None
        body = stripped[len(lead) :].lstrip()
        if body[:1] in _SEPARATORS:
            body = body[1:].lstrip()
        if body == "":
            return None
        return body
    return None
