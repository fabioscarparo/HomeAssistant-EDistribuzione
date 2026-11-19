"""Tests for redaction helpers (POD masking, URL stripping and secret scrubbing)."""
from __future__ import annotations

from custom_components.edistribuzione.redaction import (
    mask_pod,
    mask_pods_in_text,
    redact_params,
    redact_preview,
    redact_secrets,
    redact_url,
)


def test_mask_pod_italian_format():
    assert mask_pod("IT001E12345678") == "IT001E****5678"
    assert mask_pod("it001e12345678") == "IT001E****5678"


def test_mask_pod_short_and_empty():
    assert mask_pod("") == ""
    assert mask_pod("IT001") == "****"
    assert mask_pod("CUSTOM12345") == "CU****2345"


def test_mask_pods_in_text():
    text = "Error on IT001E12345678 and it001e87654321"
    masked = mask_pods_in_text(text)
    assert "12345678" not in masked
    assert "87654321" not in masked
    assert "IT001E****5678" in masked
    assert "IT001E****4321" in masked


def test_redact_url_drops_query_and_fragment():
    assert (
        redact_url("https://private.e-distribuzione.it/frontdoor.jsp?sid=SECRET#frag=1")
        == "https://private.e-distribuzione.it/frontdoor.jsp"
    )
    assert redact_url("/relative/path?code=ABC") == "/relative/path"


def test_redact_secrets_scrubs_tokens_emails_and_cf():
    raw = (
        "Authorization: Bearer tok_12345; "
        "https://x.test/cb?sid=S1&code=C2&refresh_token=R3&access_token=A4 "
        "user=mario.rossi@example.com cf=RSSMRA80A01H501U"
    )
    cleaned = redact_secrets(raw)
    for secret in ("tok_12345", "S1", "C2", "R3", "A4", "mario.rossi@example.com", "RSSMRA80A01H501U"):
        assert secret not in cleaned
    assert "[REDACTED]" in cleaned
    assert "[REDACTED_EMAIL]" in cleaned
    assert "[REDACTED_CF]" in cleaned


def test_redact_params_masks_pod_only():
    params = {"pointofdelivery": "IT001E12345678", "magnitude": "A1"}
    safe = redact_params(params)
    assert safe == {"pointofdelivery": "IT001E****5678", "magnitude": "A1"}
    assert params["pointofdelivery"] == "IT001E12345678"


def test_redact_preview_truncates_and_scrubs():
    raw = "POD IT001E12345678 Bearer SUPERSECRET " + ("x" * 500)
    preview = redact_preview(raw, max_len=100)
    assert len(preview) <= 120
    assert "SUPERSECRET" not in preview
    assert "IT001E12345678" not in preview
