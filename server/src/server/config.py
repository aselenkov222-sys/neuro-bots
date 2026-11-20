import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Tariff:
    model_requests: int
    routines: int


development_tariff = Tariff(model_requests=500, routines=5)


def configured_mail_sink() -> str | None:
    configured = os.environ.get("NEURO_BOTS_MAIL_SINK")
    if configured is None or configured == "":
        return None
    return configured
