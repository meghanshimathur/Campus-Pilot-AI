"""Central detection and redaction of sensitive text (Assignment 2 foundation).

One place decides what counts as sensitive, so memory, history, traces, audit records and (in later batches) the text
sent to the LLM all follow the same rules. Detection is by deterministic patterns in code, never by trusting the model.

Two levels of API:

* ``screen_sensitive(text)``: the original coarse screen (moved here from memory.py, same patterns and order) with
  extra secret detection added. Returns a category name or None. Used to refuse a memory value or to mask a whole
  history message.
* ``find_secrets`` / ``redact_secrets``: precise spans for SECRETS only (passwords, OTPs, PINs, verification codes,
  API keys and tokens, card numbers, national ids, bank identifiers). Redaction replaces just the secret value with
  ``[REDACTED:<category>]`` and leaves the rest of the sentence readable.

* ``scrub_for_storage(text)``: what is safe to WRITE to the database (history, traces): secrets masked in place, and the
  original behaviour kept for personal data (an email address, an amount, a health term, a long number replaces the whole
  message). A message that merely MENTIONS a password ("how do I reset my password?") is kept as it is.

Limits (stated so nobody over-trusts it): this is pattern-based. A secret written in an unusual way, in another
language, or spelled out in words can slip through. ``expect_secret=True`` (the caller knows the user was just asked
for a code or password) makes it stricter. It never claims to find every secret.
"""
import re
from dataclasses import dataclass

_FLAGS = re.IGNORECASE

# ---------------------------------------------------------------- coarse screen (moved from memory.py, unchanged)
# Credentials, payment/bank details, identity numbers, contact details, health, money amounts.
# Deliberately conservative: a false positive (a harmless message redacted or a preference refused) is accepted
# in exchange for never persisting something sensitive. Matching is by pattern, not by trusting the model.
_LEGACY = [
    ("credentials", re.compile(r"\b(password|passcode|passwd|otp|pin|cvv|cvc|secret|api[\s_-]?key|token)\b", _FLAGS)),
    ("payment_details", re.compile(r"\b(card|credit|debit|upi|ifsc|bank|account)\b.{0,20}\b(number|no|details|ending|id)\b"
                                   r"|\b(bank account|credit card|debit card|net ?banking)\b", _FLAGS)),
    ("identity_number", re.compile(r"\b(aadhaar|aadhar|passport|ssn|pan card|national id)\b", _FLAGS)),
    ("long_number", re.compile(r"\d(?:[\s-]?\d){5,}")),  # 6+ digits: card, account, phone, OTP
    ("contact_details", re.compile(r"\S+@\S+\.\S+")),
    ("health", re.compile(r"\b(health|medical|medicine|medication|diagnos\w*|illness|disease|depress\w*|anxiety|"
                          r"therapy|therapist|surgery|pregnan\w*|disabilit\w*|mental|doctor|hospital|injur\w*|"
                          r"sick|cancer|diabet\w*|asthma)\b", _FLAGS)),
    ("money_amount", re.compile(r"(?:\brs\.?|\binr|₹|\$|€|£)\s?\d|\b\d[\d,]*\s?(?:rupees|rs\b|inr\b|usd\b)"
                                r"|\b(salary|income|loan|debt)\b", _FLAGS)),
]

# precise secret categories -> the coarse category name the legacy screen reports
_COARSE_NAME = {
    "card_number": "payment_details", "bank_account": "payment_details", "national_id": "identity_number",
}

# ---------------------------------------------------------------- precise secret detection
@dataclass(frozen=True)
class Finding:
    category: str
    start: int
    end: int


@dataclass(frozen=True)
class Redaction:
    text: str
    findings: tuple


# A label such as "password" or "one time code". Plain "code" is a WEAK label (it also means source code), so it only
# counts when the value after it contains a digit.
_QUALIFIER = (r"(?:one[\s-]?time|verification|security|authentication|authenticator|login|log[\s-]?in|sign[\s-]?in|"
              r"confirmation|2fa|mfa|sms|access)")
_LABEL_CORE = (r"(?:password|passcode|pass[\s-]?code|passphrase|pass[\s-]?phrase|pass\s?word|passwd|pwd|pw|o\.?t\.?p|pin|cvv|cvc|"
               r"secret|credentials?|token|api[\s-]?key|access[\s-]?key|code)")
_LABEL_RE = re.compile(rf"\b(?:{_QUALIFIER}[\s-]+)?{_LABEL_CORE}\b", _FLAGS)
# value FIRST: "48291350 is my otp", "hunter2xyz is the password", "use 48291350 as the otp"
# Bounded on purpose (token length <= 64, starting at a token boundary): an unbounded \S* here is quadratic on a very long
# run of characters.
_SPECIAL = r"[\d#@$%^&*!_+=~]"
_VALUE_FIRST_RE = re.compile(
    rf"(?<!\S)(?P<v>(?=\S{{0,63}}{_SPECIAL})\S{{3,64}})[ \t]+(?:is|was|are)[ \t]+(?:my|the|our|your)[ \t]+(?:\w+[ \t]+){{0,2}}"
    rf"(?:{_QUALIFIER}[\s-]+)?{_LABEL_CORE}\b|"
    rf"\buse[ \t]+(?P<w>(?=\S{{0,63}}{_SPECIAL})\S{{3,64}})[ \t]+as[ \t]+(?:my|the|our|your)[ \t]+(?:\w+[ \t]+){{0,2}}"
    rf"(?:{_QUALIFIER}[\s-]+)?{_LABEL_CORE}\b", _FLAGS)

# Words that show the sentence is ABOUT a credential (resetting it, a policy, ...) rather than stating one.
_ABOUT_CONTEXT = frozenset(
    "reset resetting change changing changed policy policies recovery forgot forgotten expire expires expired expiry rules "
    "rule requirement requirements strength strong weak length".split())
# Tokens that can follow "is" without being a secret value.
_NOT_A_VALUE = frozenset(
    "a an the and or but so if then to of in on at for with from by as is are was be been not no yes none "
    "required requires needed invalid incorrect wrong expired valid correct sent received missing blank empty "
    "here there this that it its my your our their mine yours please thanks thank sorry unknown unavailable "
    "what how why when where which who can could should would will do does did i we you he she they".split())
# A value ends at one of these (a passphrase can be several words).
_VALUE_STOP = frozenset(
    "and but so because please then or if when thanks thank can could now also however why for to on in at with from "
    "by as that which who".split())
# "<label> [a few filler words] <is | : | = | - | to> <value>". One pattern per filler length, tried shortest first, so a
# connector inside the lead-in ("the code sent TO my phone IS 4821") does not stop the search at the first match.
_CONNECTOR = r"[ \t]*(?:\b(?:is|was|are|be|to)\b[ \t]*:?|:|=|->|=>|[-\u2013\u2014])[ \t]*"
_CONNECTORS = [re.compile(rf"\A(?P<fill>(?:[ \t]+[A-Za-z']+){{{n}}}){_CONNECTOR}", _FLAGS) for n in range(6)]
_DIRECT_RE = re.compile(r"\A[ \t]+(?P<v>\S+)")
_TOKEN_RE = re.compile(r"\S+")
_TRAILING = ".,;:!?)\"'”’"

_STANDALONE = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)")),
    ("api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{20,}|\bAQ\.[A-Za-z0-9_.-]{20,}|\bya29\.[A-Za-z0-9_.-]{20,}"
                           r"|\bsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}"
                           r"|\bAKIA[0-9A-Z]{16}\b")),
    ("token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
                         r"|\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("national_id", re.compile(r"(?<!\d)\d{4}[ -]\d{4}[ -]\d{4}(?!\d)|\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("bank_account", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b|\b[\w.-]{2,}@(?:ok(?:icici|hdfcbank|sbi|axis)|ybl|paytm|upi|ibl|axl|apl)\b")),
]
# "account number 123456789012", "aadhaar 1234 5678 9012", "passport K1234567", "ssn 123-45-6789": a label, then a value with
# at least five digits. A label with no number after it ("what is the account number format?") is not a secret.
_ID_LABEL_RE = re.compile(
    r"\b(?:(?:bank\s+)?account\s*(?:number|no\.?|num|#)|a/c\s*(?:no\.?|number)?|acct\s*(?:no\.?|number)?|iban|"
    r"aadhaar|aadhar|passport(?:\s+(?:number|no\.?))?|ssn|social\s+security(?:\s+number)?|pan\s+(?:card\s+)?(?:number|no\.?))",
    _FLAGS)
_ID_VALUE_RE = re.compile(r"\A[ \t]*(?:(?:is|was)\b|[:=-])?[ \t]*(?P<v>[A-Za-z0-9][A-Za-z0-9 -]{5,30}[A-Za-z0-9])")
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")


def _looks_like_hex_token(run: str) -> bool:
    """A random-looking hex string (a hash or token), not a long repeated word like 'aaaa...' or a bare number."""
    return (any(c.isdigit() for c in run) and any(c.isalpha() for c in run) and len(set(run.lower())) >= 8)
_BARE_REPLY_RE = re.compile(r"\A\W*(?:(?:it|that)(?:'s|\s+is)|its|here(?:'s|\s+is)|is)?\W*(?P<v>\S{4,40}?)\W*\Z", _FLAGS)


def _luhn_ok(digits: str) -> bool:
    total, flip = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if flip:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        flip = not flip
    return total % 10 == 0


def _label_category(label: str) -> str:
    low = re.sub(r"[\s.\-]", "", label.lower())
    if "otp" in low or "onetime" in low:
        return "otp"
    if low.endswith("pin"):
        return "pin"
    if "cvv" in low or "cvc" in low:
        return "security_code"
    if any(w in low for w in ("password", "passwd", "pwd", "passcode", "passphrase", "credential")) or low in ("pw", "passcode"):
        return "password"
    if any(w in low for w in ("key", "token", "secret")):
        return "api_key"
    return "verification_code"


# Plain "code", "token" and "secret" are also everyday words (source code, a bus token, "the secret to good grades"), so
# they only count when the value after them contains a digit. Qualified forms ("verification code", "api key") are strong.
_WEAK_LABELS = frozenset({"code", "token", "secret"})
# "pin code", "course code", "promo code" ... are not credentials.
_BENIGN_CODE_PREFIX = frozenset(
    "pin zip postal area country course subject class module room building department dept branch promo coupon discount "
    "source student roll reference".split())


def _is_weak(label: str) -> bool:
    return label.strip().lower() in _WEAK_LABELS


def _norm(token: str) -> str:
    return token.strip(_TRAILING + "([{\"'“‘").lower()


def _value_after(text: str, pos: int, label: str):
    """Span (start, end) of the secret value stated after the label at ``pos``, or None."""
    weak = _is_weak(label)
    rest = text[pos:pos + 240]
    for connector in _CONNECTORS:
        m = connector.match(rest)
        if not m:
            continue
        if any(_norm(w) in _ABOUT_CONTEXT for w in m.group("fill").split()):
            return None                                           # the sentence is about resetting / a policy, not a value
        start = pos + m.end()
        first = _TOKEN_RE.match(text, start)
        if first is None:
            continue
        first_tok = first.group(0)
        norm = _norm(first_tok)
        if len(norm) < 3 or norm in _NOT_A_VALUE or norm in _ABOUT_CONTEXT:
            continue                                              # not a value: maybe a later connector is the real one
        if weak and not (any(c.isdigit() for c in norm) and len(norm) >= 4):
            continue
        end = start + len(first_tok.rstrip(_TRAILING)) if first_tok.rstrip(_TRAILING) else start + len(first_tok)
        # a passphrase can be several words: keep going until punctuation, a stop word or 6 tokens
        if not first_tok.isdigit() and not weak and first_tok[-1] not in ".,;!?\n":
            count, cursor = 1, first.end()
            while count < 6:
                nxt = _TOKEN_RE.search(text, cursor)
                if nxt is None or "\n" in text[cursor:nxt.start()]:
                    break
                word = nxt.group(0)
                if _norm(word) in _VALUE_STOP or not _norm(word):
                    break
                end = nxt.start() + len(word.rstrip(_TRAILING) or word)
                count, cursor = count + 1, nxt.end()
                if word[-1] in ".,;!?":
                    break
        return (start, end)
    direct = _DIRECT_RE.match(rest)
    if direct:
        tok = direct.group("v")
        norm = _norm(tok)
        has_digit = any(c.isdigit() for c in norm)
        has_special = any(not c.isalnum() for c in norm)
        minimum = 3 if re.sub(r"\W", "", label.lower()) in ("cvv", "cvc") else 4
        if (has_digit or (has_special and not weak)) and len(norm) >= minimum and norm not in _NOT_A_VALUE:
            start = pos + direct.start("v")
            return (start, start + len(tok.rstrip(_TRAILING) or tok))
    return None


def _merge(findings):
    out = []
    for f in sorted(findings, key=lambda f: (f.start, -f.end)):
        if out and f.start < out[-1].end:
            prev = out[-1]
            if f.end > prev.end:
                out[-1] = Finding(prev.category, prev.start, f.end)
            continue
        out.append(f)
    return out


def find_secrets(text, *, expect_secret: bool = False) -> list:
    """Precise spans of secret values in ``text`` (empty list if none or ``text`` is not a string).

    ``expect_secret=True`` means the caller just asked the user for a code or password: a short reply that is only a
    value ("4821", "it is hunter2") is then treated as the secret."""
    if not isinstance(text, str) or not text:
        return []
    found = []
    for m in _LABEL_RE.finditer(text):
        label = m.group(0)
        low = label.strip().lower()
        if low == "pin" and text[m.end():m.end() + 12].lstrip().lower().startswith("code"):
            continue                                            # a postal PIN code
        if low == "code":
            before = text[max(0, m.start() - 20):m.start()].split()
            if before and _norm(before[-1]) in _BENIGN_CODE_PREFIX:
                continue                                        # "course code", "promo code", ...
        span = _value_after(text, m.end(), label)
        if span:
            found.append(Finding(_label_category(m.group(0)), span[0], span[1]))
    for m in _VALUE_FIRST_RE.finditer(text):
        group = "v" if m.group("v") is not None else "w"
        value = m.group(group)
        label = text[m.end(group):m.end()]
        if len(value.strip(_TRAILING)) >= 3 and _norm(value) not in _NOT_A_VALUE:
            found.append(Finding(_label_category(label), m.start(group), m.start(group) + len(value.rstrip(_TRAILING) or value)))
    for m in _ID_LABEL_RE.finditer(text):
        value = _ID_VALUE_RE.match(text[m.end():m.end() + 60])
        if value:
            raw = value.group("v")
            # keep only the number-like part ("123456789012 for refunds" -> "123456789012"; "K1234567"; "123-45-6789")
            number = re.match(r"[A-Za-z]{0,2}\d(?:[ -]?\d)*[A-Za-z]?", raw)
            if number and sum(c.isdigit() for c in number.group(0)) >= 5:
                start = m.end() + value.start("v")
                kind = "bank_account" if re.match(r"(?:bank|account|a/c|acct|iban)", m.group(0), re.IGNORECASE) else "national_id"
                found.append(Finding(kind, start, start + len(number.group(0))))
    for category, pattern in _STANDALONE:
        for m in pattern.finditer(text):
            found.append(Finding(category, m.start(), m.end()))
    for m in _HEX_RE.finditer(text):
        if _looks_like_hex_token(m.group(0)):
            found.append(Finding("token", m.start(), m.end()))
    for m in _CARD_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            found.append(Finding("card_number", m.start(), m.end()))
    if expect_secret and not found:
        m = _BARE_REPLY_RE.match(text.strip())
        if m:
            v = m.group("v")
            if any(c.isdigit() for c in v) or any(not c.isalnum() for c in v):          # a plain word ("thanks") never counts
                offset = len(text) - len(text.lstrip())
                start = offset + text.strip().find(v)
                found.append(Finding("secret_value", start, start + len(v)))
    return _merge(found)


def contains_secret(text, *, expect_secret: bool = False) -> bool:
    return bool(find_secrets(text, expect_secret=expect_secret))


def redact_secrets(text, *, expect_secret: bool = False) -> Redaction:
    """Replace each secret value with ``[REDACTED:<category>]``. The rest of the text is unchanged."""
    if not isinstance(text, str):
        return Redaction("", ())
    findings = find_secrets(text, expect_secret=expect_secret)
    out, cursor = [], 0
    for f in findings:
        out.append(text[cursor:f.start])
        out.append(f"[REDACTED:{f.category}]")
        cursor = f.end
    out.append(text[cursor:])
    return Redaction("".join(out), tuple(findings))


def screen_sensitive(text: str) -> str | None:
    """Coarse screen: the category if the text looks sensitive, else None.

    Keeps the original patterns and order (so existing behaviour is unchanged) and adds the precise secret scan, which
    catches phrasings the label-only patterns miss ("my one time code is 4821", "my pwd is hunter2")."""
    if not isinstance(text, str):
        return None
    for name, pattern in _LEGACY:
        if pattern.search(text):
            return name
    findings = find_secrets(text)
    if findings:
        return _COARSE_NAME.get(findings[0].category, "credentials")
    return None


REDACTED_MESSAGE = "[redacted: message may contain sensitive information]"
# Categories of the coarse screen that are PERSONAL data actually present in the text (an email address, a long number, an
# amount, a health term): they keep the original whole-message redaction in stored text. The label-only categories do not:
# the words "password", "account number" or "aadhaar" are not secrets, and the real values (a password, an account or card
# or aadhaar NUMBER) are masked precisely by find_secrets instead.
_PERSONAL = frozenset({"long_number", "contact_details", "health", "money_amount"})


def scrub_for_storage(text, *, expect_secret: bool = False, max_chars: int = 2000) -> str:
    """Text that is safe to write to the database. Secret values are masked in place (``[REDACTED:otp]``) and the rest of
    the message is kept. If the remaining text still holds personal data (email, amount, health term, long number) the
    whole message is replaced, as it always was. Idempotent."""
    if not isinstance(text, str):
        return ""
    masked = redact_secrets(text.strip(), expect_secret=expect_secret).text
    for name, pattern in _LEGACY:
        if name in _PERSONAL and pattern.search(masked):
            return REDACTED_MESSAGE
    return masked[:max_chars]


_ASK_VERB = r"(?:enter|share|send|provide|type|tell|give|reply|confirm|submit|what\s+is|what's|need|require|read\s+out)"
_ASK_RE = re.compile(
    rf"\b{_ASK_VERB}\b[^.?!\n]{{0,60}}\b(?:otp|one[\s-]?time\s+(?:code|password|pin|passcode)|password|passcode|pin|"
    rf"verification\s+code|security\s+code|cvv|authentication\s+code)\b", _FLAGS)


def asks_for_secret(text) -> bool:
    """Did this (assistant) message ask the user to type a code or password? If so, the user's next short reply is treated as
    the secret (``expect_secret``). A message that only says it will NEVER ask ("do not share your OTP") does not count."""
    if not isinstance(text, str) or not text:
        return False
    if re.search(r"\b(?:never|do\s+not|don't|should\s+not|must\s+not|cannot|can't|won't)\b[^.?!\n]{0,40}"
                 r"\b(?:share|send|enter|type|ask|need|store|save|give)\b", text, _FLAGS):
        return False
    return bool(_ASK_RE.search(text))


# ---------------------------------------------------------------- structured data (audit details, trace summaries)
_SECRET_KEYS = frozenset(
    "password passwd pwd passcode otp pin cvv cvc secret token api_key apikey authorization auth credential credentials "
    "session_token access_token refresh_token".split())


def sanitize_details(value, *, max_str: int = 300, max_depth: int = 4, mask_personal: bool = True):
    """A copy of ``value`` that is safe to store: secret values masked, values under secret-looking keys replaced,
    strings capped, depth limited. With ``mask_personal`` a string that the coarse screen flags (an email address,
    a phone number, a health term, ...) is replaced as a whole, so audit records describe what was attempted without
    keeping the personal value itself. The input is never modified."""
    if max_depth < 0:
        return "[truncated]"
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:50]:
            key = str(k)[:60]
            if key.lower() in _SECRET_KEYS:
                out[key] = "[REDACTED]"
            else:
                out[key] = sanitize_details(v, max_str=max_str, max_depth=max_depth - 1, mask_personal=mask_personal)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_details(v, max_str=max_str, max_depth=max_depth - 1, mask_personal=mask_personal)
                for v in list(value)[:50]]
    if isinstance(value, str):
        text = redact_secrets(value).text
        # a bare word ("otp", "email", "password") names a kind of thing; the coarse screen is for VALUES
        if mask_personal and not re.fullmatch(r"[A-Za-z_]{1,30}", text) and screen_sensitive(text) is not None \
                and "[REDACTED:" not in text:
            return "[redacted: personal or sensitive value]"
        return text if len(text) <= max_str else text[:max_str] + "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__name__}>"
