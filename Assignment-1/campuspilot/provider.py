"""LLM provider interface. Messages use the OpenAI chat format internally."""
import email.utils
import json
import logging
import math
import random
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger("campuspilot.provider")

TRANSIENT_HTTP = {408, 429, 500, 502, 503, 504}
TRANSIENT_NAMES = {"APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout", "ReadTimeout",
                   "RemoteProtocolError", "TimeoutException"}
_KEY_LIKE = re.compile(r"AQ\.[A-Za-z0-9_.\-]{10,}|AIza[A-Za-z0-9_\-]{10,}|ya29\.[A-Za-z0-9_.\-]{10,}|sk-[A-Za-z0-9_\-]{10,}")
MAX_ERROR_MESSAGE = 200


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    # Opaque provider-specific data (e.g. Gemini thought signature) that must be
    # echoed back on the next turn. Never set by OpenAIProvider.
    provider_data: object = None


@dataclass
class ChatResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int | None = None   # None when the API does not report usage
    output_tokens: int | None = None
    model: str = ""


def _http_status(exc) -> int | None:
    """HTTP status of an API error: google-genai uses .code, the OpenAI SDK uses .status_code."""
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def is_transient_error(exc) -> bool:
    """True for failures worth retrying shortly: overload/rate limit/server errors and connection problems.
    Client errors such as 400/401/403/404 are NOT transient and fail immediately."""
    status = _http_status(exc)
    if status is not None:
        return status in TRANSIENT_HTTP
    return type(exc).__name__ in TRANSIENT_NAMES


def is_quota_error(exc) -> bool:
    """HTTP 429 / RESOURCE_EXHAUSTED: a rate limit or quota. Needs longer waits than a brief server blip."""
    return _http_status(exc) == 429 or getattr(exc, "status", None) == "RESOURCE_EXHAUSTED"


# What a failed call means, for reporting: only 'quota', 'unavailable' and 'config' are the PROVIDER blocking the
# agent; 'request' (e.g. 400) and 'unknown' may be our own bug, so they are never excused as provider problems.
PROVIDER_BLOCKED_KINDS = frozenset({"quota", "unavailable", "config"})


def kind_for_status(status: int) -> str:
    """quota (429) | unavailable (408, 5xx) | config (401/403/404) | request (any other status)."""
    if status == 429:
        return "quota"
    if status == 408 or status >= 500:
        return "unavailable"
    if status in (401, 403, 404):
        return "config"
    return "request"


def error_kind(exc) -> str:
    """quota (429) | unavailable (408, 5xx, connection/timeout) | config (401/403/404: key, permission, model)
    | request (other 4xx) | unknown (not a recognised API error)."""
    if is_quota_error(exc):
        return "quota"
    status = _http_status(exc)
    if status is not None:
        return kind_for_status(status)
    return "unavailable" if type(exc).__name__ in TRANSIENT_NAMES else "unknown"


_RETRY_IN_TEXT = re.compile(r"retry in\s+([0-9]+(?:\.[0-9]+)?)\s*s", re.IGNORECASE)


def _seconds(value):
    """'34s', '1.5s', 34, '34' or {'seconds': 34, 'nanos': 5e8} -> float seconds, else None."""
    try:
        if isinstance(value, dict):
            return float(value.get("seconds", 0)) + float(value.get("nanos", 0)) / 1e9
        if isinstance(value, str):
            value = value.strip().rstrip("sS")
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _find_retry_delays(node, depth=0):
    if depth > 6:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("retryDelay", "retry_delay"):
                seconds = _seconds(value)
                if seconds is not None:
                    yield seconds
            else:
                yield from _find_retry_delays(value, depth + 1)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _find_retry_delays(item, depth + 1)


def retry_after_seconds(exc) -> float | None:
    """How long the server says to wait before retrying, in seconds, or None if it gave no hint.
    Sources: the Retry-After header (seconds or HTTP date), Google's RetryInfo.retryDelay in the error details,
    and 'Please retry in 34.5s' in the message. If several are present the longest wins (the safe choice)."""
    hints = []
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is not None:
        try:
            raw = headers.get("retry-after")
        except Exception:
            raw = None
        if raw is not None:
            seconds = _seconds(raw)
            if seconds is None:
                try:
                    when = email.utils.parsedate_to_datetime(str(raw))
                    seconds = max(0.0, when.timestamp() - time.time())
                except (TypeError, ValueError):
                    seconds = None
            if seconds is not None:
                hints.append(seconds)
    hints.extend(_find_retry_delays(getattr(exc, "details", None)))
    message = getattr(exc, "message", None)
    if isinstance(message, str):
        match = _RETRY_IN_TEXT.search(message)
        if match:
            hints.append(float(match.group(1)))
    return max(hints) if hints else None


class LLMProvider(ABC):
    name = "base"
    _secret: str | None = None            # this provider's API key, used only to mask it in error text
    # Retry/pacing policy. Defaults suit the interactive dashboard (short waits); the evaluation script passes longer ones.
    retry_delays: tuple = (1.0, 2.0)      # waits before each retry of a transient failure (408/5xx/connection): 3 attempts
    quota_delays: tuple = (1.0, 2.0)      # waits before each retry of HTTP 429 / RESOURCE_EXHAUSTED
    max_wait: float = 10.0                # longest single wait; if the server asks for more, give up instead of waiting
    jitter: float = 0.0                   # up to this many extra random seconds per wait (avoids lock-step retries)
    min_interval: float = 0.0             # minimum seconds between the START of two model requests (0 = no pacing)
    _sleep = staticmethod(time.sleep)
    _clock = staticmethod(time.monotonic)
    _last_request: float | None = None

    @abstractmethod
    def chat(self, messages: list[dict], tool_schemas: list[dict]) -> ChatResponse: ...

    def _configure(self, retry_delays=None, quota_delays=None, max_wait=None, jitter=None, min_interval=None,
                   sleep=None, clock=None):
        if retry_delays is not None:
            self.retry_delays = tuple(retry_delays)
            if quota_delays is None:           # backwards compatible: one schedule for both unless told otherwise
                self.quota_delays = tuple(retry_delays)
        if quota_delays is not None:
            self.quota_delays = tuple(quota_delays)
        for attr, value in (("max_wait", max_wait), ("jitter", jitter), ("min_interval", min_interval)):
            if value is not None:
                setattr(self, attr, float(value))
        if sleep is not None:
            self._sleep = sleep
        if clock is not None:
            self._clock = clock

    def _pace(self):
        """Wait, if needed, so consecutive model requests (including retries) start at least min_interval apart."""
        if self.min_interval <= 0:
            return
        now = self._clock()
        if self._last_request is not None:
            wait = self._last_request + self.min_interval - now
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_request = now

    def _with_retry(self, call):
        """Run call() with pacing. This class owns ALL retrying for model requests (see each provider's constructor
        for how the SDK's own retries are handled), so the documented attempt counts and spacing hold.

        Retry HTTP 429 and other transient failures with exponential/backoff waits
        (separate schedules and counts), never waiting less than the server's own hint and never more than
        max_wait: a hint larger than max_wait (e.g. 'retry in 3 hours' for a daily quota) is not waited out.
        Anything else is re-raised immediately. Total attempts per kind = 1 + len(its delays)."""
        used = {"quota": 0, "transient": 0}
        while True:
            self._pace()
            try:
                return call()
            except Exception as exc:
                kind = "quota" if is_quota_error(exc) else "transient" if is_transient_error(exc) else None
                if kind is None:
                    raise
                delays = self.quota_delays if kind == "quota" else self.retry_delays
                if used[kind] >= len(delays):
                    raise
                hint = retry_after_seconds(exc)
                if hint is not None and hint > self.max_wait:
                    logger.warning("%s: %s; server asks to wait %.0fs (> max %.0fs): giving up instead of retrying",
                                   self.name, self.describe_error(exc), hint, self.max_wait)
                    raise
                delay = min(max(delays[used[kind]], hint or 0.0), self.max_wait)
                if self.jitter > 0:
                    delay += random.uniform(0.0, self.jitter)
                used[kind] += 1
                logger.warning("%s: %s LLM error (%s); retry %d/%d in %.1fs%s", self.name, kind,
                               self.describe_error(exc), used[kind], len(delays), delay,
                               f" (server hint {hint:.1f}s)" if hint is not None else "")
                self._sleep(delay)

    def error_kind(self, exc) -> str:
        """Classify a failed call (see module-level error_kind)."""
        return error_kind(exc)

    def _mask(self, text) -> str:
        t = str(text)
        if self._secret:
            t = t.replace(self._secret, "<KEY>")
        return _KEY_LIKE.sub("<KEY>", t)

    def describe_error(self, exc) -> str:
        """Short, safe description for logs/traces: type, HTTP code/status and (for recognised API errors only)
        a truncated message with key-like text masked. Arbitrary exceptions contribute their type name only."""
        parts = [type(exc).__name__]
        status = _http_status(exc)
        if status is not None:
            parts.append(str(status))
        label = getattr(exc, "status", None)
        if isinstance(label, str) and label:
            parts.append(label)
        text = " ".join(parts)
        message = getattr(exc, "message", None) if status is not None else None
        if isinstance(message, str) and message.strip():
            message = re.sub(r"\s+", " ", self._mask(message)).strip()[:MAX_ERROR_MESSAGE]
            return f"{text}: {message}"
        return text


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str, model: str, client=None, **policy):
        if client is None:
            from openai import OpenAI  # lazy: only needed when actually used
            # The SDK retries failed requests itself (default max_retries=2). That would multiply with
            # _with_retry (up to 3x3 attempts) and break the pacing/attempt counts, so retrying is owned here.
            client = OpenAI(api_key=api_key, max_retries=0)
        self.client = client
        self.model = model
        self._secret = api_key
        self._configure(**policy)  # retry_delays, quota_delays, max_wait, jitter, min_interval, sleep, clock

    def chat(self, messages, tool_schemas):
        tools = [{"type": "function", "function": s} for s in tool_schemas]
        resp = self._with_retry(lambda: self.client.chat.completions.create(
            model=self.model, messages=messages, tools=tools))
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = None  # registry reports this as invalid arguments
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        usage = getattr(resp, "usage", None)
        return ChatResponse(
            content=msg.content,
            tool_calls=calls,
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
            model=getattr(resp, "model", self.model),
        )


class GeminiProvider(LLMProvider):
    """Google GenAI SDK (`google-genai`) with manual function calling."""
    name = "gemini"

    def __init__(self, api_key: str, model: str, client=None, **policy):
        if client is None:
            from google import genai  # lazy: only needed when actually used
            # No http_options/retry_options on purpose. Checked in the installed google-genai (2.32.0):
            # retry_args(None) is stop_after_attempt(1), i.e. the SDK does NOT retry unless retry_options are
            # set, so _with_retry is the only retry layer. Re-check this if the SDK is upgraded.
            client = genai.Client(api_key=api_key)
        self.client = client
        self.model = model
        self._secret = api_key
        self._configure(**policy)  # retry_delays, quota_delays, max_wait, jitter, min_interval, sleep, clock

    @staticmethod
    def to_gemini(messages: list[dict]):
        """Translate OpenAI-style messages to (system_instruction, contents)."""
        from google.genai import types

        system, contents, names = None, [], {}
        pending = []  # function_response parts of the current tool round

        def flush():
            if pending:
                contents.append(types.Content(role="user", parts=list(pending)))
                pending.clear()

        for m in messages:
            role = m["role"]
            if role == "tool":
                result = json.loads(m["content"])
                pending.append(types.Part.from_function_response(
                    name=names.get(m["tool_call_id"], "unknown"),
                    response=result if isinstance(result, dict) else {"result": result}))
                continue
            flush()
            if role == "system":
                system = m["content"]
            elif role == "user":
                contents.append(types.Content(role="user", parts=[types.Part(text=m["content"])]))
            elif role == "assistant":
                parts = [types.Part(text=m["content"])] if m.get("content") else []
                for tc in m.get("tool_calls", []):
                    fn = tc["function"]
                    names[tc["id"]] = fn["name"]
                    parts.append(types.Part(
                        function_call=types.FunctionCall(name=fn["name"], args=json.loads(fn["arguments"] or "{}")),
                        thought_signature=tc.get("provider_data")))
                contents.append(types.Content(role="model", parts=parts))
        flush()
        return system, contents

    def chat(self, messages, tool_schemas):
        from google.genai import types

        system, contents = self.to_gemini(messages)
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=s["name"], description=s["description"],
                                          parameters_json_schema=s["parameters"])
                for s in tool_schemas])] if tool_schemas else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        resp = self._with_retry(lambda: self.client.models.generate_content(
            model=self.model, contents=contents, config=config))
        text, calls = [], []
        parts = (resp.candidates[0].content.parts or []) if resp.candidates and resp.candidates[0].content else []
        for i, part in enumerate(parts):
            if part.function_call:
                calls.append(ToolCall(id=f"call_{i}", name=part.function_call.name,
                                      arguments=dict(part.function_call.args or {}),
                                      provider_data=part.thought_signature))
            elif part.text and not part.thought:
                text.append(part.text)
        usage = getattr(resp, "usage_metadata", None)
        out = getattr(usage, "candidates_token_count", None)
        thoughts = getattr(usage, "thoughts_token_count", None)
        if out is not None and thoughts:
            out += thoughts  # thinking tokens are billed as output
        return ChatResponse(
            content="".join(text) or None,
            tool_calls=calls,
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=out,
            model=getattr(resp, "model_version", None) or self.model,
        )


def build_provider(settings, **policy) -> LLMProvider | None:
    """Return a provider for the configured settings, or None if unavailable. `policy` (retry_delays, quota_delays,
    max_wait, jitter, min_interval) overrides the interactive defaults, e.g. for the evaluation script."""
    if settings.llm_provider == "gemini" and settings.gemini_api_key:
        return GeminiProvider(settings.gemini_api_key, settings.gemini_model, **policy)
    if settings.llm_provider == "openai" and settings.openai_api_key:
        return OpenAIProvider(settings.openai_api_key, settings.openai_model, **policy)
    return None
