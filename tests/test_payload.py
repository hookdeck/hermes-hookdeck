from __future__ import annotations

import pytest

from hookdeck import payload


def test_valid_utf8_decodes_including_multibyte():
    assert payload.decode('{"a": "café 日本 🪝"}'.encode()) == '{"a": "café 日本 🪝"}'


def test_invalid_utf8_is_rejected():
    with pytest.raises(payload.UndecodablePayload):
        payload.decode(b'{"a": "caf\xe9"}')


def test_utf16_is_rejected_even_though_json_would_accept_it():
    # RFC 8259 §8.1 requires UTF-8 for JSON exchanged between systems, but
    # json.loads sniffs the BOM and takes UTF-16 happily.
    with pytest.raises(payload.UndecodablePayload):
        payload.decode('{"a": "ok"}'.encode("utf-16"))


def test_json_and_form_bodies_both_parse():
    assert payload.parse('{"a": 1}') == {"a": 1}
    assert payload.parse("kind=ping&who=bob") == {"kind": "ping", "who": "bob"}


def test_a_body_that_is_neither_is_rejected():
    with pytest.raises(payload.UndecodablePayload):
        payload.parse("this is not json and not a form")


def test_an_escaped_lone_surrogate_survives_decoding_and_must_be_replaced():
    # Pure ASCII on the wire and valid JSON, so decode() cannot catch it — but
    # Python yields a str that raises when anything encodes it, which for a
    # webhook is inside the agent run, long after the ack.
    parsed = payload.parse(payload.decode(b'{"a": "hi \\ud800 there"}'))
    assert payload.has_lone_surrogates(parsed)

    cleaned = payload.replace_lone_surrogates(parsed)
    assert cleaned == {"a": "hi � there"}
    cleaned["a"].encode("utf-8")  # the whole point: now encodable


def test_a_valid_surrogate_pair_is_left_alone():
    parsed = payload.parse('{"a": "\\ud83e\\udd1d"}')
    assert not payload.has_lone_surrogates(parsed)
    assert payload.replace_lone_surrogates(parsed) == {"a": "\U0001f91d"}


def test_replacement_reaches_nested_structures():
    parsed = payload.parse('{"a": {"b": ["ok", "x\\ud800"]}}')
    assert payload.replace_lone_surrogates(parsed) == {"a": {"b": ["ok", "x�"]}}


def test_dig_follows_dotted_paths_and_gives_up_quietly():
    body = {"data": {"object": {"id": "ch_1"}}, "n": 1}
    assert payload.dig(body, "data.object.id") == "ch_1"
    assert payload.dig(body, "data.missing.id") is None
    assert payload.dig(body, "n.deeper") is None
