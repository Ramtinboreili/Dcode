"""Artifact store: standalone deliverables Dcode makes for you to view.

HTML pages, SVG diagrams, Markdown documents, CSV tables and code files are
saved here (``~/.dcode/artifacts/<id>.json``) instead of being pasted into the
chat. The web UI lists and previews them; HTML runs in a sandboxed iframe with
no network access. Each save of an existing id keeps the previous version.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import home_dir, write_private_json

KINDS = ("html", "markdown", "svg", "csv", "code")
MAX_VERSIONS = 10
MAX_CONTENT = 2_000_000
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_EXTENSIONS = {"html": "html", "markdown": "md", "svg": "svg", "csv": "csv"}


def artifacts_dir() -> Path:
    path = home_dir() / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def valid_id(artifact_id: str) -> bool:
    return bool(_ID_RE.match(artifact_id or ""))


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40].strip("-") or "artifact"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def guess_kind(kind: str, content: str) -> str:
    kind = (kind or "").lower().strip()
    aliases = {"md": "markdown", "doc": "markdown", "document": "markdown", "page": "html", "web": "html",
               "diagram": "svg", "table": "csv"}
    kind = aliases.get(kind, kind)
    if kind in KINDS:
        return kind
    head = content.lstrip()[:200].lower()
    if head.startswith(("<!doctype html", "<html")):
        return "html"
    if head.startswith("<svg"):
        return "svg"
    return "code"


def filename_for(meta: dict[str, Any]) -> str:
    ext = _EXTENSIONS.get(meta.get("kind", ""), "") or (meta.get("language") or "txt")
    return f"{meta['id']}.{re.sub(r'[^a-z0-9]', '', ext.lower()) or 'txt'}"


class ArtifactStore:
    def _path(self, artifact_id: str) -> Path:
        if not valid_id(artifact_id):
            raise ValueError(f"invalid artifact id: {artifact_id!r}")
        return artifacts_dir() / f"{artifact_id}.json"

    def save(
        self,
        title: str,
        content: str,
        kind: str = "",
        language: str = "",
        artifact_id: str | None = None,
        cwd: str = "",
        thread: str = "",
    ) -> dict[str, Any]:
        content = str(content or "")
        if not content.strip():
            raise ValueError("artifact content is empty")
        if len(content) > MAX_CONTENT:
            raise ValueError(f"artifact is too large ({len(content)} chars, limit {MAX_CONTENT})")
        existing = self.get(artifact_id) if artifact_id and valid_id(artifact_id) else None
        now = _now()
        if existing:
            history = existing.get("history") or []
            history.append({"content": existing["content"], "updated": existing["updated"]})
            record = dict(existing)
            record.update(
                title=title or existing["title"],
                kind=guess_kind(kind or existing["kind"], content),
                language=language or existing.get("language", ""),
                content=content,
                updated=now,
                history=history[-MAX_VERSIONS:],
            )
        else:
            new_id = f"{_slug(title)}-{secrets.token_hex(3)}"
            record = {
                "id": new_id, "title": title.strip() or "Untitled", "kind": guess_kind(kind, content),
                "language": language, "content": content, "created": now, "updated": now,
                "cwd": cwd, "thread": thread, "history": [],
            }
        write_private_json(self._path(record["id"]), record)
        return self.meta(record)

    @staticmethod
    def meta(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": record["id"], "title": record["title"], "kind": record["kind"],
            "language": record.get("language", ""), "created": record.get("created", ""),
            "updated": record.get("updated", ""), "cwd": record.get("cwd", ""), "thread": record.get("thread", ""),
            "size": len(record.get("content", "")), "versions": len(record.get("history") or []) + 1,
        }

    def get(self, artifact_id: str) -> dict[str, Any] | None:
        try:
            path = self._path(artifact_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def list(self) -> list[dict[str, Any]]:
        items = []
        for path in artifacts_dir().glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and "id" in record:
                items.append(self.meta(record))
        items.sort(key=lambda m: m.get("updated", ""), reverse=True)
        return items

    def versions(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        """Version list, oldest first; the last entry is the current content."""
        history = record.get("history") or []
        items = [{"version": i + 1, "updated": h.get("updated", ""), "size": len(h.get("content", ""))}
                 for i, h in enumerate(history)]
        items.append({"version": len(history) + 1, "updated": record.get("updated", ""),
                      "size": len(record.get("content", "")), "current": True})
        return items

    def content_at(self, record: dict[str, Any], version: int) -> str | None:
        history = record.get("history") or []
        if 1 <= version <= len(history):
            return history[version - 1].get("content", "")
        if version == len(history) + 1:
            return record.get("content", "")
        return None

    def rename(self, artifact_id: str, title: str) -> dict[str, Any]:
        record = self.get(artifact_id)
        if record is None:
            raise ValueError("artifact not found")
        record["title"] = title.strip() or record["title"]
        write_private_json(self._path(artifact_id), record)
        return self.meta(record)

    def restore(self, artifact_id: str, version: int) -> dict[str, Any]:
        record = self.get(artifact_id)
        if record is None:
            raise ValueError("artifact not found")
        content = self.content_at(record, version)
        if content is None:
            raise ValueError(f"no version {version}")
        return self.save(record["title"], content, artifact_id=artifact_id)

    def duplicate(self, artifact_id: str) -> dict[str, Any]:
        record = self.get(artifact_id)
        if record is None:
            raise ValueError("artifact not found")
        return self.save(f"{record['title']} (copy)", record["content"], kind=record["kind"],
                         language=record.get("language", ""), cwd=record.get("cwd", ""), thread=record.get("thread", ""))

    def delete(self, artifact_id: str) -> bool:
        try:
            path = self._path(artifact_id)
        except ValueError:
            return False
        if path.exists():
            path.unlink()
            return True
        return False
