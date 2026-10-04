"""File checkpoints so `/undo` can roll back what Dcode changed — git or not.

Before any tool modifies a file, the original bytes (or "did not exist") are
recorded under the current task's checkpoint. Undo restores the newest
checkpoint. Checkpoints live in memory for the session only.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

MAX_CHECKPOINTS = 30
MAX_FILE_BYTES = 5 * 1024 * 1024  # don't snapshot huge files


@dataclass
class Checkpoint:
    label: str
    files: dict[Path, bytes | None] = field(default_factory=dict)  # None = file did not exist


class CheckpointStore:
    def __init__(self) -> None:
        self._stack: list[Checkpoint] = []
        self._lock = threading.Lock()

    def begin(self, label: str) -> None:
        with self._lock:
            # Drop empty checkpoints so /undo always targets a task that changed something.
            if self._stack and not self._stack[-1].files:
                self._stack.pop()
            self._stack.append(Checkpoint(label=label.strip()[:80] or "task"))
            del self._stack[:-MAX_CHECKPOINTS]

    def record(self, path: Path) -> None:
        """Remember ``path``'s current state the first time a task touches it."""
        with self._lock:
            if not self._stack:
                self._stack.append(Checkpoint(label="task"))
            current = self._stack[-1]
            if path in current.files:
                return
            if path.is_file():
                if path.stat().st_size > MAX_FILE_BYTES:
                    return
                current.files[path] = path.read_bytes()
            elif not path.exists():
                current.files[path] = None

    def latest(self) -> Checkpoint | None:
        with self._lock:
            for checkpoint in reversed(self._stack):
                if checkpoint.files:
                    return checkpoint
        return None

    def undo(self) -> tuple[str, list[str]] | None:
        """Restore the newest non-empty checkpoint. Returns (label, restored paths)."""
        with self._lock:
            while self._stack and not self._stack[-1].files:
                self._stack.pop()
            if not self._stack:
                return None
            checkpoint = self._stack.pop()
        restored = []
        for path, original in checkpoint.files.items():
            try:
                if original is None:
                    if path.is_file():
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(original)
                restored.append(str(path))
            except OSError:
                continue
        return checkpoint.label, restored
