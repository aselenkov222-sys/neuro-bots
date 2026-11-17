import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _project_table() -> dict:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        return tomllib.load(project_file)["project"]


def _dependency_name(requirement: str) -> str:
    name = requirement.split(";", 1)[0].strip()
    name = name.split("[", 1)[0]
    for separator in (">", "<", "=", "!", "~", " "):
        name = name.split(separator, 1)[0]
    return name.strip().lower()


def test_requires_python_is_3_15_range() -> None:
    assert _project_table()["requires-python"] == ">=3.15,<3.16"


def test_fastapi_is_a_project_dependency() -> None:
    names = [
        _dependency_name(requirement)
        for requirement in _project_table()["dependencies"]
    ]
    assert "fastapi" in names


def test_fastapi_application_imports() -> None:
    from fastapi import FastAPI

    from server.app import app

    assert isinstance(app, FastAPI)
