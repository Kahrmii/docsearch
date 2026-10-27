"""Agent loop: the model calls tools until it can answer."""
import json

from . import config, llm, tools
from .tools import Cancelled

MAX_STEPS = 8
MAX_HISTORY = 8  # messages (questions and answers) from earlier rounds

SYSTEM = """You are a research assistant for a local document archive (full text in Elasticsearch). \
You answer questions exclusively with the help of the tools.
Rules:
- ALWAYS answer in the language of the user's question (for example a German question gets a German answer).
- Take numbers (counts, frequencies) only from tool results, never estimate them.
- Search names and technical terms as an exact phrase in `phrases`. If you find nothing, try other spellings \
(first name last name / last name, first name) and say what you tried.
- How often does X occur: count_term. Which email addresses/web addresses/domains/dates occur, or which most \
often: find_patterns. Distribution by file type, author or year: group_by. What does it say about X, what did A \
write to B: search_documents with phrases=[A, B]; to read further use get_document.
- A filename (e.g. "000304_report.txt") is not part of the text: use find_files for it, not search_documents. \
If a file is not found there it is not in the index; say so and give possible reasons (too large, error while \
reading, different index); do not claim it does not exist.
- Support content statements with the filename in square brackets, but ONLY with filenames that appear \
word for word in a tool result. Results without filenames (e.g. find_patterns, group_by) have no source: then name \
none and do not invent any. Quote only briefly.
- If the results do not answer the question, say so openly. Invent nothing.
- Do not guess or invent email addresses, names, numbers or figures, and never search for values you only \
assumed. Report only what is in the tool results. An address that stands next to a name in a document does not \
necessarily belong to that person: point this out.
- Answer concisely."""

EMPTY_ANSWER_NUDGE = ("Now give a short answer, in the language of the user's question: what did the tool results "
                      "show? If nothing suitable was found, say so and name what you tried.")


def _size(message):
    return len(json.dumps(message, ensure_ascii=False))


def trim_messages(messages, budget):
    """Keep the history within the character budget: shorten old tool results first, then drop old rounds."""
    messages = list(messages)
    while sum(map(_size, messages)) > budget:
        old_result = next((i for i, m in enumerate(messages[:-1])
                           if m["role"] == "tool" and len(m["content"]) > 200), None)
        if old_result is not None:
            messages[old_result] = {**messages[old_result], "content": "[result shortened]"}
        elif len(messages) > 2:
            del messages[1]  # oldest history entry (0 = system message)
        else:
            break
    return messages


def _answer_or_fallback(client, messages, budget, used_tools):
    """The model said nothing: ask once more, otherwise report honestly instead of showing an empty answer."""
    reply = client.chat(trim_messages(messages + [{"role": "user", "content": EMPTY_ANSWER_NUDGE}], budget))
    text = llm.clean(reply.get("content"))
    if text:
        return text
    return ("The model returned no answer. Tools used: " + (", ".join(used_tools) or "none")
            + ". Please rephrase the question.")


def ask(question, ctx, client, history=None, on_step=None, max_steps=MAX_STEPS):
    """Answer a question with the given LLM client (local or cloud).

    `on_step(name, args)` reports every tool call. Raises Cancelled when aborted."""
    # "/no_think" is a switch of the local Qwen3 model and does not belong in cloud requests
    system = SYSTEM + ("" if client.cloud or config.LLM_THINK else "\n/no_think")
    messages = [{"role": "system", "content": system}] + list(history or [])[-MAX_HISTORY:]
    messages.append({"role": "user", "content": question})
    budget = ctx.ctx_chars
    used_tools = []

    for step in range(max_steps):
        if ctx.should_stop():
            raise Cancelled()
        last = step == max_steps - 1
        if last:
            messages.append({"role": "user", "content": "Answer now with the results you have, "
                                                        "without further tools."})
        reply = client.chat(trim_messages(messages, budget), tools=None if last else tools.TOOLS)
        calls = reply.get("tool_calls") or []
        if not calls:
            return llm.clean(reply.get("content")) or _answer_or_fallback(client, messages, budget, used_tools)
        messages.append({"role": "assistant", "content": reply.get("content") or "", "tool_calls": calls})
        for call in calls:
            function = call["function"]
            used_tools.append(function["name"])
            try:
                args = json.loads(function.get("arguments") or "{}")
            except ValueError:
                args = None
            if on_step:
                on_step(function["name"], args or {})
            result = (tools.run_tool(ctx, function["name"], args) if args is not None
                      else json.dumps({"error": "arguments are not valid JSON"}))
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": result})
    return _answer_or_fallback(client, messages, budget, used_tools)  # model still calls tools in the last step
