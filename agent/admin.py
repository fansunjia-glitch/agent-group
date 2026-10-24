"""Signed administrator messages carried by the ordinary topic transport.

Invitations and keys are supplied by the trusted local/SSH supervisor, never
discovered from a topic's display name or accepted from model tool arguments.
"""

import hashlib
import hmac
import json
import re


NOTICE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def sign_notice(envelope: dict, secret: str) -> str:
    """Return the HMAC of canonical UTF-8 JSON, excluding ``signature``."""
    if not isinstance(secret, str) or not secret:
        raise ValueError("administrator secret must be a non-empty string")
    payload = {key: value for key, value in envelope.items() if key != "signature"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def verify_notice(envelope: object, secret: str) -> bool:
    """Validate the envelope and signature before exposing its content."""
    if not isinstance(envelope, dict) or envelope.get("kind") != "admin.notice":
        return False
    if not isinstance(envelope.get("notice_id"), str) or not NOTICE_ID.fullmatch(envelope["notice_id"]):
        return False
    if not isinstance(envelope.get("content"), str) or not envelope["content"].strip():
        return False
    targets = envelope.get("targets")
    if not isinstance(targets, list) or not targets or not all(isinstance(item, str) and item for item in targets):
        return False
    signature = envelope.get("signature")
    if not isinstance(signature, str) or len(signature) != 64:
        return False
    try:
        return hmac.compare_digest(signature, sign_notice(envelope, secret))
    except (TypeError, ValueError):
        return False
