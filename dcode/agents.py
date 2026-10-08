"""Agent roles and prompts.

The main agent orchestrates. Sub-agents get a *fresh* context window with only
their task, a role-specific tool set, and a short prompt, then hand back a
compact report. On a 32K model this is the single biggest win: exploration
that would burn 20K tokens of file contents in the main thread costs the main
agent only the ~500-token report.
"""

from __future__ import annotations

from .tools import READ_ONLY_TOOL_NAMES

MEMORY_TOOL_NAMES = ["memory_save", "memory_search"]

# Permission modes (cycle with Shift+Tab in the terminal).
MODES = ["accept_edits", "auto", "plan", "manual"]
DEFAULT_MODE = "accept_edits"
MODE_LABELS = {
    "manual": "manual — ask before every edit and command",
    "accept_edits": "accept edits — edit freely, ask before commands",
    "auto": "auto — never ask",
    "plan": "plan — read-only, propose a plan first",
}
MODE_SHORT = {"manual": "manual mode", "accept_edits": "accept edits", "auto": "auto mode", "plan": "plan mode"}
FILE_CHANGE_TOOLS = {"write_file", "edit_file", "create_directory", "delete_file"}
PLAN_BLOCKED_TOOLS = FILE_CHANGE_TOOLS | {"run_command", "git_commit", "job_stop"}
JOB_TOOL_NAMES = ["job_status", "job_stop"]
JOBS_PROMPT = (
    "For long-running commands (dev servers, watchers, long builds) use run_command with background=true, "
    "then check them with job_status and stop them with job_stop."
)
PLAN_PROMPT = (
    "PLAN MODE is on: you must not modify files or run commands (those tools are disabled). "
    "Investigate with read-only tools (and explore/review helpers), then present a concise, numbered plan: "
    "goal, files to change and how, commands to run, risks. End by asking the user to approve the plan."
)
TODO_PROMPT = (
    "For tasks with 3 or more steps, keep a checklist with todo_write: send the whole list, mark the step "
    "you are on in_progress and finished ones done. It survives context compaction, so it keeps you on track."
)
TODO_STATUSES = ("pending", "in_progress", "done")
_TODO_ALIASES = {"completed": "done", "complete": "done", "finished": "done", "todo": "pending",
                 "not_started": "pending", "doing": "in_progress", "active": "in_progress", "in-progress": "in_progress"}


def normalize_todos(raw) -> list[dict[str, str]]:
    """Accept what small models actually send: objects, plain strings, odd status words."""
    if isinstance(raw, str):
        raw = [line.strip("-*• ") for line in raw.splitlines() if line.strip()]
    items = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, str):
            text, status = entry, "pending"
        elif isinstance(entry, dict):
            text = str(entry.get("text") or entry.get("content") or entry.get("task") or entry.get("title") or "")
            status = str(entry.get("status") or "pending").strip().lower().replace(" ", "_")
        else:
            continue
        status = _TODO_ALIASES.get(status, status)
        if text.strip():
            items.append({"text": " ".join(text.split())[:200],
                          "status": status if status in TODO_STATUSES else "pending"})
    return items[:30]


def render_todos(items: list[dict[str, str]]) -> str:
    marks = {"done": "[x]", "in_progress": "[>]", "pending": "[ ]"}
    return "\n".join(f"{marks[i['status']]} {i['text']}" for i in items)


DB_TOOL_NAMES = ["db_schema", "db_query"]
DB_PROMPT = (
    "Databases ({names}): call db_schema before writing SQL, then db_query with one statement. Reads are "
    "read-only and row-limited, so count, group and filter in SQL instead of fetching rows. A statement that "
    "changes data or structure is shown to the user for approval first; never work around a refusal."
)
MCP_TOOL_NAMES = ["mcp_tools", "mcp_call"]
MCP_PROMPT = (
    "External tools (MCP servers the user added). Only the names are listed here: call mcp_tools with a "
    "server and tool to read its arguments, then mcp_call. Prefer the built-in tools when both fit.\n{index}"
)
ARTIFACT_TOOL_NAMES = ["artifact_save", "artifact_get"]
SKILL_TOOL_NAMES = ["skill_use", "skill_save"]
# Tools a sub-agent may get even though no permission switch governs them.
UNGATED_TOOLS = {"memory_search", "skill_use"}

ROLE_TOOLS: dict[str, list[str]] = {
    "explore": READ_ONLY_TOOL_NAMES + ["memory_search", "skill_use"],
    "review": READ_ONLY_TOOL_NAMES + ["memory_search", "skill_use"],
    "code": [
        "read_file", "edit_file", "write_file", "list_files", "search_in_files",
        "create_directory", "run_command", "git_diff", "memory_search", "skill_use",
    ],
    "test": [
        "read_file", "edit_file", "write_file", "list_files", "search_in_files",
        "run_command", "memory_search", "skill_use",
    ],
}

# Roles that never modify the workspace — safe to run concurrently.
PARALLEL_SAFE_ROLES = {"explore", "review"}

ROLE_DESCRIPTIONS = {
    "explore": "find and read code, report facts with file:line refs (read-only)",
    "code": "implement one well-defined change",
    "review": "review code or a diff for bugs (read-only)",
    "test": "write and/or run tests, report results",
}

_ROLE_PROMPTS = {
    "explore": (
        "You are an exploration sub-agent. Investigate the codebase to answer the task. "
        "Do not modify anything. Read only what you need (use line ranges and search)."
    ),
    "review": (
        "You are a code-review sub-agent. Look for real bugs, regressions, security issues and "
        "missing tests. Do not modify anything. Be specific and skip style nits."
    ),
    "code": (
        "You are a coding sub-agent. Implement exactly the requested change with minimal edits. "
        "Read before editing; use edit_file with exact old_text. Verify (e.g. run tests) if cheap."
    ),
    "test": (
        "You are a testing sub-agent. Write or run tests as asked. Report pass/fail with the key "
        "error lines only."
    ),
}

INIT_PROMPT = (
    "{action} {file} in the project root: short instructions for coding agents working on this repository. "
    "First look around (use explore helpers on a big repo): README, build/package files, CI config, the main "
    "source folders.{extra} Then write at most ~60 lines of Markdown with: what the project is (1-2 lines); the "
    "exact commands to install, build, test (also a single test), lint and run it; the code layout (key folders "
    "and files); conventions that aren't obvious from the code; gotchas. Only facts you verified — no generic "
    "advice. Save it with write_file{keep}, then reply with a 2-line summary."
)

REPORT_RULES = (
    "You cannot ask the user questions. When finished, reply with a concise REPORT "
    "(under 200 words): findings or changes with file paths and line numbers, and anything unresolved."
)


def role_prompt(role: str, cwd: str, custom: str = "") -> str:
    base = custom.strip() or _ROLE_PROMPTS.get(role, _ROLE_PROMPTS["explore"])
    return f"{base}\nWorkspace: {cwd}\n{REPORT_RULES}"


MAIN_PROMPT = """You are Dcode, an expert coding agent in the user's terminal. Workspace: {cwd}

Rules:
- Use tools; never guess file contents. Read only the line ranges you need.
- Change files with edit_file (exact old_text). Use write_file only for new files.
- Keep replies short while working.
- When the task is done, ALWAYS end with a short report for the user:
  what you did, files changed, commands run and their results, and anything left to do."""

DELEGATION_PROMPT = """Your context is small (~{window} tokens), so delegate with the `delegate` tool:
{roles}
Sub-agents start empty: give each a complete, self-contained task (goal, relevant paths, constraints).
Run independent explore/review tasks in parallel by issuing several delegate calls in one reply.
Do small, obvious things yourself."""

ARTIFACT_PROMPT = (
    "Artifacts: put standalone deliverables the user will view (HTML pages/dashboards, SVG diagrams, "
    "Markdown reports, CSV tables, long code files) in artifact_save instead of the chat, then reply with "
    "a one-line summary. Never just say you will create it: call artifact_save in the same reply. "
    "HTML must be self-contained (inline CSS/JS, no internet). To revise, pass the same id. If the content is "
    "long, save a first part, then call artifact_save again with the same id and append=true."
)

SKILLS_PROMPT = (
    "Skills are playbooks. If one below fits the task, call skill_use first and follow it. "
    "When the user asks for a new skill, write it with skill_save."
)

MEMORY_PROMPT = (
    "Long-term memory: save durable facts (project conventions, architecture, user preferences, "
    "gotchas) with memory_save; look things up with memory_search. Don't save trivia."
)


def delegation_block(window: int, descriptions: dict[str, str] | None = None) -> str:
    roles = "\n".join(f"- {name}: {desc}" for name, desc in (descriptions or ROLE_DESCRIPTIONS).items())
    return DELEGATION_PROMPT.format(window=window, roles=roles)


EXTRA_TOOL_SCHEMAS = {
    "artifact_save": {
        "type": "function",
        "function": {
            "name": "artifact_save",
            "description": "Save a viewable artifact (page, diagram, report, table, code). Pass id to update one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": ["html", "markdown", "svg", "csv", "code"]},
                    "content": {"type": "string"},
                    "language": {"type": "string", "description": "for kind=code"},
                    "id": {"type": "string", "description": "existing artifact id to update"},
                    "append": {"type": "boolean", "description": "add content to the end of artifact `id` (build long ones in parts)"},
                },
                "required": ["title", "kind", "content"],
            },
        },
    },
    "db_schema": {
        "type": "function",
        "function": {
            "name": "db_schema",
            "description": "Tables of a saved database connection (sizes, columns, keys). Pass table for one "
                           "table's full detail.",
            "parameters": {
                "type": "object",
                "properties": {"connection": {"type": "string"}, "table": {"type": "string"}},
                "required": [],
            },
        },
    },
    "db_query": {
        "type": "function",
        "function": {
            "name": "db_query",
            "description": "Run one SQL statement on a saved connection. save_as writes all rows of a read to "
                           "a .csv or .jsonl file instead of returning them.",
            "parameters": {
                "type": "object",
                "properties": {"connection": {"type": "string"}, "sql": {"type": "string"},
                               "save_as": {"type": "string"}},
                "required": ["connection", "sql"],
            },
        },
    },
    "mcp_tools": {
        "type": "function",
        "function": {
            "name": "mcp_tools",
            "description": "Describe external (MCP) tools: a server's tool list, or one tool's arguments.",
            "parameters": {
                "type": "object",
                "properties": {"server": {"type": "string"}, "tool": {"type": "string"}},
                "required": [],
            },
        },
    },
    "mcp_call": {
        "type": "function",
        "function": {
            "name": "mcp_call",
            "description": "Run an external (MCP) tool. Read its arguments with mcp_tools first.",
            "parameters": {
                "type": "object",
                "properties": {"server": {"type": "string"}, "tool": {"type": "string"},
                               "arguments": {"type": "object"}},
                "required": ["server", "tool"],
            },
        },
    },
    "todo_write": {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": "Replace your task checklist (send every item each time).",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
                            },
                            "required": ["text", "status"],
                        },
                    },
                },
                "required": ["items"],
            },
        },
    },
    "ask_user": {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "Ask the user a question when a real ambiguity blocks you (not for permission). "
                           "Give 2-4 short options when you can.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}},
                "required": ["question"],
            },
        },
    },
    "job_status": {
        "type": "function",
        "function": {
            "name": "job_status",
            "description": "State and latest output of a background job (or all jobs if id is omitted).",
            "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": []},
        },
    },
    "job_stop": {
        "type": "function",
        "function": {
            "name": "job_stop",
            "description": "Stop a background job.",
            "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
        },
    },
    "artifact_get": {
        "type": "function",
        "function": {
            "name": "artifact_get",
            "description": "Get an artifact's current content by id (or list artifacts if id is empty).",
            "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": []},
        },
    },
    "skill_use": {
        "type": "function",
        "function": {
            "name": "skill_use",
            "description": "Load a skill's instructions by name.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        },
    },
    "skill_save": {
        "type": "function",
        "function": {
            "name": "skill_save",
            "description": "Create or update a reusable skill (Markdown playbook) for the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "short-kebab-name"},
                    "description": {"type": "string", "description": "one line: when to use it"},
                    "instructions": {"type": "string", "description": "Markdown bullet points"},
                },
                "required": ["name", "description", "instructions"],
            },
        },
    },
    "delegate": {
        "type": "function",
        "function": {
            "name": "delegate",
            "description": "Run a sub-agent with a fresh context on a self-contained task; returns its report.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "enum": list(ROLE_TOOLS)},
                    "task": {"type": "string", "description": "Complete instructions incl. paths and goal."},
                },
                "required": ["role", "task"],
            },
        },
    },
    "memory_save": {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": "Save a durable fact to long-term memory (overwrites same name).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "short-kebab-name"},
                    "content": {"type": "string"},
                    "scope": {"type": "string", "enum": ["project", "global"]},
                },
                "required": ["name", "content"],
            },
        },
    },
    "memory_search": {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": "Search long-term memory and past session summaries.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
}
