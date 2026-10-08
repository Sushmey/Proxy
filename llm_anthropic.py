"""Anthropic provider for llm_client.py, built on Anthropic's official
package (pip install anthropic). llm_client imports this file only when
LLM_PROVIDER=anthropic, so nobody else needs the package.

The rest of the code uses one message shape (see llm_client.py). Anthropic's
format differs in ways that matter here:
  - the system prompt is a separate field, not a message;
  - a tool call is a "tool_use" block and its result a "tool_result" block
    inside a user message, and results for several calls go in ONE message;
  - tools are declared with "input_schema", not OpenAI's {"function": ...};
  - max_tokens is required;
  - when the model's built-in thinking is on, its thinking blocks must be
    passed back unchanged while it works through tool calls. The normalized
    reply drops them, so every reply also carries the model's exact blocks
    under RAW_KEY, and they are replayed from there on the next call.
"""

import json

try:
    import anthropic
except ImportError as exc:
    raise RuntimeError(
        "LLM_PROVIDER=anthropic needs Anthropic's package: pip install anthropic"
    ) from exc

RAW_KEY = "_anthropic_content"

_clients = {}


def _client(s):
    """One client per (key, base_url). api_key=None lets the package find
    credentials itself (ANTHROPIC_API_KEY, or a profile from `ant auth login`)."""
    key = (s.api_key, s.base_url)
    if key not in _clients:
        _clients[key] = anthropic.Anthropic(api_key=s.api_key, base_url=s.base_url)
    return _clients[key]


def _to_anthropic_tools(tools):
    out = []
    for t in tools or []:
        fn = t.get("function")
        if fn:
            out.append(
                {
                    "name": fn["name"],
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
                }
            )
        else:
            out.append(t)  # already in Anthropic's shape
    return out


def _as_dict(args):
    if isinstance(args, dict):
        return args
    try:
        parsed = json.loads(args) if isinstance(args, str) and args.strip() else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _to_anthropic_messages(messages):
    """Our message list -> (system string, Anthropic messages)."""
    system_parts = []
    out = []
    pending = []  # (id, name) of tool calls still waiting for a result
    counter = 0

    def push(role, blocks):
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)  # tool results must share one user message
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_parts.append(m["content"])
        elif role == "assistant":
            raw = m.get(RAW_KEY)
            if raw:
                blocks = list(raw)
            else:
                blocks = []
                if (m.get("content") or "").strip():
                    blocks.append({"type": "text", "text": m["content"]})
                for c in m.get("tool_calls") or []:  # a call that came from another provider
                    fn = c.get("function", {})
                    counter += 1
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": c.get("id") or f"toolu_{counter}",
                            "name": fn.get("name"),
                            "input": _as_dict(fn.get("arguments")),
                        }
                    )
            for b in blocks:
                if b.get("type") == "tool_use":
                    pending.append((b["id"], b["name"]))
            if blocks:  # an empty assistant turn would be rejected
                push("assistant", blocks)
        elif role == "tool":
            content = str(m.get("content", "")) or "(empty result)"
            if not pending:
                push("user", [{"type": "text", "text": f"[result of {m.get('name')}] {content}"}])
                continue
            idx = next((i for i, (_, name) in enumerate(pending) if name == m.get("name")), 0)
            call_id, _ = pending.pop(idx)
            push("user", [{"type": "tool_result", "tool_use_id": call_id, "content": content}])
        else:
            text = m.get("content") or ""
            if text.strip():
                push("user", [{"type": "text", "text": text}])

    while out and out[0]["role"] != "user":  # the first message must be the user's
        out.pop(0)
    return "\n\n".join(system_parts), out


def _from_response(response):
    """Anthropic's reply -> our shape, plus the exact blocks under RAW_KEY."""
    blocks = list(response.content)
    calls = []
    for b in blocks:
        if b.type == "tool_use":
            calls.append({"id": b.id, "function": {"name": b.name, "arguments": _as_dict(b.input)}})
    msg = {
        "role": "assistant",
        "content": "".join(b.text for b in blocks if b.type == "text"),
        RAW_KEY: [b.model_dump(exclude_none=True) for b in blocks],
    }
    if calls:
        msg["tool_calls"] = calls

    stop = getattr(response, "stop_reason", None)
    if stop == "max_tokens":
        print("[llm] warning: the reply was cut off at max_tokens. Raise LLM_MAX_TOKENS in .env.")
    elif stop == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        print(f"[llm] warning: the model declined this request (category: {category}).")
    return msg


def chat(s, messages, tools, use_tools, timeout):
    system, converted = _to_anthropic_messages(messages)
    kwargs = {"model": s.model, "max_tokens": s.max_tokens, "messages": converted}
    if system:
        kwargs["system"] = system

    a_tools = _to_anthropic_tools(tools)
    has_tool_blocks = any(
        b.get("type") in ("tool_use", "tool_result") for m in converted for b in m["content"]
    )
    # A request whose history contains tool_use / tool_result blocks must
    # still declare the tools. For the "answer now" fallback (use_tools=False)
    # declare them but forbid calling any.
    if a_tools and (use_tools or has_tool_blocks):
        kwargs["tools"] = a_tools
        if not use_tools:
            kwargs["tool_choice"] = {"type": "none"}

    response = _client(s).with_options(timeout=timeout).messages.create(**kwargs)
    return _from_response(response)


def json_text(s, system_prompt, user_content, timeout):
    """The reply text for a one-shot call; llm_client parses it as JSON.
    There's no prefill (removed on current models), so the system prompts'
    own "respond with ONLY a JSON object" instruction does the work."""
    response = (
        _client(s)
        .with_options(timeout=timeout)
        .messages.create(
            model=s.model,
            max_tokens=s.max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
    )
    return "".join(b.text for b in response.content if b.type == "text")
