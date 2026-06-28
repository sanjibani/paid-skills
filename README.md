# paid-skills

**Open-source runtime for monetizing Claude Skills.**

Load a SKILL.md, call Anthropic, meter invocations, hook into Stripe. Deploy to Cloudflare Workers, Fly.io, AWS Lambda, or your own server. No vendor lock-in — your skills, your prices, your customer data.

Built around the [open Agent Skills standard](https://agentskills.io).

---

## What this is

Every existing Claude Skill monetization platform (Anthropic Marketplace, Coze, Capafy, Agent37, Agensi, SkillHQ) does the same thing: you upload a SKILL.md, they host it, they charge users, they keep a cut.

**They all lock you into their DB and their payment processor.** When you want to leave — or when you want to bundle your skill with your own product — you're stuck.

`paid-skills` is the missing piece: **a small, portable runtime that does the execution + metering, and a clean protocol for plugging in any payment backend**. The runtime is MIT-licensed, your skills are yours, your customer relationships are yours.

## What this isn't

- Not a marketplace. You bring the storefront (or sell through your own SaaS).
- Not a payment processor. We provide hooks for Stripe / Lightning / USDC; you wire it up.
- Not a SaaS. There's no hosted version. You run it.

## Install

```bash
pip install paid-skills
```

Or clone + dev install:

```bash
git clone https://github.com/sanjibani/paid-skills
cd paid-skills
pip install -e ".[dev]"
```

## Quick start

### 1. Write a Skill (one file)

`skills/summarize-url/SKILL.md`:

```markdown
---
name: summarize-url
description: Fetches a public URL and returns a 3-bullet executive summary. Use when the user pastes a link and asks for a summary or TLDR.
---

You are a precise executive-summary assistant. When the user provides a URL:
1. Acknowledge the URL briefly.
2. Call the `fetch_url` tool to retrieve content.
3. Produce a 3-bullet summary: bottom line, key facts, why it matters.
4. Never invent content. If the URL is unreachable, say so.
```

### 2. Run it from the CLI

```bash
export ANTHROPIC_API_KEY=sk-ant-...
paid-skills invoke ./skills/summarize-url "Summarize https://example.com/blog/post"
```

```
[3-bullet summary here]

--- billing ---
input_tokens: 482
output_tokens: 173
duration_ms: 1842
total_cost_usd: $0.0334
invoice: $0.0100 per-call + $0.0234 tokens @ $0.018/1k × 655 tokens
invocation_id: 8f1c4a72-...
```

### 3. Run it from Python

```python
from paid_skills import PaidSkillRuntime, SkillInvocation, load_skill

runtime = PaidSkillRuntime(per_call_usd=0.05, cost_per_1k_tokens_usd=0.018)
skill = load_skill("./skills/summarize-url")

result = runtime.invoke(SkillInvocation(
    skill=skill,
    user_message="Summarize https://anthropic.com/news/claude-skills",
    metadata={"user_id": "u_42", "channel": "web"},
))
print(result.text)
print(f"charged: ${result.quote.total_usd:.4f}")
```

### 4. Run it as an HTTP service

```bash
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn paid_skills.server:app --host 0.0.0.0 --port 8000
```

```bash
# Health check
curl localhost:8000/health

# Inspect a skill
curl localhost:8000/skills/./skills/summarize-url

# Get a price quote
curl -X POST localhost:8000/quote \
  -H "Content-Type: application/json" \
  -d '{"skill_path": "./skills/summarize-url", "user_message": "Summarize https://x.com"}'

# Invoke a skill (charges the per-call + token fees)
curl -X POST localhost:8000/invoke \
  -H "Content-Type: application/json" \
  -d '{"skill_path": "./skills/summarize-url", "user_message": "Summarize https://x.com"}'
```

## Pricing model

Two knobs:

| Knob | Default | What it does |
| --- | --- | --- |
| `per_call_usd` | 0.01 | Flat fee per invocation |
| `cost_per_1k_tokens_usd` | 0.018 | Pass-through rate per 1K tokens (Anthropic model rate) |
| `token_margin` | 0.30 | Markup on token cost (30% margin) |

Override per-deployment, per-skill, or per-tier. Charge premium for slow models (Opus) vs cheap tier (Haiku). It's pure math, no I/O.

```python
from paid_skills import calculate_price, format_price

quote = calculate_price(
    input_tokens=2000,
    output_tokens=800,
    per_call_usd=0.10,           # premium tier
    cost_per_1k_tokens_usd=0.075, # Opus blended rate
    margin=0.50,                 # 50% margin
)
print(format_price(quote))  # $0.2415 ($0.1000 per-call + $0.1415 tokens @ ...)
```

## Payment integration

Implement the `PaymentHook` protocol to plug in any payment backend:

```python
from paid_skills.runtime import PaymentHook, PaymentRequired

class StripeHook:
    def __init__(self, stripe, db):
        self.stripe = stripe
        self.db = db  # your own DB — we don't touch it

    def before_invocation(self, invocation):
        user_id = invocation.metadata.get("user_id")
        if not self.db.has_credit(user_id):
            raise PaymentRequired(f"User {user_id} has no credit")

    def after_invocation(self, invocation, result):
        user_id = invocation.metadata.get("user_id")
        self.db.charge(
            user_id,
            amount_usd=result.quote.total_usd,
            description=f"Skill: {invocation.skill.name} ({result.invocation_id})",
        )
        # Optional: also create a Stripe InvoiceItem / metered usage record
        # self.stripe.InvoiceItem.create(customer=..., amount=int(...), currency="usd")
```

Plug it in:

```python
runtime = PaidSkillRuntime(
    payment_hook=StripeHook(stripe, my_db),
    per_call_usd=0.05,
)
```

Or wrap the HTTP service with your own auth + payment middleware. See `paid_skills/server.py` for the commented-out Stripe webhook handler.

## Deploy anywhere

### Cloudflare Workers

```toml
# wrangler.toml
name = "paid-skills"
main = "src/paid_skills/worker.py"
compatibility_date = "2026-04-01"

[vars]
ANTHROPIC_API_KEY = "sk-ant-..."
```

Replace the FastAPI app with a Workers-compatible handler — `paid_skills.runtime.PaidSkillRuntime` works as-is. See `examples/cloudflare-worker/` (coming soon) for a drop-in.

### Fly.io / Railway / Render / Heroku

Just deploy the FastAPI service. Add a real WSGI server (gunicorn / uvicorn workers), point your domain at it.

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
EXPOSE 8000
CMD ["uvicorn", "paid_skills.server:app", "--host", "0.0.0.0", "--port", "8000"]
```

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     Your storefront / SaaS                        │
│  (next.js / Django / Rails / whatever you already have)           │
└─────────────┬────────────────────────────────────────────────────┘
              │ customer pays here (Stripe / Lightning / USDC)
              ▼
┌──────────────────────────────────────────────────────────────────┐
│              paid-skills runtime (this package)                   │
│                                                                  │
│   ┌────────────┐    ┌─────────────────┐    ┌──────────────────┐  │
│   │ Skill      │───▶│ PaidSkillRuntime│───▶│ Anthropic API    │  │
│   │ (SKILL.md) │    │ + meter         │    │ (Sonnet/Opus)    │  │
│   └────────────┘    └────────┬────────┘    └──────────────────┘  │
│                              │                                   │
│                              ▼                                   │
│                    ┌───────────────────┐                         │
│                    │ UsageRecord       │                         │
│                    │ (per invocation)  │                         │
│                    └───────────────────┘                         │
└─────────────┬────────────────────────────────────────────────────┘
              │
              ▼
       ┌────────────────────────┐
       │  Your DB + payment     │  ◀── YOU OWN THIS
       │  processor (your call) │
       └────────────────────────┘
```

Why this shape:
- **Skill execution** lives in the open-source runtime
- **Metering** is hookable — plug in Postgres, BigQuery, a CSV file, whatever
- **Payment** is hookable — Stripe, Stripe Connect for marketplace, Lightning, USDC, internal credits
- **Front end** is your problem (we don't pretend to know your brand)

## What we DON'T do (vs closed marketplaces)

| | closed marketplaces | paid-skills |
| --- | --- | --- |
| Source-available | ❌ | ✅ MIT |
| Host anywhere | ❌ their infra only | ✅ Cloudflare, Fly, AWS, your box |
| Payment processor | ❌ Stripe Connect only | ✅ any (implement PaymentHook) |
| Take a revenue cut | 10-30% | ✅ 0% (or run your own server, free) |
| Bundle with your SaaS | ❌ | ✅ |
| Skill format | their variant | ✅ open [agentskills.io](https://agentskills.io) spec |
| Customer data ownership | theirs | ✅ yours |

## Examples

The `examples/` directory ships two ready-to-ship skills:

- **`summarize-url/`** — paste a link, get 3 bullets
- **`contract-review/`** — paste a contract clause, get redlines + risk flags

Both follow the [open Agent Skills spec](https://agentskills.io) and load out of the box.

## Development

```bash
git clone https://github.com/sanjibani/paid-skills
cd paid-skills
pip install -e ".[dev]"
pytest          # 19 tests
```

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

- Anthropic for the [open Agent Skills standard](https://agentskills.io)
- Every indie developer trying to monetize their expertise without giving up ownership

## See also

- [sanjibani/skill-sandbox](https://github.com/sanjibani/skill-sandbox) — the **security layer** for running any third-party Skill safely (no network by default, wall-time + memory limits, JSONL audit log). Pairs with paid-skills for defense-in-depth: host a Skill in paid-skills, run it inside a skill-sandbox subprocess.
- [sanjibani/mcp-skills-pack](https://github.com/sanjibani/mcp-skills-pack) — 5 ready-to-use Claude Skills (`mcp-auth-helper`, `mcp-error-decoder`, `mcp-tool-picker`, `mcp-schema-discoverer`, `mcp-rate-limit-handler`) that teach Claude the operational best practices of working with MCP servers. Drop them into `~/.claude/skills/` and the agent stops guessing env-var names, decoding raw error envelopes, hammering rate-limited endpoints.
- [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents)
- [Open Agent Skills standard](https://agentskills.io)
- [Stripe — Connect for marketplaces](https://stripe.com/connect)
- [Higress — MCP marketplace infrastructure](https://mcp.higress.ai/)