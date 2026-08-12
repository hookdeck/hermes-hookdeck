"""Verification of Hookdeck's outbound signature.

Hookdeck verifies the *provider's* signature at the edge — Stripe's
``Stripe-Signature``, Shopify's ``X-Shopify-Hmac-Sha256``, Twilio's
``X-Twilio-Signature`` and ~140 others — and then signs its own delivery with a
single scheme. That is the whole point of the integration: Hermes implements one
verifier instead of one per provider.

The scheme is ``base64(HMAC-SHA256(raw_body, signing_secret))`` in the
``x-hookdeck-signature`` header. A second header ``x-hookdeck-signature-2``
carries the previous secret for a window after a secret roll, so both are
accepted and either matching is a pass.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Iterable, Mapping

from .constants import DEFAULT_HEADER_PREFIX, SIGNATURE, SIGNATURE_2, header_name


def compute_signature(body: bytes, secret: str) -> str:
    """Return the base64 HMAC-SHA256 digest Hookdeck would send for *body*."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _matches_any(expected: str, provided: Iterable[str]) -> bool:
    """Whether any candidate is *expected*, compared as bytes.

    Bytes rather than ``str`` because ``compare_digest`` refuses two ``str``
    arguments unless both are pure ASCII, and a candidate is a header value an
    unauthenticated sender controls outright. One non-ASCII byte in it used to
    raise ``TypeError`` straight out of the verification path, which aiohttp
    turned into a 500 — wrong twice over: it bypassed the
    ``EMITTED_STATUS_RETRYABLE`` guard every other response goes through, and
    500 sits inside the provisioned retry rule's ``500-599`` range, so a
    malformed request was retried rather than refused.

    Encoding cannot fail on either side: *expected* is base64, and ``replace``
    maps an undecodable candidate to bytes that simply do not match.
    """
    wanted = expected.encode("ascii")
    # compare_digest on every candidate rather than short-circuiting, so the
    # work done does not depend on which header happened to match.
    matched = False
    for candidate in provided:
        if candidate and hmac.compare_digest(wanted, candidate.encode("utf-8", "replace")):
            matched = True
    return matched


def verify_signature(
    headers: Mapping[str, str],
    body: bytes,
    secret: str,
    *,
    prefix: str = DEFAULT_HEADER_PREFIX,
) -> bool:
    """True when *headers* carry a valid Hookdeck signature for *body*.

    An absent secret is a hard failure, never a bypass: the caller decides
    whether an unauthenticated route is acceptable (see the loopback-only
    ``INSECURE_NO_AUTH`` path in the adapter) and must not reach here if so.
    """
    if not secret:
        return False

    lowered = {k.lower(): v for k, v in headers.items()}
    provided = [
        lowered.get(header_name(SIGNATURE, prefix), ""),
        lowered.get(header_name(SIGNATURE_2, prefix), ""),
    ]
    if not any(provided):
        return False

    return _matches_any(compute_signature(body, secret), provided)
