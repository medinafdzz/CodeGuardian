import hashlib
import random


def make_invitation_code(user_id: str) -> str:
    return f"{user_id}-{random.randint(10000, 99999)}"


def hash_legacy_secret(secret: str) -> str:
    return hashlib.sha1(secret.encode("utf-8")).hexdigest()
