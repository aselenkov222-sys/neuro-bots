import subprocess
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from server.accounts import create_account, find_account_by_email, normalize_email
from server.db import create_db_engine
from server.models import accounts, sessions
from server.passwords import (
    PasswordNotAccepted,
    hash_password,
    hasher,
    password_accepted,
    verify_password,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
SIGNS = "!@#$%^&*()-_=+[]{}:;,.?"
ACCEPTED = "abcdef1!"
OTHER = "abcdef2!"


def _compose(*args: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "--project-directory",
            str(PROJECT_ROOT),
            *args,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _pwdlib_requirement() -> Requirement:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        dependencies = tomllib.load(project_file)["project"]["dependencies"]
    matches = [
        Requirement(dependency)
        for dependency in dependencies
        if Requirement(dependency).name == "pwdlib"
    ]
    assert len(matches) == 1
    return matches[0]


def test_pwdlib_argon2_is_pinned() -> None:
    requirement = _pwdlib_requirement()
    assert requirement.extras == {"argon2"}
    assert str(requirement.specifier) == "==0.4.0"
    assert version("pwdlib") == "0.4.0"
    assert len(hasher.hashers) == 1
    assert type(hasher.current_hasher) is Argon2Hasher


def test_accepted_password_hashes_with_argon2_and_verifies_only_itself() -> None:
    assert password_accepted(ACCEPTED)
    stored_hash = hash_password(ACCEPTED)
    assert Argon2Hasher.identify(stored_hash)
    assert ACCEPTED not in stored_hash
    assert verify_password(ACCEPTED, stored_hash) is True
    assert verify_password(OTHER, stored_hash) is False
    pwdlib_hasher = PasswordHash((Argon2Hasher(),))
    assert pwdlib_hasher.verify(ACCEPTED, stored_hash) is True
    assert pwdlib_hasher.verify(OTHER, stored_hash) is False


def test_passwords_outside_the_checkpoint_rules_are_rejected() -> None:
    rejected = (
        "abcde1!",
        "a" * 127 + "1!",
        "abcdefgh!",
        "abcdefgh1",
        "abcdef1~",
    )
    assert [len(password) for password in rejected] == [7, 129, 9, 9, 8]
    for password in rejected:
        assert password_accepted(password) is False
        with pytest.raises(PasswordNotAccepted) as error:
            hash_password(password)
        assert str(error.value) == ""
        assert password not in str(error.value)


def test_length_boundaries_and_every_checkpoint_sign_are_accepted() -> None:
    length_8 = "abcdef1!"
    length_128 = "a" * 126 + "1!"
    assert len(length_8) == 8
    assert len(length_128) == 128
    assert password_accepted(length_8) is True
    assert password_accepted(length_128) is True
    assert Argon2Hasher.identify(hash_password(length_8))
    assert Argon2Hasher.identify(hash_password(length_128))
    assert password_accepted("abcde1!~") is True
    for sign in SIGNS:
        assert password_accepted(f"abcdef1{sign}") is True


def test_email_edges_and_case_normalize_to_the_stored_form() -> None:
    assert normalize_email("  User Name@Example.COM  ") == "user name@example.com"
    assert normalize_email("  " + "A" * 254 + "  ") == "a" * 254


def test_normalized_email_is_stored_unique_and_found_from_raw_input() -> None:
    raw_email = "  User Name@Example.COM  "
    stored_email = "user name@example.com"
    down = _compose("down", "--remove-orphans", timeout=60)
    assert down.returncode == 0, down.stderr
    try:
        up = _compose("up", "-d", "--wait", "--wait-timeout", "180", timeout=240)
        assert up.returncode == 0, up.stderr + up.stdout
        upgrade = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert upgrade.returncode == 0, upgrade.stderr + upgrade.stdout

        engine = create_db_engine()
        try:
            with engine.begin() as connection:
                create_account(connection, raw_email, ACCEPTED)
            with engine.connect() as connection:
                row = find_account_by_email(connection, raw_email)
                assert row is not None
                assert row["email"] == stored_email
                assert ACCEPTED not in row["password_hash"]
                assert Argon2Hasher.identify(row["password_hash"])
                assert verify_password(ACCEPTED, row["password_hash"]) is True
                assert verify_password(OTHER, row["password_hash"]) is False
                assert (
                    connection.execute(select(accounts.c.email)).scalars().all()
                    == [stored_email]
                )
                assert connection.execute(select(sessions.c.id)).scalars().all() == []
            with pytest.raises(ValueError, match="254"):
                with engine.begin() as connection:
                    create_account(connection, "A" * 255, ACCEPTED)
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    create_account(connection, "USER name@example.com", ACCEPTED)
            with engine.connect() as connection:
                assert (
                    connection.execute(select(accounts.c.email)).scalars().all()
                    == [stored_email]
                )
        finally:
            engine.dispose()
    finally:
        _compose("down", "--remove-orphans", timeout=60)
