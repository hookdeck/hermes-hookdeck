"""Turning delivered bytes into a payload the agent can be handed.

Kept separate from the adapter because none of it needs Hermes, an HTTP
request, or any state — and because the encoding rules below are subtle enough
to deserve reading on their own.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any


class UndecodablePayload(ValueError):
    """The body is neither valid UTF-8 JSON nor form-encoded.

    Distinct from "the payload says something we don't like": nothing an
    operator changes will make this body parse, which is what lets the caller
    treat it as permanently failed rather than retryable.
    """


def decode(raw: bytes) -> str:
    """Decode a delivered body as strict UTF-8.

    Deliberately not left to ``json.loads``, which sniffs the encoding and is
    laxer than JSON itself in two ways that both surface far from here:

    * it decodes with ``surrogatepass``, so a CESU-8 lone surrogate parses
      cleanly, renders into a prompt cleanly, and then raises
      ``UnicodeEncodeError`` at the network boundary inside the agent run
    * it accepts UTF-16/32 via BOM sniffing, which RFC 8259 §8.1 forbids for
      JSON exchanged between systems

    A strict decode turns both into one honest rejection at ingress, where the
    event can still be inspected and replayed.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UndecodablePayload("body is not valid UTF-8") from exc


def parse(text: str) -> Any:
    """Parse decoded body text as JSON, falling back to form encoding."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        parsed = dict(urllib.parse.parse_qsl(text, strict_parsing=True))
    except ValueError as exc:
        raise UndecodablePayload("body is neither JSON nor form-encoded") from exc
    return parsed


def has_lone_surrogates(payload: Any) -> bool:
    """True when some string in *payload* cannot be encoded as UTF-8."""
    try:
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def replace_lone_surrogates(value: Any) -> Any:
    """Swap unpaired surrogates for U+FFFD, leaving everything else intact.

    ``"\\ud800"`` written as a JSON *escape* is pure ASCII on the wire, so it
    survives :func:`decode` — and RFC 8259 explicitly permits any ``\\uXXXX``
    escape, including an unpaired surrogate, so refusing it would mean refusing
    conforming JSON. Python nonetheless decodes it to a ``str`` that raises the
    moment anything encodes it, which for a webhook means somewhere inside the
    agent run, long after the ack.

    Replacing the character keeps the event flowing, matching what JavaScript
    runtimes do anyway. Callers are expected to say so rather than substitute
    silently: quietly altering payload text is the failure this module exists
    to prevent.
    """
    if isinstance(value, str):
        return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(value, dict):
        return {k: replace_lone_surrogates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_lone_surrogates(v) for v in value]
    return value


def dig(payload: Any, dotted: str) -> Any:
    """Follow a dotted path into nested dicts, or ``None`` if it doesn't exist."""
    value = payload
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value
