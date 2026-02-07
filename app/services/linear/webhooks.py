"""Linear webhook signature verification and route handler."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

logger = logging.getLogger(__name__)


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Verify Linear webhook HMAC-SHA256 signature.

    The Linear-Signature header contains a hex-encoded HMAC-SHA256 digest
    of the raw request body, signed with the webhook secret.
    """
    if not signature or not secret:
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


def verify_timestamp(webhook_timestamp: int | None, max_age_seconds: int = 60) -> bool:
    """Verify the webhook timestamp is within acceptable range.

    webhook_timestamp is in UNIX milliseconds.
    """
    if webhook_timestamp is None:
        return True  # Lenient if not provided

    now_ms = int(time.time() * 1000)
    age_ms = abs(now_ms - webhook_timestamp)
    return age_ms <= max_age_seconds * 1000
