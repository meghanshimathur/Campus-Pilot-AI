"""Central sensitive-information detection and redaction (Assignment 2 foundation)."""
import copy
import time

import pytest

from campuspilot.secrets_guard import (Finding, contains_secret, find_secrets, redact_secrets, sanitize_details,
                                       screen_sensitive)

# (message, the secret value that must NOT survive redaction)
SECRET_MESSAGES = [
    ("my OTP is 482913", "482913"),
    ("the otp is 4821", "4821"),
    ("my one time code is 4821", "4821"),                  # not caught by the original label-only screen
    ("verification code: 4821", "4821"),                   # not caught by the original screen
    ("my pwd is hunter2", "hunter2"),                      # not caught by the original screen
    ("password is hunter2", "hunter2"),
    ("my password: Sunshine#7", "Sunshine#7"),
    ("pin 1234", "1234"),
    ("the code I got is 7391", "7391"),                    # not caught by the original screen
    ("my passcode is open sesame", "sesame"),              # a passphrase of several words is masked whole
    ("set my password to Tr0ub4dor&3", "Tr0ub4dor&3"),
    ("OTP=558201 thanks", "558201"),
    ("here is my api key AIzaSyA1234567890abcdefghijklmn", "AIzaSyA1234567890abcdefghijklmn"),
    ("Bearer abcdef1234567890abcdef1234567890", "abcdef1234567890abcdef1234567890"),
    ("card 4111 1111 1111 1111", "4111 1111 1111 1111"),
    ("aadhaar 1234 5678 9012", "1234 5678 9012"),
    ("PAN ABCDE1234F", "ABCDE1234F"),
    ("pay to rahul@okicici", "rahul@okicici"),
    ("IFSC HDFC0001234", "HDFC0001234"),
    ("a3f5c9d1e7b24680a3f5c9d1e7b24680 is the token", "a3f5c9d1e7b24680a3f5c9d1e7b24680"),
    ("the token is abc123xyz", "abc123xyz"),
    ("api secret is x7Kp9Q2z", "x7Kp9Q2z"),
    ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END PRIVATE KEY-----", "MIIEvQIBADANBgkq"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijkl", "eyJhbGciOiJIUzI1NiJ9"),
]

BENIGN_MESSAGES = [
    "How do I reset my password?", "I forgot my password and cannot log in", "What is the password policy?",
    "I like to code in Python", "My PIN code is 411001", "course code is DS201", "Please call me Dev from now on.",
    "the secret to good grades is practice", "Can I get a token for the bus?", "Is my OTP required for login?",
    "The OTP is required", "my password is expired", "I have a question about the password reset email",
    "What is my current fee status?", "The payment portal failed. Can I pay on the next working day?",
    "Where can I park my car on campus?", "When are Dr. Kavita Rao's office hours?", "I prefer studying in the morning",
    "Hello! What can you help me with?", "Please raise a support request: my fee payment failed twice.",
]


@pytest.mark.parametrize("message,secret", SECRET_MESSAGES)
def test_secret_values_are_found_and_removed_from_the_redacted_text(message, secret):
    result = redact_secrets(message)
    assert result.findings, f"not detected: {message!r}"
    assert secret not in result.text
    assert "[REDACTED:" in result.text
    assert contains_secret(message)
    assert screen_sensitive(message) is not None            # the coarse screen agrees


@pytest.mark.parametrize("message", BENIGN_MESSAGES)
def test_ordinary_messages_are_not_flagged_as_secrets(message):
    assert find_secrets(message) == []
    assert redact_secrets(message).text == message


def test_the_phrasings_the_original_screen_missed_are_now_caught():
    # measured before Assignment 2: these were stored in conversation history and accepted into long-term memory
    for message in ("my one time code is 4821", "verification code: 4821", "my pwd is hunter2", "the code I got is 7391"):
        assert screen_sensitive(message) is not None


def test_redaction_keeps_the_rest_of_the_sentence_readable():
    assert redact_secrets("my OTP is 482913, please confirm").text == "my OTP is [REDACTED:otp], please confirm"
    assert redact_secrets("OTP=558201 thanks").text == "OTP=[REDACTED:otp] thanks"


def test_redaction_is_idempotent_and_handles_multiple_secrets():
    text = "otp 4821 and password: hunter2 and card 4111 1111 1111 1111"
    once = redact_secrets(text)
    assert len(once.findings) == 3
    for secret in ("4821", "hunter2", "4111"):
        assert secret not in once.text
    assert redact_secrets(once.text).text == once.text


def test_findings_point_at_exactly_the_secret_value():
    text = "my password is hunter2 please help"
    (finding,) = find_secrets(text)
    assert isinstance(finding, Finding) and text[finding.start:finding.end] == "hunter2" and finding.category == "password"


def test_following_words_are_treated_as_part_of_a_passphrase_until_a_stop_word():
    """Deliberately conservative: it cannot tell 'open sesame' from 'hunter2 ok', so both words are masked."""
    text = "my password is hunter2 ok"
    (finding,) = find_secrets(text)
    assert text[finding.start:finding.end] == "hunter2 ok"


def test_bare_reply_is_only_a_secret_when_the_caller_expects_one():
    assert find_secrets("4821") == []                       # without context a bare number is just a number
    for reply, secret in (("4821", "4821"), ("it is hunter2", "hunter2"), ("Sunshine#7", "Sunshine#7"), ("482913", "482913")):
        result = redact_secrets(reply, expect_secret=True)
        assert secret not in result.text and result.findings


def test_luhn_filter_keeps_ordinary_long_numbers_out_of_card_detection():
    assert not any(f.category == "card_number" for f in find_secrets("reference 1234 5678 9012 3456"))
    assert any(f.category == "card_number" for f in find_secrets("4111111111111111"))


def test_random_hex_token_is_found_but_a_repeated_word_is_not():
    assert contains_secret("token a3f5c9d1e7b24680a3f5c9d1e7b24680")
    assert not contains_secret("a" * 5000)
    assert not contains_secret("deadbeef" * 8)               # too repetitive to be a random token


def test_non_string_input_is_safe():
    for bad in (None, 42, b"otp 4821", ["otp 4821"]):
        assert find_secrets(bad) == []
        assert redact_secrets(bad).text == ""
        assert screen_sensitive(bad) is None


def test_the_original_coarse_categories_are_unchanged():
    assert screen_sensitive("what are the library hours") is None
    assert screen_sensitive("Rs. 300000 due") == "long_number"
    assert screen_sensitive("mail me at someone@example.com") == "contact_details"
    assert screen_sensitive("I take medication daily") == "health"
    assert screen_sensitive("paid Rs. 5000 yesterday") == "money_amount"
    assert screen_sensitive("my password is hunter2") == "credentials"


def test_hostile_long_inputs_finish_quickly():
    start = time.perf_counter()
    for text in ("otp " * 50000, "a " * 100000, "password: " + "x" * 200000, "1 " * 100000,
                 "a is my otp " * 20000, "password is " * 20000, "#" * 200000):         # the last three once caused a slowdown
        find_secrets(text)
        redact_secrets(text)
    assert time.perf_counter() - start < 10


# ---------------------------------------------------------------- sanitize_details
def test_sanitize_details_masks_secret_values_and_secret_keys():
    out = sanitize_details({"password": "hunter2", "note": "my otp is 4821", "nested": {"token": "abc", "ok": 1},
                            "items": ["fine", "card 4111 1111 1111 1111"]})
    assert out["password"] == "[REDACTED]" and out["nested"]["token"] == "[REDACTED]" and out["nested"]["ok"] == 1
    assert "4821" not in str(out) and "4111" not in str(out) and "hunter2" not in str(out)
    assert out["items"][0] == "fine"


def test_sanitize_details_masks_personal_values_by_default_and_can_keep_them():
    assert sanitize_details({"email": "someone@example.com"})["email"] == "[redacted: personal or sensitive value]"
    assert sanitize_details({"email": "someone@example.com"}, mask_personal=False)["email"] == "someone@example.com"
    assert sanitize_details({"field": "email"}) == {"field": "email"}


def test_sanitize_details_limits_size_depth_and_types_and_never_mutates_the_input():
    original = {"long": "x" * 1000, "deep": {"a": {"b": {"c": {"d": {"e": 1}}}}}, "obj": object(), "n": None, "t": True}
    snapshot = copy.deepcopy({k: v for k, v in original.items() if k != "obj"})
    out = sanitize_details(original)
    assert len(out["long"]) <= 301 and out["obj"] == "<object>" and out["n"] is None and out["t"] is True
    assert "[truncated]" in str(out["deep"])
    assert {k: v for k, v in original.items() if k != "obj"} == snapshot
