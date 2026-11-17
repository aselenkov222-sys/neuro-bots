from dataclasses import fields

from server.config import development_tariff


def test_development_tariff_has_limits_and_no_name_or_price() -> None:
    field_names = {field.name for field in fields(development_tariff)}

    assert development_tariff.model_requests == 500
    assert development_tariff.routines == 5
    assert field_names == {"model_requests", "routines"}
    assert "name" not in field_names
    assert "price" not in field_names
