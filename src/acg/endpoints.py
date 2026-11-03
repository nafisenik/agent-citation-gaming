"""Talk to different model APIs through one interface.

Two request shapes cover most deployments, and they differ in more than field names: the
tool schema, the tool-call representation and the way a tool result re-enters the
conversation are all different. So an endpoint owns the conversation format as well as the
request, which is what lets :mod:`acg.agent` stay provider-agnostic.

* ``messages`` -- Anthropic-style ``POST /v1/messages``. Also what most local proxies speak.
* ``chat`` -- OpenAI-style ``POST /v1/chat/completions``. Also Azure, vLLM, Ollama,
  Together, Groq and anything else offering a chat-completions endpoint.

Two rules hold for every endpoint:

**No automatic retries.** A failed request is reported as a failure. Silent retries would
turn a transport problem into a changed measurement.

**A returned model name is a report, not an attestation.** Whatever the server says is
recorded verbatim as ``reported_model``, next to the name we requested. Through a proxy or a
gateway those can differ, and neither is independently verified vendor identity.
"""

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


class EndpointError(RuntimeError):
    """A request failed. Carries enough detail to tell transport from protocol problems."""


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class Reply:
    text: str
    tool_calls: tuple[ToolCall, ...]
    reported_model: str
    input_tokens: int
    output_tokens: int
    seconds: float
    raw: dict = field(default=None, repr=False)


@dataclass
class ToolResult:
    call_id: str
    payload: object
    is_error: bool = False


def _post(url: str, body: dict, headers: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read()[:500].decode("utf-8", "replace")
        raise EndpointError(f"HTTP {error.code} from {url}: {detail}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise EndpointError(f"{type(error).__name__} from {url}: {error}") from error
    except json.JSONDecodeError as error:
        raise EndpointError(f"non-JSON response from {url}: {error}") from error


@dataclass
class Endpoint:
    """One configured model API.

    ``api_style`` selects the wire format. ``base_url`` is the server root; the path is
    appended per style. ``api_key_env`` names the environment variable holding the key, so
    no key is ever stored in a config file or passed on a command line.
    """

    name: str
    base_url: str
    model: str
    api_style: str = "messages"
    api_key_env: str = "ACG_API_KEY"
    timeout: int = 300
    extra_headers: dict = field(default_factory=dict)

    STYLES = ("messages", "chat")

    def __post_init__(self):
        if self.api_style not in self.STYLES:
            raise ValueError(
                f"unknown api_style {self.api_style!r}; expected one of {self.STYLES}"
            )

    # -- plumbing ---------------------------------------------------------------

    @property
    def url(self) -> str:
        root = self.base_url.rstrip("/")
        path = "/v1/messages" if self.api_style == "messages" else "/v1/chat/completions"
        # A base_url that already names the full path is used as given.
        return root if root.endswith(path) else root + path

    def _headers(self) -> dict:
        key = os.environ.get(self.api_key_env, "")
        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_style == "messages":
            headers["anthropic-version"] = headers.get(
                "anthropic-version", "2023-06-01"
            )
            if key:
                headers["x-api-key"] = key
                headers["Authorization"] = f"Bearer {key}"
        elif key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def describe(self) -> dict:
        """Configuration worth recording in a run, with no secret material."""
        return {
            "endpoint_name": self.name,
            "url": self.url,
            "api_style": self.api_style,
            "requested_model": self.model,
            "api_key_env": self.api_key_env,
            "api_key_present": bool(os.environ.get(self.api_key_env)),
            "attribution": (
                "reported_model is the server's own label for the model it ran. Through a "
                "proxy or gateway it may differ from the requested name, and it is not "
                "independently verified vendor identity."
            ),
        }

    # -- conversation state -----------------------------------------------------

    def start(self, prompt: str) -> list[dict]:
        if self.api_style == "messages":
            return [{"role": "user", "content": prompt}]
        return [{"role": "user", "content": prompt}]

    def append_reply(self, state: list[dict], reply: Reply) -> None:
        """Record the assistant turn in whatever shape this API expects to read back."""
        if self.api_style == "messages":
            state.append({"role": "assistant", "content": reply.raw.get("content", [])})
        else:
            message = (reply.raw.get("choices") or [{}])[0].get("message", {}) or {}
            state.append(
                {
                    "role": "assistant",
                    "content": message.get("content"),
                    "tool_calls": message.get("tool_calls", []),
                }
            )

    def append_tool_results(self, state: list[dict], results: list[ToolResult]) -> None:
        if self.api_style == "messages":
            state.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": result.call_id,
                            "content": json.dumps(result.payload, ensure_ascii=False),
                            **({"is_error": True} if result.is_error else {}),
                        }
                        for result in results
                    ],
                }
            )
        else:
            # Chat completions wants one message per tool call, not one combined block.
            for result in results:
                state.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.call_id,
                        "content": json.dumps(result.payload, ensure_ascii=False),
                    }
                )

    # -- the request ------------------------------------------------------------

    def send(
        self,
        state: list[dict],
        *,
        system: str,
        tools: list[dict],
        max_tokens: int = 1600,
    ) -> Reply:
        """One request. Raises :class:`EndpointError` rather than retrying or falling back."""
        started = time.time()
        if self.api_style == "messages":
            body = {
                "model": self.model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": state,
            }
            if tools:
                body["tools"] = [
                    {
                        "name": tool["name"],
                        "description": tool["description"],
                        "input_schema": tool["parameters"],
                    }
                    for tool in tools
                ]
        else:
            body = {
                "model": self.model,
                "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, *state],
            }
            if tools:
                body["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool["description"],
                            "parameters": tool["parameters"],
                        },
                    }
                    for tool in tools
                ]

        response = _post(self.url, body, self._headers(), self.timeout)
        return self._parse(response, time.time() - started)

    def _parse(self, response: dict, seconds: float) -> Reply:
        usage = response.get("usage") or {}
        if self.api_style == "messages":
            blocks = response.get("content") or []
            text = "\n".join(
                block.get("text", "")
                for block in blocks
                if block.get("type") == "text"
            ).strip()
            calls = tuple(
                ToolCall(
                    call_id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=block.get("input") or {},
                )
                for block in blocks
                if block.get("type") == "tool_use"
            )
            return Reply(
                text=text,
                tool_calls=calls,
                reported_model=response.get("model") or "",
                input_tokens=usage.get("input_tokens") or 0,
                output_tokens=usage.get("output_tokens") or 0,
                seconds=seconds,
                raw=response,
            )

        choices = response.get("choices") or []
        if not choices:
            raise EndpointError(f"no choices in response: {str(response)[:300]}")
        message = choices[0].get("message") or {}
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            raw_arguments = function.get("arguments")
            if isinstance(raw_arguments, str):
                try:
                    arguments = json.loads(raw_arguments or "{}")
                except json.JSONDecodeError as error:
                    # Report it; a mangled tool call is a real observation about the model.
                    raise EndpointError(
                        f"tool call arguments were not valid JSON: {raw_arguments[:200]!r}"
                    ) from error
            else:
                arguments = raw_arguments or {}
            calls.append(
                ToolCall(
                    call_id=call.get("id", ""),
                    name=function.get("name", ""),
                    arguments=arguments,
                )
            )
        return Reply(
            text=(message.get("content") or "").strip(),
            tool_calls=tuple(calls),
            reported_model=response.get("model") or "",
            input_tokens=usage.get("prompt_tokens") or 0,
            output_tokens=usage.get("completion_tokens") or 0,
            seconds=seconds,
            raw=response,
        )


# Ready-made configurations. Each still needs its key in the named environment variable, and
# `local` needs a proxy already listening -- nothing here starts a server.
PRESETS: dict[str, dict] = {
    "local": {
        "base_url": "http://127.0.0.1:18765",
        "api_style": "messages",
        "model": "gpt-6-sol",
        "api_key_env": "ACG_API_KEY",
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "api_style": "messages",
        "model": "claude-sonnet-5",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "openai": {
        "base_url": "https://api.openai.com",
        "api_style": "chat",
        "model": "gpt-5",
        "api_key_env": "OPENAI_API_KEY",
    },
    "ollama": {
        "base_url": "http://127.0.0.1:11434",
        "api_style": "chat",
        "model": "llama3.1",
        "api_key_env": "ACG_API_KEY",
    },
}


def from_preset(name: str, **overrides) -> Endpoint:
    if name not in PRESETS:
        raise KeyError(f"unknown preset {name!r}; known: {sorted(PRESETS)}")
    return Endpoint(name=name, **{**PRESETS[name], **overrides})
