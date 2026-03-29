<p align="center">
  <h1 align="center">Aegis</h1>
  <p align="center"><strong>The Industrial Standard for Agentic Governance</strong></p>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> &bull;
  <a href="#the-problem">The Problem</a> &bull;
  <a href="#architecture">Architecture</a> &bull;
  <a href="#agent-handshake-protocol">Handshake Protocol</a> &bull;
  <a href="#features">Features</a>
</p>

---

## The Problem

We are entering a fully agentic world. Hundreds of frameworks now exist to make agents **do things** — browse the web, write code, execute trades, deploy contracts.

But almost none of them answer the harder question: **who controls the agents?**

Today, a single misconfigured agent can:
- Drain a wallet by calling a swap function in an infinite loop
- Burn through $500 of GPT-4 tokens on a task that needed Gemini Flash
- Race-condition its way into double-spending a shared treasury
- Deploy a contract to mainnet when it was only authorised for testnet

These aren't hypotheticals. They are the inevitable consequences of **execution without governance**.

We have mastered making agents act. We are failing at making agents act **within limits**.

## The Answer

**Aegis is not an agent framework.** It doesn't make agents smarter or more capable. It makes them **accountable**.

Aegis is a dedicated **Resource Governor** — a Control Plane that sits between human intent and agentic execution. Every agent, regardless of framework (CrewAI, LangGraph, AutoGen, or your custom bot), must request permission before consuming resources. No exceptions.

```
Human sets policy  -->  Aegis enforces it  -->  Agents operate within bounds
```

Think of it as **Kubernetes for agent resource management**: you declare the rules, Aegis enforces them at runtime, and agents that violate policy get rejected before they can cause damage.

## Why This Matters

| Without Aegis | With Aegis |
|---|---|
| Agent picks GPT-4 for every task, burns budget in hours | **ModelArbiter** routes each task to the cheapest model that satisfies complexity requirements |
| Two agents simultaneously access the same wallet, causing a double-spend | **StateLockManager** enforces Redis-backed semaphores with priority-based preemption |
| A bot autonomously withdraws funds at 3am with no human oversight | **HITL Escalation** pauses execution and pings the operator on Telegram for approval |
| No one notices a policy is too loose until after the incident | **Kaizen Engine** runs shadow experiments and auto-optimises model routing and cost ceilings |
| Gas spikes on Sui cause a $0.01 operation to cost $5.00 | **Web3 Adapter** checks real-time gas prices and blocks execution above the budget threshold |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Human Operator                         │
│              (Policy, Budget, Final Authority)               │
└─────────────────┬───────────────────────────────────────────┘
                  │  Policy + Budget Config
                  ▼
┌─────────────────────────────────────────────────────────────┐
│                      AEGIS GOVERNOR                         │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ PolicyEngine │  │ ModelArbiter │  │ StateLockManager │  │
│  │  (Guardrails)│  │ (Zero-Waste) │  │   (Semaphores)   │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ HITL Manager │  │KaizenEngine  │  │ PolicyOptimizer  │  │
│  │(Human Loop)  │  │(Continuous   │  │ (Auto-Tune)      │  │
│  │              │  │ Improvement) │  │                  │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
│  ┌──────────────┐  ┌──────────────┐                        │
│  │ Web3 Adapter │  │ LLM Adapter  │   <-- Redis-backed     │
│  │(Gas-Aware)   │  │(Token Track) │                        │
│  └──────────────┘  └──────────────┘                        │
└─────────────────┬───────────────────────────────────────────┘
                  │  Handshake Protocol (JSON/HTTP)
                  ▼
┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐
│ Agent 1│  │ Agent 2│  │ Agent 3│  │ Agent 4│  │ Agent N│
│ CrewAI │  │  Lang  │  │ AutoGen│  │ Custom │  │  Any   │
│        │  │ Graph  │  │        │  │  Bot   │  │Framework│
└────────┘  └────────┘  └────────┘  └────────┘  └────────┘
```

## Core Principles

Aegis is built on three principles borrowed from industrial manufacturing:

### 1. Zero-Waste (Muda)
No agent should use a $0.015/1k-token model for a task that a $0.00015/1k-token model handles at 97% quality. The **ModelArbiter** maps task complexity (1-10) to the cheapest model tier that satisfies it. The **Kaizen Engine** continuously runs shadow experiments to find cheaper substitutions without quality loss.

### 2. Built-in Quality (BIQ)
Every agent request is validated against the **PolicyEngine** before execution — not after. Blocked intents, cost ceilings, priority floors, and agent blocklists are evaluated atomically. If a request violates policy, it never reaches the agent.

### 3. State Integrity
Agents must acquire distributed locks on shared resources (wallets, files, APIs) before accessing them. The **StateLockManager** implements Redis-backed semaphores with automatic TTL expiry and **priority-based preemption** — a high-priority agent can evict a low-priority lock holder to prevent deadlocks.

## Features

### Resource Governor
- Per-agent daily budget tracking with Redis-backed atomic counters
- Policy-driven approval: cost ceilings, blocked intents, priority floors
- Returns `GRANTED`, `PENDING`, or `REJECTED` with a reason for every request

### Intelligence Routing (ModelArbiter)
- Maps task complexity (logic density 1-10) to optimal model tiers
- 5-tier system: from Gemini Flash ($0.00015/1k) to Claude Opus/O3 ($0.015/1k)
- Cost-constrained recommendations — never overspend on intelligence

### Distributed Locking (StateLockManager)
- Redis-backed context manager for resource locks
- TTL-based auto-expiry prevents orphaned locks
- Priority-based preemption: critical agents evict lower-priority holders
- Deadlock prevention built into the protocol

### Human-in-the-Loop (HITL)
- Automatic escalation when: cost > $5, sensitive intent detected, or agent confidence < 70%
- Telegram bot sends approval requests with inline `[APPROVE]` / `[REJECT]` buttons
- 10-minute timeout auto-rejects for safety
- Fully async — 50 agents can wait for approval simultaneously without blocking

### Kaizen Engine (Continuous Improvement)
- **Shadow Mode Experiments**: A/B test cheaper models against current ones in production
- **Muda Detector**: Identifies latent tasks, lock bottlenecks, and wasted compute
- **SCAMPER Framework**: Substitute, Combine, Adapt, Modify, Put-to-other-use, Eliminate, Reverse
- **Policy Optimizer**: Auto-tunes cost ceilings and routing rules based on experiment results

### Web3 Resource Awareness
- Real-time gas price monitoring for Sui, Monad, and Solana
- Blocks transactions when gas exceeds the agent's budget threshold
- Chain-specific cost estimation

## Quick Start

```bash
# 1. Install
pip install -e ".[dev]"

# 2. Start Redis (or use USE_FAKE_REDIS=true for local dev)
docker run -d -p 6379:6379 redis:7-alpine

# 3. Configure
cp .env.example .env  # Edit with your API keys

# 4. Run the Governor
uvicorn main:app --reload --port 8000

# 5. Open the interactive API docs
# http://localhost:8000/docs
```

## Agent Handshake Protocol

Every agent must complete a standardised handshake before it can request resources. This is framework-agnostic — CrewAI, LangGraph, AutoGen, or a bash script with `curl` all speak the same protocol.

### Step 1: Register
```bash
curl -X POST http://localhost:8000/api/v1/handshake \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "crew-researcher-01",
    "agent_framework": "crewai",
    "capabilities": ["web_search", "code_gen"],
    "max_priority": 7,
    "session_budget_usd": 2.50
  }'
```

### Step 2: Request Permission
```bash
curl -X POST http://localhost:8000/api/v1/request-permission \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "crew-researcher-01",
    "task_intent": "Summarise Sui governance proposals",
    "resource_type": "llm_tokens",
    "estimated_cost_usd": 0.035,
    "priority": 5,
    "logic_density": 3
  }'
```

### Step 3: Receive Verdict
```json
{
  "status": "GRANTED",
  "agent_id": "crew-researcher-01",
  "reason": "Budget available and policy satisfied.",
  "remaining_budget_usd": 9.965,
  "recommended_model": "gemini-flash",
  "granted_at": "2026-03-29T12:00:00Z"
}
```

The agent now knows: **what model to use**, **how much budget remains**, and **that it has permission to proceed**. No guessing, no overruns, no surprises.

## Project Structure

```
aegis/
├── core/
│   ├── governor.py      # ResourceGovernor — budget + policy enforcement
│   ├── arbiter.py       # ModelArbiter — intelligence routing by complexity
│   ├── state_lock.py    # StateLockManager — distributed semaphore with preemption
│   ├── hitl.py          # HITL Manager — escalation, async wait, timeout
│   └── kaizen.py        # Kaizen Engine — shadow experiments, Muda detection
├── policy/
│   ├── engine.py        # PolicyEngine — rule evaluation
│   ├── optimizer.py     # PolicyOptimizer — auto-tune rules from experiments
│   └── rules.json       # Default guardrail definitions
├── adapters/
│   ├── web3_adapter.py  # Gas-aware budget monitoring (Sui/Monad/Solana)
│   ├── llm_adapter.py   # Token usage tracking for OpenAI/Anthropic/Google
│   └── telegram_bot.py  # Telegram approval bot for HITL
└── api/
    ├── routes.py        # FastAPI endpoints
    ├── schemas.py       # Pydantic V2 Handshake Protocol schemas
    └── webhook.py       # Telegram webhook receiver
```

## Running Tests

```bash
pytest -v
# 49 tests — covering budget overflow, deadlock prevention, HITL timeout,
# Kaizen experiments, policy optimization, and concurrent escalations.
```

## Tech Stack

- **Python 3.11+** with full async/await
- **FastAPI** — Governor API surface
- **Redis** — atomic resource locking and budget tracking
- **Pydantic V2** — request/response validation
- **httpx** — async HTTP for Web3 RPC and LLM provider calls
- **python-telegram-bot** — HITL approval interface

## Roadmap

- [ ] **Proxy Mode** — transparent interception (agents don't need integration code)
- [ ] **Observability Layer** — structured event stream for every grant/reject decision
- [ ] **Session-Aware Governance** — approve entire workflows, not individual requests
- [ ] **Multi-Tenancy** — isolated budget pools and policies per team/org
- [ ] **Transaction Simulation** — estimate real cost (slippage + gas), not just gas

## License

MIT
