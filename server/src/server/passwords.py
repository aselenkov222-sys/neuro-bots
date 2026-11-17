from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

MINIMUM_LENGTH = 8
MAXIMUM_LENGTH = 128
DIGITS = "0123456789"
SIGNS = "!@#$%^&*()-_=+[]{}:;,.?"

hasher = PasswordHash((Argon2Hasher(),))


class PasswordNotAccepted(Exception):
    pass


def password_accepted(password: str) -> bool:
    if len(password) < MINIMUM_LENGTH or len(password) > MAXIMUM_LENGTH:
        return False
    if not any(character in DIGITS for character in password):
        return False
    return any(character in SIGNS for character in password)


def hash_password(password: str) -> str:
    if not password_accepted(password):
        raise PasswordNotAccepted
    return hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    return hasher.verify(password, stored_hash)
