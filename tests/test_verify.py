from __future__ import annotations

from hookdeck.verify import compute_signature, verify_signature

SECRET = "whsec_test_secret"
BODY = b'{"type":"charge.succeeded","id":"evt_1"}'


def headers(**kwargs) -> dict:
    return {"content-type": "application/json", **kwargs}


def test_accepts_a_valid_signature():
    signed = headers(**{"x-hookdeck-signature": compute_signature(BODY, SECRET)})
    assert verify_signature(signed, BODY, SECRET)


def test_header_lookup_is_case_insensitive():
    signed = headers(**{"X-Hookdeck-Signature": compute_signature(BODY, SECRET)})
    assert verify_signature(signed, BODY, SECRET)


def test_accepts_the_rolled_secret_in_signature_2():
    # After a secret roll Hookdeck sends the old secret's digest in the second
    # header for a window; rejecting it would drop live traffic mid-rotation.
    signed = headers(
        **{
            "x-hookdeck-signature": compute_signature(BODY, "the-new-secret"),
            "x-hookdeck-signature-2": compute_signature(BODY, SECRET),
        }
    )
    assert verify_signature(signed, BODY, SECRET)


def test_rejects_a_tampered_body():
    signed = headers(**{"x-hookdeck-signature": compute_signature(BODY, SECRET)})
    assert not verify_signature(signed, BODY + b" ", SECRET)


def test_rejects_the_wrong_secret():
    signed = headers(**{"x-hookdeck-signature": compute_signature(BODY, "other")})
    assert not verify_signature(signed, BODY, SECRET)


def test_rejects_a_missing_signature_header():
    assert not verify_signature(headers(), BODY, SECRET)


def test_an_empty_secret_never_passes():
    signed = headers(**{"x-hookdeck-signature": compute_signature(BODY, "")})
    assert not verify_signature(signed, BODY, "")


def test_a_non_ascii_signature_is_rejected_not_raised():
    # The sender controls this header outright. compare_digest refuses two
    # non-ASCII strs, and letting that TypeError escape turned a forged
    # signature into a 500 — which the retry rule then treats as retryable.
    assert not verify_signature(headers(**{"x-hookdeck-signature": "abcé"}), BODY, SECRET)


def test_a_lone_surrogate_signature_is_rejected_not_raised():
    # Not reachable through aiohttp today, but the guard is one encode call and
    # the failure mode it prevents is a 500 on the security-critical path.
    signed = headers(**{"x-hookdeck-signature": "\ud800bad"})
    assert not verify_signature(signed, BODY, SECRET)


def test_honours_a_custom_header_prefix():
    signed = headers(**{"x-acme-signature": compute_signature(BODY, SECRET)})
    assert verify_signature(signed, BODY, SECRET, prefix="x-acme")
    assert not verify_signature(signed, BODY, SECRET)
