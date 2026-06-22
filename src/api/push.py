"""VAPID web push support.

Implements RFC 8292 (VAPID) push notification delivery using the
cryptography library directly (no pywebpush dependency).

Environment variables consumed:
  PUSH_VAPID_PRIVATE_KEY — base64url-encoded raw EC private key bytes (P-256)
  PUSH_VAPID_PUBLIC_KEY  — base64url-encoded uncompressed EC public key (P-256)

If either is absent, push is silently disabled (no error on startup).

Push categories (WEBUI.md §3.2):
  - escalation  — entity received LABEL_NEEDS_HUMAN
  - promotion   — new LABEL_AWAITING_PROMOTION triage item
  - approval    — PR transitioned to APPROVED / LABEL_READY
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time

import httpx

from src.db.push_store import PushStorePort

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# VAPID key loading
# ---------------------------------------------------------------------------

PushPayload = dict[str, object]
"""Type alias for push notification payload dicts."""


def _b64url_decode(s: str) -> bytes:
    """Decode a base64url string (with or without padding)."""
    pad = 4 - len(s) % 4
    if pad != 4:
        s = s + "=" * pad
    return base64.urlsafe_b64decode(s)


def _b64url_encode(b: bytes) -> str:
    """Encode bytes to unpadded base64url."""
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _load_vapid_private_key() -> object | None:
    """Load the VAPID private key from the environment.

    Returns a cryptography EC private key object, or None if not configured.
    """
    raw = os.environ.get("PUSH_VAPID_PRIVATE_KEY", "")
    if not raw:
        return None
    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.asymmetric.ec import (
            SECP256R1,
            derive_private_key,
        )

        key_bytes = _b64url_decode(raw)
        private_value = int.from_bytes(key_bytes, "big")
        key = derive_private_key(private_value, SECP256R1(), default_backend())
        return key
    except Exception as exc:
        log.warning("Failed to load PUSH_VAPID_PRIVATE_KEY: %s", exc)
        return None


def get_vapid_public_key() -> str | None:
    """Return the VAPID public key from the environment (unmodified)."""
    return os.environ.get("PUSH_VAPID_PUBLIC_KEY") or None


def push_enabled() -> bool:
    """Return True when both VAPID keys are present in the environment."""
    return bool(
        os.environ.get("PUSH_VAPID_PRIVATE_KEY") and os.environ.get("PUSH_VAPID_PUBLIC_KEY")
    )


# ---------------------------------------------------------------------------
# VAPID JWT (application server authentication)
# ---------------------------------------------------------------------------


def _make_vapid_jwt(audience: str, subject: str = "mailto:operator@localhost") -> str:
    """Build and sign a VAPID JWT for the push service audience.

    Args:
        audience: The origin of the push service endpoint (e.g. "https://fcm.googleapis.com").
        subject: The "sub" claim — contact info for the application server operator.

    Returns:
        Compact-serialized JWT string.

    Raises:
        RuntimeError: PUSH_VAPID_PRIVATE_KEY is not configured.
    """
    private_key = _load_vapid_private_key()
    if private_key is None:
        raise RuntimeError("PUSH_VAPID_PRIVATE_KEY is not configured")

    from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
    from cryptography.hazmat.primitives.hashes import SHA256

    now = int(time.time())
    header = {"typ": "JWT", "alg": "ES256"}
    payload = {
        "aud": audience,
        "exp": now + 3600,
        "sub": subject,
    }

    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()

    from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

    assert isinstance(private_key, EllipticCurvePrivateKey)
    der_sig = private_key.sign(signing_input, ECDSA(SHA256()))

    # Convert DER to raw r||s (64 bytes) for JWT
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    r, s = decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    sig_b64 = _b64url_encode(raw_sig)

    return f"{header_b64}.{payload_b64}.{sig_b64}"


# ---------------------------------------------------------------------------
# Push delivery
# ---------------------------------------------------------------------------


def _get_audience(endpoint: str) -> str:
    """Extract the origin (audience) from a push endpoint URL."""
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    return f"{parsed.scheme}://{parsed.netloc}"


async def send_push(
    subscription: dict[str, object],
    payload: PushPayload,
) -> bool:
    """Send a web push notification to a single subscription.

    Uses the VAPID Application Server Key for authentication (RFC 8292).
    The payload is sent as plain JSON (no content-encryption — the
    notification body is informational; sensitive data is never included).

    Args:
        subscription: Dict with keys "endpoint" and "keys" ({"p256dh": ..., "auth": ...}).
        payload: JSON-serializable notification payload.

    Returns:
        True on success (2xx from push service), False otherwise.
    """
    if not push_enabled():
        log.debug("Push not configured; skipping notification")
        return False

    endpoint = str(subscription["endpoint"])
    audience = _get_audience(endpoint)

    try:
        vapid_jwt = _make_vapid_jwt(audience)
        vapid_public_key = get_vapid_public_key() or ""
        body = json.dumps(payload).encode()

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                endpoint,
                content=body,
                headers={
                    "Authorization": f"vapid t={vapid_jwt},k={vapid_public_key}",
                    "Content-Type": "application/json",
                    "TTL": "86400",
                },
            )
        if resp.status_code in (200, 201, 202):
            return True
        if resp.status_code == 410:
            # Subscription has expired; caller should remove it
            log.info("Push subscription gone (410): %s", endpoint[:60])
        else:
            log.warning("Push delivery failed %s: %s", resp.status_code, endpoint[:60])
        return False
    except Exception as exc:
        log.warning("Push delivery error for %s: %s", endpoint[:60], exc)
        return False


async def broadcast_push(
    push_store: PushStorePort,
    payload: PushPayload,
) -> int:
    """Broadcast a push notification to all registered subscriptions.

    Args:
        push_store: The push subscription store.
        payload: Notification payload.

    Returns:
        Number of subscriptions notified successfully.
    """
    subscriptions = await push_store.all_subscriptions()
    sent = 0
    for sub in subscriptions:
        ok = await send_push(sub, payload)
        if ok:
            sent += 1
    return sent
