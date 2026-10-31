import os
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta
from threading import Lock

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import HTTPException

from .db import now

hasher = PasswordHasher()


def signing_key():
    key = os.getenv('JWT_SECRET', '')
    if len(key) < 32:
        raise RuntimeError('JWT_SECRET must contain at least 32 characters; generate one with secrets.token_hex(32)')
    return key


def token(user, key):
    return jwt.encode({'sub': user.id, 'role': user.role, 'iat': now(), 'exp': now() + timedelta(minutes=30), 'jti': secrets.token_hex(16)}, key, algorithm='HS256')


def verify_password(encoded, supplied):
    try:
        return hasher.verify(encoded, supplied)
    except VerificationError:
        return False


class RateLimiter:
    """Single-API-process limit. Use an ingress limiter before horizontal scaling."""
    def __init__(self):
        self.entries = defaultdict(deque)
        self.lock = Lock()

    def check(self, bucket, maximum, seconds=60):
        current = time.monotonic()
        with self.lock:
            if len(self.entries) > 10000:
                self.entries = defaultdict(deque, {k: v for k, v in self.entries.items() if v and v[-1] > current - seconds})
            entries = self.entries[bucket]
            while entries and entries[0] <= current - seconds:
                entries.popleft()
            if len(entries) >= maximum:
                raise HTTPException(429, detail='rate_limit_exceeded')
            entries.append(current)
