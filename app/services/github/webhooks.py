"""GitHub webhook signature verification."""

from __future__ import annotations

import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)


def verify_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature.

    The X-Hub-Signature-256 header contains 'sha256=<hex>' where <hex> is
    the HMAC-SHA256 digest of the raw request body signed with the webhook secret.
    """
    if not signature_header or not secret:
        return False

    # GitHub sends "sha256=<hex>"
    prefix = "sha256="
    if not signature_header.startswith(prefix):
        return False

    received_hex = signature_header[len(prefix) :]

    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, received_hex)
