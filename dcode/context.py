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
EARLIER_TURN_MIN_CHARS = 1500  # smaller outputs cost little and are often what "that" refers to
COMPACT_TRIGGER = 0.75
COMPACT_TARGET = 0.45
SUMMARY_PREFIX = "[Session summary of earlier conversation]"
SHRINK_ARG_KEYS = ("content", "old_text", "new_text")
ARG_STUB = ("[{chars} characters — sent with this call; omitted from history only to save context. "
            "The tool result says whether it worked.]")
ARG_STUB_MARK = "omitted from history only to save context"
USER_SHELL_ID = "shell_"  # the id of a command the user ran with "!", recorded as a tool call in session.py


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
    def __init__(self, context_window: int, max_output_tokens: int, trigger: float = COMPACT_TRIGGER,
                 target: float = COMPACT_TARGET) -> None:
        self.context_window = context_window
        self.trigger, self.target = trigger, target
        # Never let the reply budget eat more than a quarter of a tiny window.
        self.max_output_tokens = min(max_output_tokens, max(256, context_window // 4))
        self.counter = TokenCounter()

    @property
    def input_budget(self) -> int:
        return self.context_window - self.max_output_tokens

    @property
    def compact_threshold(self) -> int:
        return int(self.input_budget * self.trigger)

    @property
    def target_after_compaction(self) -> int:
        return int(self.input_budget * self.target)

    def usage(self, messages: list[Message], tools: list[dict[str, Any]] | None = None) -> int:
        return self.counter.messages(messages, tools)

    def needs_compaction(self, messages: list[Message], tools: list[dict[str, Any]] | None = None) -> bool:
        return self.usage(messages, tools) > self.compact_threshold


def _written_by_dcode(messages: list[Message], index: int) -> bool:
    """An assistant message the model never wrote: the call and the "Noted." recorded for a user's "!command"."""
    message = messages[index]
    calls = message.get("tool_calls") or []
    if calls:
        return all(str(c.get("id") or "").startswith(USER_SHELL_ID) for c in calls)
    before = messages[index - 1] if index else {}
    return message.get("content") == "Noted." and str(before.get("tool_call_id") or "").startswith(USER_SHELL_ID)


def unread_start(messages: list[Message]) -> int:
    """Where the outputs the model hasn't seen begin: right after the last message it wrote."""
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "assistant" and not _written_by_dcode(messages, index):
            return index + 1
    return 0


def unread_user_outputs(messages: list[Message]) -> set[str]:
    """Ids of the user's "!command" outputs the model hasn't read yet. Compaction leaves them for as long as it can."""
    return {str(m.get("tool_call_id")) for m in messages[unread_start(messages):]
            if m.get("role") == "tool" and str(m.get("tool_call_id") or "").startswith(USER_SHELL_ID)}


def elide_tool_results(messages: list[Message], keep_recent: int = KEEP_RECENT_TOOL_RESULTS,
                       min_chars: int = ELIDE_MIN_CHARS, protect: set[str] | None = None) -> int:
    """Replace bulky old tool outputs with short stubs. Returns how many were elided."""
    tool_indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    elided = 0
    for index in tool_indexes[:-keep_recent] if keep_recent else tool_indexes:
        content = messages[index].get("content") or ""
        if not isinstance(content, str) or len(content) < min_chars or content.startswith("[elided"):
            continue
        if protect and messages[index].get("tool_call_id") in protect:
            continue
        head = content[:200].rstrip()
        messages[index] = dict(messages[index])
        messages[index]["content"] = (
            f"[elided {len(content)} chars of {messages[index].get('name', 'tool')} output to save context; "
            f"re-run the tool if needed]\n{head}..."
        )
        elided += 1
    return elided


def elide_earlier_turns(messages: list[Message], min_chars: int = EARLIER_TURN_MIN_CHARS) -> int:
    """At a new user turn, stub the bulky tool outputs of the turns the model already answered.

    Each call re-sends the whole history, and an answered turn's raw outputs (a 10K-char
    `kubectl get pods -A`) were paid for again on every later call; the answer that used them
    stays. Only done at turn start, with the system prompt refresh, so the cached prompt
    prefix changes once per turn, not per call. What the model hasn't read yet stays whole: the
    output of a "!command" the user just ran, or of a turn that was interrupted.
    """
    elided = 0
    answered = unread_start(messages)
    for index, message in enumerate(messages[:answered]):
        content = message.get("content") or ""
        if (message.get("role") != "tool" or not isinstance(content, str) or len(content) < min_chars
                or content.startswith("[elided")):
            continue
        messages[index] = {**message, "content": (
            f"[elided {len(content)} chars of {message.get('name', 'tool')} output from an earlier turn; "
            f"re-run the tool if you need it again]\n{content[:300].rstrip()}...")}
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


def shrink_tool_args(messages: list[Message], keep_recent: int, names: list[str], min_chars: int) -> int:
    """Stub the big arguments of old calls (a whole file in write_file). Tool results can shrink; without this,
    the calls that wrote the code stay in full and a long task outgrows any window. Returns how many were cut."""
    indexes = [i for i, m in enumerate(messages) if m.get("role") == "assistant" and m.get("tool_calls")]
    shrunk = 0
    for index in indexes[:-keep_recent] if keep_recent else indexes:
        calls = []
        for call in messages[index]["tool_calls"]:
            function = call.get("function") or {}
            if function.get("name") in names:
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = None
                big = [k for k in SHRINK_ARG_KEYS if isinstance(args, dict) and isinstance(args.get(k), str)
                       and len(args[k]) > min_chars and ARG_STUB_MARK not in args[k]]
                if big:
                    args.update({k: ARG_STUB.format(chars=len(args[k])) for k in big})
                    # A copy: these dicts are also what the request that was just logged holds.
                    call = {**call, "function": {**function, "arguments": json.dumps(args, ensure_ascii=False)}}
                    shrunk += 1
            calls.append(call)
        messages[index] = {**messages[index], "tool_calls": calls}
    return shrunk


def _all_answered(messages: list[Message], cut: int) -> bool:
    """Cutting here leaves no call without its result, and no result without its call."""
    called = {c.get("id") for m in messages[:cut] for c in (m.get("tool_calls") or [])}
    answered = {m.get("tool_call_id") for m in messages[:cut] if m.get("role") == "tool"}
    return called <= answered


def step_cut(messages: list[Message], keep_tokens: int, counter: TokenCounter, floor: int) -> int:
    """Where to cut inside a running task: before an assistant or user message, never between a call and its
    result, keeping the newest step and as many before it as fit ``keep_tokens``. 0 = nothing to fold."""
    starts = [i for i in range(2, min(floor, len(messages) - 1) + 1)
              if messages[i].get("role") in ("assistant", "user") and _all_answered(messages, i)]
    cut = 0
    for start in reversed(starts):
        if cut and counter.messages(messages[start:]) > keep_tokens:
            break
        cut = start
    if not cut or not any(m.get("role") == "assistant" for m in messages[1:cut]):
        return 0
    return cut


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
    options: dict[str, Any] | None = None,
    *,
    force: bool = False,
    task: str = "",
) -> dict[str, Any] | None:
    """Shrink ``messages`` in place if over budget. Returns a report or None.

    ``options`` is the experiment section (the defaults when absent). ``force``: the server refused the request
    as too long, so neither the estimate nor the configured window can be trusted (a server started with a shorter
    --max-model-len than Dcode was told): at least halve it, whatever the numbers say. ``task``: what the user asked
    for, kept word for word if the message that said it gets folded into a summary.
    """
    options = options or {}
    keep_recent = options.get("keep_recent_tool_results", KEEP_RECENT_TOOL_RESULTS)
    min_chars = options.get("elide_min_chars", ELIDE_MIN_CHARS)
    before = budget.usage(messages, tools)
    if before <= budget.compact_threshold and not force:
        return None
    goal = min(budget.target_after_compaction, before // 2) if force else budget.target_after_compaction
    stage = 1
    folded: list[Message] = []
    pinned = False
    intra_task = False
    protect = unread_user_outputs(messages)
    if force:
        keep_recent = min(keep_recent, 1)

    elided = elide_tool_results(messages, keep_recent, min_chars, protect)
    shrunk = 0
    if options.get("shrink_old_args", True):
        shrunk = shrink_tool_args(messages, keep_recent, options.get("shrink_args_tools", ["write_file", "edit_file"]),
                                  options.get("shrink_args_min_chars", 240))
    after = budget.usage(messages, tools)
    summary_text = ""
    if (after > budget.compact_threshold or force) and options.get("summarization", True):
        tools_cost = budget.counter.messages([], tools)
        system_cost = budget.counter.message(messages[0])
        keep = max(512, goal - system_cost - tools_cost)
        # Nothing the model hasn't read yet goes into a summary.
        floor = min((i for i, m in enumerate(messages) if m.get("tool_call_id") in protect), default=len(messages))
        while floor < len(messages) and floor > 0 and messages[floor].get("role") != "user":
            floor -= 1
        old, recent = split_for_summary(messages, keep, budget.counter)
        if old and len(messages) - len(recent) > floor:
            old, recent = messages[1:floor], messages[floor:]
        # Turn boundaries can't help a single long task. Only over the input budget by default: below it today's
        # request still fits, so it stays exactly as it was (intra_task_trigger moves this for a study).
        remaining = (system_cost + tools_cost + budget.counter.messages(recent)) if old else after
        if options.get("intra_task_summarize", True) and (force or remaining > budget.input_budget * options.get("intra_task_trigger", 1.0)):
            cut = step_cut(messages, keep, budget.counter, floor)
            if cut:
                old, recent, intra_task = messages[1:cut], messages[cut:], True
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
            if intra_task:
                pinned = bool(task) and not any(m.get("role") == "user" and task in str(m.get("content")) for m in recent)
                if pinned:
                    room = max(1000, int(budget.input_budget * CHARS_PER_TOKEN * 0.1))
                    summary_message["content"] += f"\n\n[The task you are working on]\n{task[:room]}"
                summary_message["content"] += "\n\n[Older steps of this task are summarized above. Continue from the steps below.]"
            ack = {"role": "assistant", "content": "Understood. Continuing from the summary."}
            # Roles must alternate on strict templates: the summary already leads into an assistant step.
            bridge = [ack] if not recent or recent[0].get("role") != "assistant" else []
            messages[1:] = [summary_message] + bridge + recent
            stage, folded = 2, old
        after = budget.usage(messages, tools)

    last_resort = goal if force else budget.input_budget
    if after > last_resort:
        # Last resort: elide every tool result, then drop oldest turns.
        stage = 3
        elide_tool_results(messages, 0, min_chars)
        first = 1 if pinned else 0  # that summary holds the task itself
        while budget.usage(messages, tools) > last_resort and len(_turn_boundaries(messages)) > first + 1:
            boundaries = _turn_boundaries(messages)
            del messages[boundaries[first]:boundaries[first + 1]]
        after = budget.usage(messages, tools)

    return {"before": before, "after": after, "elided": elided, "summary": summary_text, "stage": stage, "folded": folded,
            "args_shrunk": shrunk, "intra_task": intra_task}
