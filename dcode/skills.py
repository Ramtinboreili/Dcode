"""Skills: short Markdown playbooks that shape how Dcode works.

* Built-in skills ship with Dcode (``dcode/skills/*.md``) and are copied once
  into ``~/.dcode/skills`` so you can edit them. Deleting one sticks.
* You can add your own: drop a ``.md`` file in ``~/.dcode/skills`` (or in a
  project's ``.dcode/skills`` folder), write one in the web UI, or ask Dcode
  to create one (``skill_save`` tool).
* Pinned skills are always in the prompt. All others are listed by name and
  description only, and the model loads one with ``skill_use`` when it fits —
  cheap enough for 32K models.

Format::

    ---
    name: devops
    description: One line saying when to use it.
    ---
    # DevOps
    - instructions...
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import skills_dir

BUILTIN_DIR = Path(__file__).resolve().parent / "skills"
_SEEDED_MARKER = ".builtin-seeded"
_NAME_RE = re.compile(r"[^a-z0-9-]+")


@dataclass
class Skill:
    name: str
    description: str
    content: str
    path: Path
    scope: str  # "user" or "project"
    builtin: bool = False

    @property
    def filename(self) -> str:
        return self.path.name


def normalize_name(name: str) -> str:
    return _NAME_RE.sub("-", name.lower().removesuffix(".md")).strip("-")[:60] or "skill"


def seed_builtin_skills() -> None:
    """Copy built-in skills the user hasn't seen yet (never overwrites, never resurrects)."""
    target = skills_dir()
    if not BUILTIN_DIR.is_dir():
        return
    marker = target / _SEEDED_MARKER
    try:
        seeded = set(marker.read_text().split()) if marker.exists() else set()
    except OSError:
        seeded = set()
    changed = False
    for source in sorted(BUILTIN_DIR.glob("*.md")):
        if source.name in seeded:
            continue
        destination = target / source.name
        if not destination.exists():
            try:
                shutil.copyfile(source, destination)
            except OSError:
                continue
        seeded.add(source.name)
        changed = True
    if changed:
        try:
            marker.write_text("\n".join(sorted(seeded)) + "\n")
        except OSError:
            pass


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split ``---``-delimited ``key: value`` lines (skills, agents, commands) from the body."""
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---\n") or text.startswith("---\r\n"):
        text = text.replace("\r\n", "\n")
        end = text.find("\n---", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                key, sep, value = line.partition(":")
                if sep:
                    meta[key.strip().lower()] = value.strip().strip("\"'")
            body = text[end + 4:].lstrip("\n")
    return meta, body


def _parse(path: Path, scope: str) -> Skill | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, body = parse_frontmatter(text)
    description = meta.get("description", "")
    if not description:
        for line in body.splitlines():
            line = line.strip().lstrip("-*# ").strip()
            if line:
                description = line[:140]
                break
    return Skill(
        name=normalize_name(meta.get("name") or path.stem),
        description=description,
        content=body.strip(),
        path=path,
        scope=scope,
        builtin=(BUILTIN_DIR / path.name).exists(),
    )


def list_skills(workspace: Path | None = None) -> list[Skill]:
    """User skills plus project skills (project wins on name clashes)."""
    found: dict[str, Skill] = {}
    sources = [(skills_dir(), "user")]
    if workspace is not None:
        sources.append((workspace / ".dcode" / "skills", "project"))
    for folder, scope in sources:
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            skill = _parse(path, scope)
            if skill:
                found[skill.name] = skill
    return sorted(found.values(), key=lambda s: s.name)


def get_skill(name: str, workspace: Path | None = None) -> Skill | None:
    wanted = normalize_name(name)
    return next((s for s in list_skills(workspace) if s.name == wanted), None)


def save_skill(name: str, description: str, instructions: str, folder: Path | None = None) -> Skill:
    slug = normalize_name(name)
    folder = folder or skills_dir()
    folder.mkdir(parents=True, exist_ok=True)
    body = instructions.strip()
    if not body.startswith("#"):
        body = f"# {slug.replace('-', ' ').title()}\n\n{body}"
    text = f"---\nname: {slug}\ndescription: {' '.join(description.split())[:200]}\n---\n{body}\n"
    path = folder / f"{slug}.md"
    path.write_text(text, encoding="utf-8")
    skill = _parse(path, "user")
    assert skill is not None
    return skill
