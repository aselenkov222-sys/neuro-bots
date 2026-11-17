import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _project_table() -> dict:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        return tomllib.load(project_file)["project"]


def test_requires_python_is_3_15_range() -> None:
    assert _project_table()["requires-python"] == ">=3.15,<3.16"


def test_desktop_package_imports() -> None:
    import desktop

    assert desktop.__name__ == "desktop"
