"""Long-term memory for Dcode.

Memory architecture (cheapest → most durable):

* **Working memory** – the live message list, kept inside the context budget
  by :mod:`dcode.context` (elision + summarization).
* **Episodic memory** – a summary of each session, written when history is
  compacted or a thread is closed. Stored as ``session-*`` memories so later
  sessions can recall what happened before.
* **Semantic memory** – durable facts the agent (or the user, via the UI)
  saves explicitly: conventions, architecture notes, gotchas. Two scopes:
  ``project`` (per workspace) and ``global`` (per user).

Everything is plain Markdown with a small front-matter header, so users can
read / edit it by hand. Retrieval is BM25 over name + description + tags +
body — fully offline, no embedding model needed.
"""

from __future__ import annotations

import builtins
import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import memory_dir

SCOPES = ("project", "global")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "is", "it", "this", "that",
    "with", "as", "be", "are", "was", "at", "by", "from", "i", "you", "we", "my", "me", "do",
    "can", "please", "make", "use", "file", "code",
}


def _tokens(text: str) -> list[str]:
    out = []
    for raw in _TOKEN_RE.findall(text.lower()):
        # Split snake_case / dotted identifiers into parts as well.
        parts = [raw] + [p for p in raw.split("_") if p and p != raw]
        out.extend(p for p in parts if p not in _STOPWORDS and len(p) > 1)
    return out


def slugify(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", name.strip().lower()).strip("-")
    return cleaned[:80] or "note"


def project_key(root: Path) -> str:
    resolved = root.expanduser().resolve()
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:8]
    return f"{slugify(resolved.name or 'root')}-{digest}"


@dataclass
class Memory:
    name: str
    scope: str
    body: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    updated: str = ""
    path: Path | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "scope": self.scope,
            "description": self.description,
            "tags": list(self.tags),
            "updated": self.updated,
            "body": self.body,
        }


def _parse(path: Path, scope: str) -> Memory | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    meta[key.strip()] = value.strip()
            body = text[end + 4:].lstrip("\n")
    tags = [t.strip() for t in meta.get("tags", "").split(",") if t.strip()]
    return Memory(
        name=meta.get("name") or path.stem,
        scope=scope,
        body=body.strip(),
        description=meta.get("description", ""),
        tags=tags,
        updated=meta.get("updated", ""),
        path=path,
    )


def write_memory(
    folder: Path,
    scope: str,
    name: str,
    body: str,
    description: str = "",
    tags: list[str] | None = None,
) -> Memory:
    """Write one memory file into ``folder`` (front-matter + Markdown body)."""
    folder.mkdir(parents=True, exist_ok=True)
    slug = slugify(name)
    path = folder / f"{slug}.md"
    if not description and body.strip():
        description = body.strip().splitlines()[0]
    description = description.replace("\n", " ")[:160]
    tags = [t.strip() for t in (tags or []) if t and t.strip()]
    updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    header = (
        "---\n"
        f"name: {slug}\n"
        f"description: {description}\n"
        f"tags: {', '.join(tags)}\n"
        f"updated: {updated}\n"
        "---\n\n"
    )
    path.write_text(header + body.strip() + "\n", encoding="utf-8")
    return Memory(slug, scope, body.strip(), description, tags, updated, path)


class MemoryStore:
    def __init__(self, project_root: Path | None = None) -> None:
        self.project_root = (project_root or Path.cwd()).expanduser().resolve()

    # --- locations --------------------------------------------------------------
    def scope_dir(self, scope: str) -> Path:
        if scope == "global":
            path = memory_dir() / "global"
        else:
            path = memory_dir() / "projects" / project_key(self.project_root)
        path.mkdir(parents=True, exist_ok=True)
        return path

    # --- CRUD -------------------------------------------------------------------
    def save(
        self,
        name: str,
        body: str,
        description: str = "",
        scope: str = "project",
        tags: builtins.list[str] | None = None,
    ) -> Memory:
        scope = scope if scope in SCOPES else "project"
        return write_memory(self.scope_dir(scope), scope, name, body, description, tags)

    def delete(self, name: str, scope: str = "project") -> bool:
        path = self.scope_dir(scope) / f"{slugify(name)}.md"
        if path.exists():
            path.unlink()
            return True
        return False

    def get(self, name: str, scope: str = "project") -> Memory | None:
        path = self.scope_dir(scope) / f"{slugify(name)}.md"
        return _parse(path, scope) if path.exists() else None

    def list(self, scope: str | None = None) -> builtins.list[Memory]:
        scopes = [scope] if scope in SCOPES else list(SCOPES)
        items: list[Memory] = []
        for current in scopes:
            for path in self.scope_dir(current).glob("*.md"):
                parsed = _parse(path, current)
                if parsed:
                    items.append(parsed)
        items.sort(key=lambda m: m.updated, reverse=True)
        return items

    # --- retrieval ----------------------------------------------------------------
    def search(self, query: str, k: int = 5, include_sessions: bool = True) -> builtins.list[Memory]:
        docs = [m for m in self.list() if include_sessions or "session" not in m.tags]
        query_terms = _tokens(query)
        if not docs or not query_terms:
            return []
        doc_terms = [
            _tokens(f"{m.name} {m.name} {m.description} {m.description} {' '.join(m.tags)} {m.body}") for m in docs
        ]
        avg_len = sum(len(t) for t in doc_terms) / len(doc_terms) or 1.0
        df: Counter = Counter()
        for terms in doc_terms:
            df.update(set(terms))
        n = len(docs)
        k1, b = 1.4, 0.75
        scored = []
        for memory, terms in zip(docs, doc_terms, strict=True):
            tf = Counter(terms)
            score = 0.0
            for term in set(query_terms):
                if term not in tf:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                freq = tf[term]
                score += idf * freq * (k1 + 1) / (freq + k1 * (1 - b + b * len(terms) / avg_len))
            if score > 0:
                scored.append((score, memory))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [memory for _, memory in scored[:k]]

    def index_text(self, max_chars: int) -> str:
        """Compact 'name — description' index for the system prompt."""
        lines: list[str] = []
        used = 0
        for memory in self.list():
            if "session" in memory.tags:
                continue
            line = f"- [{memory.scope}] {memory.name}: {memory.description}"
            if used + len(line) > max_chars:
                lines.append("- ... (more; use memory_search)")
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    def recall(self, query: str, max_chars: int) -> str:
        """Bodies of the memories most relevant to ``query``, within a budget."""
        chunks: list[str] = []
        used = 0
        for memory in self.search(query, k=4):
            body = memory.body
            room = max_chars - used
            if room < 200:
                break
            if len(body) > room:
                body = body[: room - 20] + " ..."
            chunk = f"### {memory.name} ({memory.scope})\n{body}"
            chunks.append(chunk)
            used += len(chunk)
        return "\n\n".join(chunks)

    def save_session_summary(self, thread_name: str, summary: str) -> Memory | None:
        if not summary.strip():
            return None
        return self.save(
            name=f"session-{thread_name}",
            body=summary,
            description=f"Summary of session {thread_name}",
            scope="project",
            tags=["session"],
        )
