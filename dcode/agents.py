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

REPORT_RULES = (
    "You cannot ask the user questions. When finished, reply with a concise REPORT "
    "(under 200 words): findings or changes with file paths and line numbers, and anything unresolved."
)


def role_prompt(role: str, cwd: str) -> str:
    return f"{_ROLE_PROMPTS.get(role, _ROLE_PROMPTS['explore'])}\nWorkspace: {cwd}\n{REPORT_RULES}"


MAIN_PROMPT = """You are Dcode, an expert coding agent in the user's terminal. Workspace: {cwd}

Rules:
- Use tools; never guess file contents. Read only the line ranges you need.
- Change files with edit_file (exact old_text). Use write_file only for new files.
- Keep replies short. When finished, state what changed and how it was verified."""

DELEGATION_PROMPT = """Your context is small (~{window} tokens), so delegate with the `delegate` tool:
{roles}
Sub-agents start empty: give each a complete, self-contained task (goal, relevant paths, constraints).
Run independent explore/review tasks in parallel by issuing several delegate calls in one reply.
Do small, obvious things yourself."""

ARTIFACT_PROMPT = (
    "Artifacts: put standalone deliverables the user will view (HTML pages/dashboards, SVG diagrams, "
    "Markdown reports, CSV tables, long code files) in artifact_save instead of the chat, then reply with "
    "a one-line summary. HTML must be self-contained (inline CSS/JS, no internet). To revise, pass the same id."
)

SKILLS_PROMPT = (
    "Skills are playbooks. If one below fits the task, call skill_use first and follow it. "
    "When the user asks for a new skill, write it with skill_save."
)

MEMORY_PROMPT = (
    "Long-term memory: save durable facts (project conventions, architecture, user preferences, "
    "gotchas) with memory_save; look things up with memory_search. Don't save trivia."
)


def delegation_block(window: int) -> str:
    roles = "\n".join(f"- {name}: {desc}" for name, desc in ROLE_DESCRIPTIONS.items())
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
                },
                "required": ["title", "kind", "content"],
            },
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
