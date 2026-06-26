"""Load and parse Claude Skills (SKILL.md + bundled resources).

Anthropic's official Skill spec: https://agentskills.io
A Skill is a folder containing a SKILL.md with YAML frontmatter and a markdown
body, plus optional bundled resources (scripts, templates, references).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class SkillLoadError(ValueError):
    """Raised when a skill directory or SKILL.md is invalid."""


@dataclass(frozen=True)
class Skill:
    """A parsed Claude Skill.

    Attributes:
        name: Skill name from frontmatter.
        description: When Claude should load this skill (frontmatter description).
        body: The markdown body — the actual instructions + context.
        path: Absolute path to the skill directory.
        resources: Relative paths to bundled files (scripts, templates, etc).
        raw_frontmatter: Full original frontmatter dict for advanced use.
    """

    name: str
    description: str
    body: str
    path: Path
    resources: list[Path] = field(default_factory=list)
    raw_frontmatter: dict = field(default_factory=dict)

    def system_prompt_addition(self) -> str:
        """Format this skill for inclusion in a Claude system prompt.

        Matches the Anthropic spec: frontmatter description (always loaded)
        + body (loaded only when triggered). The host runtime decides when to
        call this — we just give it the right shape.
        """
        return (
            f"# Skill: {self.name}\n\n"
            f"{self.description}\n\n"
            f"---\n\n"
            f"{self.body.strip()}\n"
        )


_FRONTMATTER_RE = re.compile(
    r"\A\s*---\s*\n(?P<front>.*?)\n---\s*\n?(?P<body>.*)\Z",
    re.DOTALL,
)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter + markdown body. Returns (frontmatter, body)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        # No frontmatter — treat the whole thing as body, with name/description derived from H1.
        return {}, text
    try:
        frontmatter = yaml.safe_load(m.group("front")) or {}
    except yaml.YAMLError as e:
        raise SkillLoadError(f"Invalid YAML frontmatter: {e}") from e
    if not isinstance(frontmatter, dict):
        raise SkillLoadError(
            f"Frontmatter must be a YAML mapping, got {type(frontmatter).__name__}"
        )
    return frontmatter, m.group("body").strip()


def load_skill(path: str | Path) -> Skill:
    """Load a Skill from a directory containing SKILL.md.

    Args:
        path: Directory containing SKILL.md. Or a direct path to SKILL.md.

    Returns:
        A parsed ``Skill`` instance.

    Raises:
        SkillLoadError: if the path is invalid, missing SKILL.md, or has bad frontmatter.
    """
    path = Path(path).expanduser().resolve()
    skill_md = path if path.is_file() else path / "SKILL.md"
    if not skill_md.is_file():
        raise SkillLoadError(f"SKILL.md not found at {skill_md}")

    text = skill_md.read_text(encoding="utf-8")
    frontmatter, body = _parse_frontmatter(text)

    name = frontmatter.get("name") or path.parent.name if path.is_file() else path.name
    description = frontmatter.get("description", "").strip()
    if not description:
        # Fall back to first H1 in the body — better than nothing.
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                description = stripped[2:].strip()
                break
    if not description:
        raise SkillLoadError(
            f"Skill {name!r} has no 'description' in frontmatter and no H1 in body — "
            "Claude needs it to decide when to trigger."
        )

    # Collect bundled resources (everything else in the directory)
    resources: list[Path] = []
    skill_dir = skill_md.parent
    for child in sorted(skill_dir.iterdir()):
        if child.name == "SKILL.md" or child.name.startswith("."):
            continue
        if child.is_file():
            resources.append(child)

    return Skill(
        name=name,
        description=description,
        body=body,
        path=skill_dir,
        resources=resources,
        raw_frontmatter=frontmatter,
    )