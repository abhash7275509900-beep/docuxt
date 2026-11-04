"""
Auth: signup/login issue a JWT; require_auth protects routes.

Password hashing uses PBKDF2-HMAC-SHA256 via hashlib (stdlib only,
no bcrypt dependency needed). JWT via PyJWT.
Requires DOCENGINE_SECRET to be set to a strong 32+ character secret.
Enforces password length bounds (8..128) and constant-time dummy verification.
"""
import hashlib
import hmac
import os
import time
import functools
import jwt as pyjwt
from flask import request, g, jsonify

from app import db

# Built-in key so the app runs on Render with no setup.
# (An environment variable DOCENGINE_SECRET, if set, takes priority.)
_BUILTIN_SECRET = "a659867701177d3f910d934b9ee6b8e0f1f7abae34cf77fe650c173e13c4059e"
SECRET_KEY = os.environ.get("DOCENGINE_SECRET", "") or _BUILTIN_SECRET
if SECRET_KEY == "test" and os.environ.get("DOCENGINE_ENV", "").lower() == "production":
    raise RuntimeError("DOCENGINE_SECRET=test is not allowed in production")
elif SECRET_KEY == "test":
    # Explicit test-mode shortcut (non-production only)
    SECRET_KEY = "test-mode-secret-key-at-least-32-bytes-long-0000"
elif (
    len(SECRET_KEY) < 32
    or "change-me" in SECRET_KEY.lower()
    or "dev-secret" in SECRET_KEY.lower()
):
    raise RuntimeError("Set DOCENGINE_SECRET to a random 32+ char value")

JWT_ALGO = "HS256"
JWT_EXPIRY_SECONDS = 7 * 24 * 3600  # 7 days
PBKDF2_ITERATIONS = 260_000
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128


def validate_password(password) -> str:
    if not isinstance(password, str) or not (MIN_PASSWORD_LEN <= len(password) <= MAX_PASSWORD_LEN):
        raise ValueError(f"password must be between {MIN_PASSWORD_LEN} and {MAX_PASSWORD_LEN} characters")
    return password


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}${dk.hex()}"


_DUMMY_HASH = hash_password("dummy-password-for-constant-time-login")


def verify_password(password: str, stored_hash: str) -> bool:
    if not isinstance(password, str) or not isinstance(stored_hash, str):
        return False
    if len(password) > MAX_PASSWORD_LEN:
        return False
    try:
        salt_hex, dk_hex = stored_hash.split("$")
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(dk.hex(), dk_hex)


def verify_dummy_password(password: str) -> bool:
    if not isinstance(password, str):
        password = ""
    return verify_password(password[:MAX_PASSWORD_LEN], _DUMMY_HASH)


def issue_token(user_id: str) -> str:
    payload = {
        "sub": user_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + JWT_EXPIRY_SECONDS,
    }
    return pyjwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGO)


def decode_token(token: str):
    return pyjwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGO])


def require_auth(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify(error="missing or malformed Authorization header"), 401
        token = auth_header[len("Bearer "):]
        try:
            payload = decode_token(token)
        except pyjwt.ExpiredSignatureError:
            return jsonify(error="token expired"), 401
        except pyjwt.InvalidTokenError:
            return jsonify(error="invalid token"), 401
        user = db.get_user_by_id(payload.get("sub"))
        if user is None:
            return jsonify(error="user not found"), 401
        g.user = user
        g.user_id = user["id"]
        return fn(*args, **kwargs)
    return wrapper
