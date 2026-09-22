import base64
import json
import time

from anymodel_subagents.redact import redact, redact_deep


def test_redact_no_secrets_is_noop():
    text = "hello world, nothing sensitive here"
    assert redact(text, []) == text
    assert redact(text) == text


def test_redact_empty_string():
    assert redact("") == ""


def test_redact_live_key_value():
    key = "my-super-secret-value-12345"
    text = f"Authorization: Bearer {key} was sent"
    out = redact(text, [key])
    assert key not in out
    assert "[REDACTED]" in out


def test_redact_openrouter_key_pattern():
    text = "leaked key: sk-or-v1-abcdefghijklmnopqrstuvwxyz0123456789"
    out = redact(text)
    assert "sk-or-v1-" not in out
    assert "[REDACTED]" in out


def test_redact_generic_sk_pattern():
    text = "token=sk-abcdefghijklmnopqrstuvwx"
    out = redact(text)
    assert "sk-abcdefghijklmnopqrstuvwx" not in out


def test_redact_github_token_pattern():
    text = "ghp_abcdefghijklmnopqrstuvwxyz012345"
    out = redact(text)
    assert "ghp_" not in out
    assert "[REDACTED]" in out


def test_redact_aws_key_pattern():
    text = "AKIA1234567890ABCDEF is an aws access key"
    out = redact(text)
    assert "AKIA1234567890ABCDEF" not in out
    assert "[REDACTED]" in out


def test_redact_multiple_occurrences():
    key = "sekret"
    text = f"{key} appears twice: {key}"
    out = redact(text, [key])
    assert out.count("[REDACTED]") == 2


def test_redact_sk_ant_pattern():
    text = "key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz_ABCDEFG-1234"
    out = redact(text)
    assert "sk-ant-" not in out
    assert "[REDACTED]" in out


def test_redact_github_pat_pattern():
    text = "token github_pat_11AAAAAAA0abcdefghijklmnop_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefgh"
    out = redact(text)
    assert "github_pat_" not in out
    assert "[REDACTED]" in out


def test_redact_slack_token_patterns():
    for prefix in ("xoxb", "xoxa", "xoxp", "xoxr", "xoxs"):
        text = f"slack token {prefix}-1234567890-1234567890123-abcdefghijklmnopqrstuvwx"
        out = redact(text)
        assert prefix not in out
        assert "[REDACTED]" in out


def test_redact_google_api_key_pattern():
    text = "GOOGLE_API_KEY=AIzaSyD-abcdefghijklmnopqrstuvwxyz012345"
    out = redact(text)
    assert "AIzaSyD" not in out
    assert "[REDACTED]" in out


def test_redact_private_key_block():
    text = (
        "before\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEAsomefakekeydata1234567890==\n"
        "-----END RSA PRIVATE KEY-----\n"
        "after"
    )
    out = redact(text)
    assert "MIIEpAIBAAKCAQEA" not in out
    assert "[REDACTED]" in out
    assert "before" in out
    assert "after" in out


def test_redact_openssh_private_key_block():
    text = "-----BEGIN OPENSSH PRIVATE KEY-----\nabcdef123456\n-----END OPENSSH PRIVATE KEY-----"
    out = redact(text)
    assert "abcdef123456" not in out
    assert "[REDACTED]" in out


def test_redact_base64_form_of_live_secret():
    key = "sk-or-v1-abcdefghijklmnopqrstuvwxyz0123456789"
    encoded = base64.b64encode(key.encode()).decode()
    text = f"found this encoded blob: {encoded}"
    out = redact(text, [key])
    assert encoded not in out
    assert "[REDACTED]" in out


def test_redact_json_escaped_form_of_live_secret():
    key = 'my"quoted\\secret-value'
    escaped = json.dumps(key)[1:-1]
    text = f'{{"token": "{escaped}"}}'
    out = redact(text, [key])
    assert escaped not in out
    assert "[REDACTED]" in out


def test_redact_deep_walks_nested_structures():
    key = "topsecretvalue12345"
    obj = {
        "a": key,
        "b": [key, {"c": key}],
        "d": ("nested", key),
        "e": 42,
        "f": None,
    }
    out = redact_deep(obj, [key])
    assert out["a"] == "[REDACTED]"
    assert out["b"][0] == "[REDACTED]"
    assert out["b"][1]["c"] == "[REDACTED]"
    assert out["d"][1] == "[REDACTED]"
    assert out["e"] == 42
    assert out["f"] is None


def test_redact_does_not_flag_git_sha():
    text = "Fixed in commit a1b2c3d4e5f678901234567890abcdef12345678, see also 0123456789abcdef0123456789abcdef01234567."
    out = redact(text)
    assert out == text


def test_redact_does_not_flag_ordinary_prose_and_code():
    text = (
        "def compute_risk_score(user):\n"
        "    # risk-assessment-2024-01-01-final-report-document\n"
        "    return user.desk_number * 2  # not a secret, just a desk-check\n"
    )
    out = redact(text)
    assert out == text


def test_redact_no_secrets_list_still_applies_key_patterns():
    text = "leaked: ghp_abcdefghijklmnopqrstuvwxyz012345"
    assert redact(text) != text


def test_pem_redaction_is_fast_on_begin_marker_spam():
    # Worker-controlled text reaches redact on the event loop. A BEGIN...END regex was
    # quadratic unbounded (~10 s for 2 MB) and still ~5 s/MB with a bounded body.
    hostile = "-----BEGIN PRIVATE KEY-----\n" * 150_000  # ~4 MB
    for text in (hostile, hostile + "-----END PRIVATE KEY-----"):
        start = time.monotonic()
        redact(text, [])
        assert time.monotonic() - start < 1.0


def test_pem_block_pairs_with_the_first_following_end():
    a = "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----"
    b = (
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nBBBB\n"
        "-----END ENCRYPTED PRIVATE KEY-----"
    )
    out = redact(f"x {a} y {b} z -----BEGIN PRIVATE KEY----- unterminated", [])
    assert out == "x [REDACTED] y [REDACTED] z -----BEGIN PRIVATE KEY----- unterminated"


def test_pem_begin_spam_before_a_real_block_still_redacts_it():
    spam = "-----BEGIN PRIVATE KEY----- " * 3
    out = redact(f"{spam}\nSECRETBODY\n-----END PRIVATE KEY----- tail", [])
    assert "SECRETBODY" not in out
    assert out.endswith(" tail")


def test_pem_markers_too_far_apart_are_not_a_key_block():
    text = "-----BEGIN PRIVATE KEY-----" + "x" * 20_000 + "-----END PRIVATE KEY-----"
    assert redact(text, []) == text


def test_pem_block_of_realistic_size_is_still_redacted():
    body = "\n".join("Q" * 64 for _ in range(100))  # ~6.5 KB, larger than an 8192-bit RSA key
    pem = f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"
    out = redact(f"before {pem} after", [])
    assert "QQQQ" not in out
    assert out.startswith("before ") and out.endswith(" after")


def test_pem_body_length_boundary():
    def block(n: int) -> str:
        return "-----BEGIN PRIVATE KEY-----" + "k" * n + "-----END PRIVATE KEY-----"

    assert redact(block(16_384), []) == "[REDACTED]"
    assert redact(block(16_385), []) == block(16_385)
