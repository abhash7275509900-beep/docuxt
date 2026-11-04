"""Pure-Python RFC 7519 JWT implementation compatible with PyJWT."""
import base64
import hashlib
import hmac
import json
import time


class PyJWTError(Exception):
    pass


class InvalidTokenError(PyJWTError):
    pass


class DecodeError(InvalidTokenError):
    pass


class ExpiredSignatureError(InvalidTokenError):
    pass


class InvalidSignatureError(InvalidTokenError):
    pass


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    rem = len(s) % 4
    if rem:
        s += "=" * (4 - rem)
    return base64.urlsafe_b64decode(s.encode("ascii"))


def encode(payload: dict, key: str, algorithm: str = "HS256", headers: dict = None) -> str:
    if algorithm != "HS256":
        raise NotImplementedError(f"Unsupported algorithm: {algorithm}")
    hdr = {"typ": "JWT", "alg": algorithm}
    if headers:
        hdr.update(headers)
    header_b64 = _b64url_encode(json.dumps(hdr, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    sig = hmac.new(key_bytes, signing_input, hashlib.sha256).digest()
    return f"{header_b64}.{payload_b64}.{_b64url_encode(sig)}"


def decode(token: str, key: str, algorithms=None, options: dict = None) -> dict:
    if not isinstance(token, str) or token.count(".") != 2:
        raise DecodeError("Not enough segments")
    if algorithms and "HS256" not in algorithms:
        raise InvalidTokenError("Unsupported algorithm")
    header_b64, payload_b64, sig_b64 = token.split(".")
    try:
        header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        sig = _b64url_decode(sig_b64)
    except Exception as e:
        raise DecodeError(f"Invalid token encoding: {e}") from e

    if header.get("alg") != "HS256":
        raise InvalidTokenError("Invalid algorithm")

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    expected_sig = hmac.new(key_bytes, signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected_sig):
        raise InvalidSignatureError("Signature verification failed")

    verify_exp = True
    if options and options.get("verify_exp") is False:
        verify_exp = False
    if verify_exp and "exp" in payload:
        if time.time() > float(payload["exp"]):
            raise ExpiredSignatureError("Signature has expired")

    return payload
