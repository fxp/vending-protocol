"""Load UCP Agent Skills from agentic-commerce-skills/skills/."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import settings


@dataclass
class Skill:
    name: str
    stage: str
    description: str
    body: str
    path: Path


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw, body = m.group(1), m.group(2)
    meta: dict = {}
    key = None
    for line in raw.splitlines():
        if re.match(r"^[A-Za-z_]+:", line):
            key, _, val = line.partition(":")
            key = key.strip()
            meta[key] = val.strip().strip(">").strip()
        elif key and line.strip():
            meta[key] = (meta.get(key, "") + " " + line.strip()).strip()
    return meta, body


def _stage_from_path(path: Path) -> str:
    for part in path.parts:
        m = re.match(r"^\d+-(.+)$", part)
        if m:
            return m.group(1)
    return "general"


def load_skills() -> list[Skill]:
    skills_dir = settings.skills_dir
    skills: list[Skill] = []
    if not skills_dir.exists():
        return skills
    for skill_md in sorted(skills_dir.rglob("SKILL.md")):
        if " 2/" in str(skill_md):
            continue
        text = skill_md.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)
        skills.append(
            Skill(
                name=meta.get("name", skill_md.parent.name),
                stage=_stage_from_path(skill_md),
                description=meta.get("description", ""),
                body=body.strip(),
                path=skill_md,
            )
        )
    return skills


def skills_catalog(skills: list[Skill]) -> str:
    lines = []
    for s in skills:
        desc = " ".join(s.description.split())
        if len(desc) > 240:
            desc = desc[:237] + "..."
        lines.append(f"- **{s.name}** [{s.stage}] — {desc}")
    return "\n".join(lines)
