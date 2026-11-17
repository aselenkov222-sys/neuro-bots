from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Tariff:
    model_requests: int
    routines: int


development_tariff = Tariff(model_requests=500, routines=5)
