"""CLI for paid-skills — quick way to test a skill from the terminal."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .pricing import calculate_price, format_price
from .runtime import PaidSkillRuntime, SkillInvocation
from .skill_loader import load_skill


def main() -> None:
    p = argparse.ArgumentParser(description="Invoke a paid Claude Skill from the CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    inv = sub.add_parser("invoke", help="Load a skill and call Claude")
    inv.add_argument("skill_path", help="Path to skill directory or SKILL.md")
    inv.add_argument("message", help="User message")
    inv.add_argument("--model", default="claude-sonnet-4-6")
    inv.add_argument("--max-tokens", type=int, default=1024)
    inv.add_argument("--per-call-usd", type=float, default=0.01)
    inv.add_argument("--cost-per-1k", type=float, default=0.018)

    inspect = sub.add_parser("inspect", help="Show frontmatter + body length of a skill")
    inspect.add_argument("skill_path")

    args = p.parse_args()

    if args.cmd == "inspect":
        skill = load_skill(args.skill_path)
        print(json.dumps({
            "name": skill.name,
            "description": skill.description,
            "body_chars": len(skill.body),
            "resources": [r.name for r in skill.resources],
            "frontmatter": skill.raw_frontmatter,
        }, indent=2))
        return

    if args.cmd == "invoke":
        skill = load_skill(args.skill_path)
        runtime = PaidSkillRuntime(
            per_call_usd=args.per_call_usd,
            cost_per_1k_tokens_usd=args.cost_per_1k,
        )
        invocation = SkillInvocation(
            skill=skill,
            user_message=args.message,
            model=args.model,
            max_tokens=args.max_tokens,
        )
        result = runtime.invoke(invocation)
        print(f"\n{result.text}\n")
        print("--- billing ---")
        print(f"input_tokens: {result.input_tokens}")
        print(f"output_tokens: {result.output_tokens}")
        print(f"duration_ms: {result.duration_ms}")
        print(f"total_cost_usd: ${result.quote.total_usd:.4f}")
        print(f"invoice: {format_price(result.quote)}")
        print(f"invocation_id: {result.invocation_id}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)