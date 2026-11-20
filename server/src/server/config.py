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


def configured_tariff_name() -> str | None:
    configured = os.environ.get("NEURO_BOTS_TARIFF_NAME")
    if configured is None or configured == "":
        return None
    return configured


def configured_tariff_price() -> str | None:
    configured = os.environ.get("NEURO_BOTS_TARIFF_PRICE")
    if configured is None or configured == "":
        return None
    return configured


def payment_required() -> bool:
    return configured_tariff_name() is not None and configured_tariff_price() is not None
