"""LLM clients: local (OpenAI-compatible, e.g. LM Studio) or cloud (Anthropic Messages API).

Both return answers in the same format (OpenAI style with tool_calls), so the agent does not need to know the provider."""
import json
import logging
import re
import time
import urllib.error
import urllib.request

from . import cloud_policy, config

# Hard-coded: cloud data only ever goes to this address (not changeable through configuration).
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
PROVIDER_LABELS = {"local": "Local (LM Studio)", "anthropic": "Claude (cloud)"}

log = logging.getLogger("docsearch.llm")


class LlmError(RuntimeError):
    pass


class CloudNotAllowed(LlmError):
    pass


def clean(text):
    """Remove thinking blocks."""
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def _post(url, headers, body, timeout, retries=3, unreachable_hint=""):
    """JSON POST with retries on 429/529 (rate limit, overload)."""
    for attempt in range(retries):
        request = urllib.request.Request(url, json.dumps(body).encode("utf-8"),
                                         {"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 529) and attempt < retries - 1:
                time.sleep(min(float(e.headers.get("retry-after") or 5 * (attempt + 1)), 30))
                continue
            raise LlmError(f"LLM server {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise LlmError(f"LLM server not reachable ({url.split('/v1')[0]}): {e.reason}.{unreachable_hint}") from e


# ---- local ---------------------------------------------------------------------------------------

class LocalClient:
    cloud = False

    def label(self):
        return f"local: {config.LLM_MODEL}"

    def chat(self, messages, tools=None, max_tokens=1500, temperature=0.1):
        body = {"model": config.LLM_MODEL, "messages": messages, "temperature": temperature,
                "max_tokens": max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        reply = _post(config.LLM_BASE_URL + "/v1/chat/completions", {}, body, 900,
                      unreachable_hint=" Is the model loaded and the server started?")
        choice = reply["choices"][0]
        message = choice["message"]
        if not clean(message.get("content")) and not message.get("tool_calls"):
            usage = reply.get("usage", {})  # evidence for "why did the model say nothing?" in logs/app_*.log
            log.warning("Empty model reply: finish_reason=%s prompt_tokens=%s completion_tokens=%s "
                        "reasoning_chars=%s", choice.get("finish_reason"), usage.get("prompt_tokens"),
                        usage.get("completion_tokens"), len(message.get("reasoning_content") or ""))
        return message

    def context_tokens(self, default=8192):
        """Context length loaded in the server (LM Studio API), otherwise `default`."""
        try:
            with urllib.request.urlopen(config.LLM_BASE_URL + "/api/v0/models", timeout=10) as response:
                for model in json.loads(response.read().decode("utf-8"))["data"]:
                    if model["id"] == config.LLM_MODEL and model.get("loaded_context_length"):
                        return int(model["loaded_context_length"])
        except Exception:
            pass
        return default

    def context_chars(self):
        """Character budget for the messages. Normal text is about 3 characters per token, but hashes, keys and
        base64 only about 1.1, so a cautious 2 is used; 3200 tokens are reserved for the tool definitions
        and the answer."""
        return max(4000, int((self.context_tokens() - 3200) * 2.0))


# ---- cloud: Anthropic ----------------------------------------------------------------------------

def to_anthropic_tool(tool):
    function = tool["function"]
    return {"name": function["name"], "description": function["description"], "input_schema": function["parameters"]}


def to_anthropic_messages(messages):
    """OpenAI messages -> (system, Anthropic messages). Tool results become tool_result blocks."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    out, open_ids = [], set()

    def add(role, blocks):
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        role = m["role"]
        if role == "user":
            add("user", [{"type": "text", "text": m["content"]}])
        elif role == "assistant":
            blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            open_ids = set()
            for call in m.get("tool_calls") or []:
                open_ids.add(call["id"])
                blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"],
                               "input": json.loads(call["function"].get("arguments") or "{}")})
            if blocks:
                add("assistant", blocks)
        elif role == "tool" and m.get("tool_call_id") in open_ids:  # the API rejects orphaned results
            add("user", [{"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}])
    while out and out[0]["role"] != "user":  # the API requires a user message first
        out.pop(0)
    return system, out


def from_anthropic(reply):
    blocks = reply.get("content") or []
    calls = [{"id": b["id"], "type": "function",
              "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}}
             for b in blocks if b["type"] == "tool_use"]
    return {"role": "assistant", "content": "".join(b["text"] for b in blocks if b["type"] == "text"),
            "tool_calls": calls, "stop_reason": reply.get("stop_reason")}


class AnthropicClient:
    cloud = True

    def __init__(self):
        self._key = config.anthropic_key()
        self.model = config.ANTHROPIC_MODEL

    def label(self):
        return f"cloud: {self.model}"

    def context_chars(self):
        return 120_000  # large context window; the budget only limits cost and response time

    def chat(self, messages, tools=None, max_tokens=2000, temperature=None):
        # `temperature` is accepted for interface compatibility; the cloud model uses its default
        system, converted = to_anthropic_messages(messages)
        body = {"model": self.model, "max_tokens": max_tokens, "messages": converted}
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [to_anthropic_tool(t) for t in tools]
        headers = {"x-api-key": self._key, "anthropic-version": ANTHROPIC_VERSION}
        reply = _post(ANTHROPIC_URL, headers, body, 300)
        message = from_anthropic(reply)
        if not message["content"].strip() and not message["tool_calls"]:
            # e.g. stop_reason "refusal": the provider's safety filters blocked the answer
            log.warning("Empty cloud reply: stop_reason=%s usage=%s", reply.get("stop_reason"), reply.get("usage"))
        return message


def get_client(provider, index):
    """Client for the provider. Cloud only for indices that were explicitly approved."""
    if provider == "anthropic":
        if not cloud_policy.is_allowed(index):
            raise CloudNotAllowed(
                f"Index '{index}' is not approved for the cloud; nothing was sent. "
                "In the window tick 'Allow cloud for this index' "
                f"(command line: python ask.py --index {index} --allow-cloud).")
        return AnthropicClient()
    if provider == "local":
        return LocalClient()
    raise LlmError(f"Unknown provider '{provider}' (allowed: local, anthropic)")
