"""Context-window budgeting for small-context (≤32K) local models.

No tokenizer is available offline for arbitrary models, so we estimate with a
chars-per-token heuristic and *calibrate* it against the prompt_tokens the
server reports back. Compaction then happens in two stages:

1. Elide old tool outputs (cheap, lossless for the model's reasoning chain —
   the call itself stays, only the bulky result is replaced by a stub).
2. Summarize the oldest turns into a single "session summary" message.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

Message = dict[str, Any]

CHARS_PER_TOKEN = 3.5
PER_MESSAGE_OVERHEAD = 6
KEEP_RECENT_TOOL_RESULTS = 3
ELIDE_MIN_CHARS = 600
SUMMARY_PREFIX = "[Session summary of earlier conversation]"


class TokenCounter:
    """Heuristic token estimator that learns from server-reported usage."""

    def __init__(self) -> None:
        self.ratio = 1.0  # actual / estimated, smoothed

    def raw(self, text: str) -> int:
        return int(len(text) / CHARS_PER_TOKEN) + 1

    def text(self, text: str) -> int:
        return int(self.raw(text) * self.ratio)

    def message(self, message: Message) -> int:
        return int(self._raw_message(message) * self.ratio)

    def messages(self, messages: list[Message], tools: list[dict[str, Any]] | None = None) -> int:
        return int(self._raw_total(messages, tools) * self.ratio)

    def calibrate(self, messages: list[Message], tools: list[dict[str, Any]] | None, actual: int) -> None:
        if actual <= 0:
            return
        estimate = self._raw_total(messages, tools)
        if estimate <= 0:
            return
        observed = max(0.5, min(actual / estimate, 2.5))
        self.ratio = 0.7 * self.ratio + 0.3 * observed

    def _raw_total(self, messages: list[Message], tools: list[dict[str, Any]] | None) -> int:
        total = sum(self._raw_message(m) for m in messages)
        if tools:
            total += self.raw(json.dumps(tools, separators=(",", ":")))
        return total

    def _raw_message(self, message: Message) -> int:
        total = PER_MESSAGE_OVERHEAD
        content = message.get("content")
        if isinstance(content, str):
            total += self.raw(content)
        elif isinstance(content, list):
            total += self.raw(json.dumps(content))
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            total += self.raw(str(function.get("name", ""))) + self.raw(str(function.get("arguments", "")))
        return total


class ContextBudget:
    def __init__(self, context_window: int, max_output_tokens: int) -> None:
        self.context_window = context_window
        # Never let the reply budget eat more than a quarter of a tiny window.
        self.max_output_tokens = min(max_output_tokens, max(256, context_window // 4))
        self.counter = TokenCounter()

    @property
    def input_budget(self) -> int:
        return self.context_window - self.max_output_tokens

    @property
    def compact_threshold(self) -> int:
        return int(self.input_budget * 0.75)

    @property
    def target_after_compaction(self) -> int:
        return int(self.input_budget * 0.45)

    def usage(self, messages: list[Message], tools: list[dict[str, Any]] | None = None) -> int:
        return self.counter.messages(messages, tools)

    def needs_compaction(self, messages: list[Message], tools: list[dict[str, Any]] | None = None) -> bool:
        return self.usage(messages, tools) > self.compact_threshold


def elide_tool_results(messages: list[Message], keep_recent: int = KEEP_RECENT_TOOL_RESULTS) -> int:
    """Replace bulky old tool outputs with short stubs. Returns how many were elided."""
    tool_indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    elided = 0
    for index in tool_indexes[:-keep_recent] if keep_recent else tool_indexes:
        content = messages[index].get("content") or ""
        if not isinstance(content, str) or len(content) < ELIDE_MIN_CHARS or content.startswith("[elided"):
            continue
        head = content[:200].rstrip()
        messages[index] = dict(messages[index])
        messages[index]["content"] = (
            f"[elided {len(content)} chars of {messages[index].get('name', 'tool')} output to save context; "
            f"re-run the tool if needed]\n{head}..."
        )
        elided += 1
    return elided


def _turn_boundaries(messages: list[Message]) -> list[int]:
    """Indexes of user messages — the only safe places to cut history.

    Cutting anywhere else could separate an assistant tool_call from its tool
    result, which most servers reject.
    """
    return [i for i, m in enumerate(messages) if m.get("role") == "user" and i > 0]


def split_for_summary(messages: list[Message], keep_tokens: int, counter: TokenCounter) -> tuple[list[Message], list[Message]]:
    """Split non-system history into (old_to_summarize, recent_to_keep)."""
    boundaries = _turn_boundaries(messages)
    if len(boundaries) < 2:
        return [], messages[1:]
    # Walk back from the newest turn, keeping turns while they fit keep_tokens.
    cut = boundaries[-1]
    for boundary in reversed(boundaries):
        if counter.messages(messages[boundary:]) > keep_tokens:
            break
        cut = boundary
    if cut <= boundaries[0]:
        # Everything fits or only one turn exists — nothing to fold away.
        cut = boundaries[1] if len(boundaries) > 1 else boundaries[0]
    old = [m for m in messages[1:cut]]
    recent = messages[cut:]
    return old, recent


def render_for_summary(messages: list[Message], limit_chars: int) -> str:
    """Flatten messages into compact text for the summarizer prompt."""
    lines: list[str] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if not isinstance(content, str):
            content = json.dumps(content)
        if role == "tool":
            lines.append(f"TOOL {m.get('name', '')}: {content[:300]}")
        elif role == "assistant":
            calls = ", ".join(
                f"{(c.get('function') or {}).get('name')}({str((c.get('function') or {}).get('arguments', ''))[:120]})"
                for c in m.get("tool_calls") or []
            )
            if content:
                lines.append(f"ASSISTANT: {content[:1200]}")
            if calls:
                lines.append(f"ASSISTANT called: {calls}")
        else:
            lines.append(f"{str(role).upper()}: {content[:1500]}")
    text = "\n".join(lines)
    if len(text) > limit_chars:
        text = text[-limit_chars:]
    return text


SUMMARY_INSTRUCTIONS = (
    "Summarize this coding-session transcript for your future self. Keep: the user's goals, "
    "decisions made, files touched (paths), key facts discovered, commands that worked/failed, "
    "and what remains to do. Bullet points, at most 250 words. No preamble."
)


def compact(
    messages: list[Message],
    budget: ContextBudget,
    summarize: Callable[[str], str],
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Shrink ``messages`` in place if over budget. Returns a report or None."""
    before = budget.usage(messages, tools)
    if before <= budget.compact_threshold:
        return None

    elided = elide_tool_results(messages)
    after = budget.usage(messages, tools)
    summary_text = ""
    if after > budget.compact_threshold:
        tools_cost = budget.counter.messages([], tools)
        keep = max(512, budget.target_after_compaction - budget.counter.message(messages[0]) - tools_cost)
        old, recent = split_for_summary(messages, keep, budget.counter)
        if old:
            previous = [m for m in old if isinstance(m.get("content"), str) and m["content"].startswith(SUMMARY_PREFIX)]
            transcript = render_for_summary(old, limit_chars=int(budget.input_budget * CHARS_PER_TOKEN * 0.6))
            try:
                summary_text = summarize(transcript).strip()
            except Exception:
                summary_text = ""
            if not summary_text:
                # Fall back to a mechanical digest rather than losing everything.
                summary_text = "\n".join(
                    f"- {m['content'][:200]}" for m in old if m.get("role") == "user" and isinstance(m.get("content"), str)
                )[:2000]
            if previous and previous[-1]["content"] not in summary_text:
                summary_text = previous[-1]["content"][len(SUMMARY_PREFIX):].strip()[:1500] + "\n" + summary_text
            summary_message = {"role": "user", "content": f"{SUMMARY_PREFIX}\n{summary_text}"}
            ack = {"role": "assistant", "content": "Understood. Continuing from the summary."}
            messages[1:] = [summary_message, ack] + recent
        after = budget.usage(messages, tools)

    if after > budget.input_budget:
        # Last resort: elide every tool result, then drop oldest turns.
        elide_tool_results(messages, keep_recent=0)
        while budget.usage(messages, tools) > budget.input_budget and len(_turn_boundaries(messages)) > 1:
            boundaries = _turn_boundaries(messages)
            del messages[boundaries[0]:boundaries[1]]
        after = budget.usage(messages, tools)

    return {"before": before, "after": after, "elided": elided, "summary": summary_text}
