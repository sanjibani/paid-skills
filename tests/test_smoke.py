"""Tests for paid-skills. No live Anthropic API calls."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from paid_skills.pricing import calculate_price, format_price
from paid_skills.runtime import (
    InMemoryMeter,
    PaidSkillRuntime,
    PaymentRequired,
    SkillInvocation,
    UsageRecord,
)
from paid_skills.skill_loader import Skill, SkillLoadError, load_skill


# ----- skill_loader ---------------------------------------------------------


def test_load_skill_minimal(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: A test skill.\n---\n\nBody text.\n",
        encoding="utf-8",
    )
    skill = load_skill(skill_dir)
    assert skill.name == "my-skill"
    assert skill.description == "A test skill."
    assert "Body text." in skill.body
    assert skill.resources == []


def test_load_skill_with_resources(tmp_path: Path) -> None:
    skill_dir = tmp_path / "with-resources"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: with-resources\ndescription: Test.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (skill_dir / "script.py").write_text("print('hi')", encoding="utf-8")
    (skill_dir / "template.md").write_text("# Tmpl", encoding="utf-8")
    skill = load_skill(skill_dir)
    resource_names = {r.name for r in skill.resources}
    assert resource_names == {"script.py", "template.md"}


def test_load_skill_direct_path(tmp_path: Path) -> None:
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text(
        "---\nname: direct\ndescription: Direct path.\n---\n\nbody\n",
        encoding="utf-8",
    )
    skill = load_skill(skill_md)
    assert skill.name == "direct"


def test_load_skill_no_frontmatter_uses_filename(tmp_path: Path) -> None:
    skill_dir = tmp_path / "no-fm"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# No Frontmatter\n\nbody\n", encoding="utf-8")
    skill = load_skill(skill_dir)
    assert skill.name == "no-fm"
    assert skill.body.startswith("# No Frontmatter")


def test_load_skill_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SkillLoadError):
        load_skill(tmp_path / "does-not-exist")


def test_load_skill_missing_description(tmp_path: Path) -> None:
    skill_dir = tmp_path / "no-desc"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: no-desc\n---\n\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(SkillLoadError, match="description"):
        load_skill(skill_dir)


def test_skill_system_prompt_shape(tmp_path: Path) -> None:
    skill_dir = tmp_path / "shape"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: shape\ndescription: A test skill.\n---\n\nThe actual instructions.\n",
        encoding="utf-8",
    )
    skill = load_skill(skill_dir)
    prompt = skill.system_prompt_addition()
    assert prompt.startswith("# Skill: shape")
    assert "A test skill." in prompt
    assert "The actual instructions." in prompt


# ----- pricing --------------------------------------------------------------


def test_calculate_price_basic() -> None:
    quote = calculate_price(input_tokens=1000, output_tokens=500)
    # 1.5K tokens × $0.018/1K × 1.30 margin = $0.0351
    # + $0.01 per-call = $0.0451
    assert quote.total_usd == pytest.approx(0.0451, rel=1e-3)
    assert quote.token_cost_usd == pytest.approx(0.0351, rel=1e-3)


def test_calculate_price_zero_tokens() -> None:
    quote = calculate_price(input_tokens=0, output_tokens=0)
    assert quote.total_usd == 0.01  # only per-call fee


def test_format_price() -> None:
    quote = calculate_price(input_tokens=1000, output_tokens=500)
    out = format_price(quote)
    assert "per-call" in out
    assert "tokens" in out


# ----- runtime --------------------------------------------------------------


def _fake_response(text: str = "Hello", input_tokens: int = 100, output_tokens: int = 50) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    resp.model = "claude-sonnet-4-6"
    resp.usage.input_tokens = input_tokens
    resp.usage.output_tokens = output_tokens
    return resp


def test_runtime_invoke_records_meter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake_client = MagicMock()
    fake_client.messages.create = MagicMock(return_value=_fake_response())
    runtime = PaidSkillRuntime(client=fake_client)
    meter = InMemoryMeter()
    runtime.meter = meter

    skill = Skill(
        name="test",
        description="A test.",
        body="body",
        path=Path("/tmp"),
    )
    result = runtime.invoke(
        SkillInvocation(skill=skill, user_message="hi", metadata={"user_id": "u1"})
    )
    assert result.text == "Hello"
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert len(meter.records) == 1
    rec = meter.records[0]
    assert rec.skill_name == "test"
    assert rec.metadata["user_id"] == "u1"
    assert rec.total_cost_usd > 0


def test_runtime_payment_hook_called(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake_client = MagicMock()
    fake_client.messages.create = MagicMock(return_value=_fake_response())
    runtime = PaidSkillRuntime(client=fake_client)

    before_called = []
    after_called = []

    class FakeHook:
        def before_invocation(self, inv: SkillInvocation) -> None:
            before_called.append(inv)

        def after_invocation(self, inv: SkillInvocation, result: object) -> None:
            after_called.append((inv, result))

    runtime.payment_hook = FakeHook()  # type: ignore[assignment]
    skill = Skill(name="t", description="d", body="b", path=Path("/tmp"))
    result = runtime.invoke(SkillInvocation(skill=skill, user_message="hi"))
    assert len(before_called) == 1
    assert len(after_called) == 1
    assert after_called[0][1] is result


def test_runtime_payment_required_bubbles_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake_client = MagicMock()
    runtime = PaidSkillRuntime(client=fake_client)

    class BlockingHook:
        def before_invocation(self, inv: SkillInvocation) -> None:
            raise PaymentRequired("no credit")

    runtime.payment_hook = BlockingHook()  # type: ignore[assignment]
    skill = Skill(name="t", description="d", body="b", path=Path("/tmp"))
    with pytest.raises(PaymentRequired):
        runtime.invoke(SkillInvocation(skill=skill, user_message="hi"))
    # Claude should NOT be called when payment fails
    fake_client.messages.create.assert_not_called()


def test_runtime_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="API key"):
        PaidSkillRuntime()


# ----- example skills --------------------------------------------------------


def test_summarize_url_example_loads() -> None:
    examples_dir = Path(__file__).parent.parent / "examples" / "summarize-url"
    skill = load_skill(examples_dir)
    assert skill.name == "summarize-url"
    assert "summarize" in skill.description.lower()
    assert "fetch_url" in skill.body


def test_contract_review_example_loads() -> None:
    examples_dir = Path(__file__).parent.parent / "examples" / "contract-review"
    skill = load_skill(examples_dir)
    assert skill.name == "contract-review"
    assert "redline" in skill.body.lower() or "negoti" in skill.body.lower()


# ----- JSON smoke for the package -------------------------------------------


def test_package_metadata() -> None:
    import paid_skills
    assert paid_skills.__version__ == "0.1.0"
    assert "load_skill" in paid_skills.__all__
    assert "PaidSkillRuntime" in paid_skills.__all__