# Aegis

**Resource Governor for Multi-Agent Systems.**

Aegis is not an agent framework — it is a **Control Plane** that sits between human intent and agentic execution (CrewAI, LangGraph, custom bots) to govern **budget**, **compute**, and **shared state**.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      Human Operator                         │
└─────────────────┬───────────────────────────────────────────┘
                  │  Policy + Budget Config
                  ▼
┌─────────────────────────────────────────────────────────────┐
│                    AEGIS GOVERNOR                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ PolicyEngine │  │ ModelArbiter │  │ StateLockManager │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
│  ┌──────────────┐  ┌──────────────┐                        │
│  │ Web3 Adapter │  │ LLM Adapter  │   ◄── Redis-backed    │
│  └──────────────┘  └──────────────┘                        │
└─────────────────┬───────────────────────────────────────────┘
                  │  Handshake Protocol (JSON/HTTP)
                  ▼
┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐
│ Agent 1│  │ Agent 2│  │ Agent 3│  │ Agent N│
│ CrewAI │  │  Lang  │  │ Custom │  │  Bot   │
│        │  │ Graph  │  │        │  │        │
└────────┘  └────────┘  └────────┘  └────────┘
```

## Core Principles

| Principle | Description |
|-----------|-------------|
| **Zero-Waste (Muda)** | No agent uses a high-tier model for a low-tier task. The ModelArbiter routes by task complexity. |
| **Built-in Quality** | Every decision is validated against the PolicyEngine before execution. |
| **State Integrity** | Agents must acquire Redis-backed locks on shared resources to prevent race conditions. |

## Quick Start

```bash
# 1. Install
pip install -e ".[dev]"

# 2. Start Redis
docker run -d -p 6379:6379 redis:7-alpine

# 3. Configure
cp .env.example .env  # Edit with your API keys

# 4. Run the Governor
uvicorn main:app --reload --port 8000

# 5. Open the API docs
# http://localhost:8000/docs
```

## Agent Handshake Protocol

Agents communicate with Aegis via a standardised JSON protocol:

### 1. Register (Handshake)
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

### 2. Request Permission
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

### 3. Response
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

## Project Structure

```
aegis/
├── core/
│   ├── governor.py      # ResourceGovernor — budget + policy enforcement
│   ├── arbiter.py       # ModelArbiter — intelligence routing
│   └── state_lock.py    # StateLockManager — distributed semaphore
├── policy/
│   ├── engine.py        # PolicyEngine — rule evaluation
│   └── rules.json       # Default guardrail definitions
├── adapters/
│   ├── web3_adapter.py  # Gas-aware budget monitoring (Sui/Monad/Solana)
│   └── llm_adapter.py   # Token usage tracking
└── api/
    ├── routes.py        # FastAPI endpoints
    └── schemas.py       # Pydantic V2 Handshake Protocol schemas
```

## Running Tests

```bash
pytest -v
```

## Tech Stack

- **Python 3.11+** with full async/await
- **FastAPI** — Governor API surface
- **Redis** — atomic resource locking and budget tracking
- **Pydantic V2** — request/response validation
- **httpx** — async HTTP for Web3 RPC calls

## License

MIT
