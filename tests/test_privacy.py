"""The PII boundary: redact() must catch what a real repo actually leaks.

Every credential-shaped literal below is fabricated for this test — a value in
the AKIA/ASIA key-ID format and the base64 secret-key shape, neither ever
issued by AWS or any provider. The point is to match the *shape* real leaks
take, never to reuse a real one.
"""

from __future__ import annotations

from prflagger.privacy import redact


def test_redacts_email_in_commit_trailer() -> None:
    text = "Co-authored-by: Jane Doe <jane.doe@example.com>"
    out = redact(text)
    assert "jane.doe@example.com" not in out
    assert "[REDACTED_EMAIL]" in out
    assert "Jane Doe" in out  # a name alone is not PII this boundary claims to catch


def test_redacts_aws_access_key_id() -> None:
    # Fabricated key ID: correct AKIA + 16-char shape, never issued by AWS.
    text = "export AWS_ACCESS_KEY_ID=AKIAFAKEEXAMPLE12345"
    out = redact(text)
    assert "AKIAFAKEEXAMPLE12345" not in out
    assert "[REDACTED_AWS_KEY_ID]" in out


def test_redacts_aws_secret_access_key() -> None:
    # Fabricated secret: correct 40-char base64 shape, never issued by AWS.
    secret = "fAKEsecretVALUEfor4tests0nly1234567890AB"
    text = f"AWS_SECRET_ACCESS_KEY = {secret}"
    out = redact(text)
    assert secret not in out


def test_redacts_bedrock_bearer_token() -> None:
    # Fabricated bearer token: matches the AB-prefixed base64 shape Bedrock
    # issues, but is not a real one.
    token = "AB" + "fake0Example1Token2Value3Shape4Only5" * 2
    out = redact(f"AWS_BEARER_TOKEN_BEDROCK='{token}'")
    assert token not in out
    assert "[REDACTED_BEARER_TOKEN]" in out


def test_redacts_generic_key_value_secret() -> None:
    text = 'api_key: "sk-abcdefghijklmnopqrstuvwx"'
    out = redact(text)
    assert "sk-abcdefghijklmnopqrstuvwx" not in out
    assert "[REDACTED]" in out


def test_leaves_ordinary_code_untouched() -> None:
    diff = "def split_arg_string(value):\n    return shlex.split(value)\n"
    assert redact(diff) == diff


def test_leaves_short_hex_shas_untouched() -> None:
    # A git SHA (7-40 hex chars) must survive — it is evidence, not a secret,
    # and the four-field contract cites SHAs in `how_we_know`.
    text = "fixed in b92b0945b468ef36e68840a2cfbc339acae5866a"
    assert redact(text) == text


def test_idempotent() -> None:
    text = "contact jane@example.com about AKIAFAKEEXAMPLE12345"
    once = redact(text)
    assert redact(once) == once
