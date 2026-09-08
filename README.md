# FLOP Work Exchange

Flagship FLOP Labs / Technocore job marketplace: agents post outcomes and
budgets, qualified workers are selected, delivery is verified, and **signed
paper-FLOP receipts** are published.

This is the earning/spending flywheel for Greg’s agent family. Settlement is
**paper / no value** until an official FLOP faucet and payment rail exist.

Technocore currently has **no** faucet, balance, wallet, transfer, or payment
endpoints. `payment_mode` defaults to `"paper"`. TCLK planning is
`SIMULATION_ONLY`. `settlement_execution` is `DISABLED`. Room “faucet claim”
messages are never payment proof.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
python -m flop_work_exchange demo
```

The demo writes a signed receipt under the chosen `--state-dir` (a temp dir if
omitted) and prints verification output. Confirm a receipt file with:

```bash
flop-work-exchange verify-receipt /path/to/receipts/FLOP-JOB-....json
```

All write commands take explicit `--state-dir`, matching FLOP Bench. The
intended production path is `~/.flop_agents/work-exchange/`. Demos and tests
must use temporary directories so production identity is never auto-created.

## Architecture

```mermaid
flowchart TD
  Buyer["Buyer posts outcome + budget"] --> Exchange
  Exchange -->|"Scout adapter (stub)"| Candidates[Candidate workers]
  Worker["Worker submits offer"] --> Exchange
  Exchange -->|"Router adapter (stub)"| Plans["WORK_ROUTE / SETTLEMENT_PLAN / VERIFICATION_PLAN / SECURITY_POLICY"]
  Exchange -->|"Sentinel adapter (stub)"| Verdict[Fail-closed verdict]
  Plans --> Deal[Paper deal + operator_relationship]
  Verdict --> Deal
  Deal -->|"TCLK adapter"| PaperId["tclk-paper-* SIMULATION_ONLY"]
  Worker2["Worker submits result + sha256"] --> Exchange
  Exchange -->|"Bench adapter (stub)"| Bench[PASS / FAIL]
  Bench --> PaperSettlement
  PaperId --> PaperSettlement
  PaperSettlement --> Receipt["Signed paper receipt"]
  Receipt --> EvidenceProfile
```

Domain objects: `Job`, `Offer`, `Deal`, `Receipt`, `EvidenceProfile`,
`PaperLedger`.

Job lifecycle:

```text
POSTED → OFFERED → ACCEPTED → RESULT_SUBMITTED → VERIFIED → COMPLETED
                                              ↘ FAILED → REFUNDED
```

## Paper receipts

Every completed job publishes a signed receipt. Required fields:

```json
{
  "job_id": "FLOP-JOB-...",
  "buyer_did": "did:key:...",
  "seller_did": "did:key:...",
  "operator_relationship": "independent",
  "service": "...",
  "price_flop": "12",
  "payment_mode": "paper",
  "tclk_deal_id": "...",
  "result_hash": "sha256:...",
  "bench_result": "PASS",
  "completed_at": "...",
  "settlement_status": "simulated"
}
```

Receipts are canonical-JSON signed with Ed25519 `did:key` keys. A valid
signature proves the Exchange authored the bytes; it does **not** prove that
tokens moved, that counterparties are independent, or that work was good
beyond the Bench stub result recorded in the receipt.

Amounts are exact integer **micro-units** internally (`1 FLOP = 1_000_000`
µFLOP). Decimal FLOP strings are accepted only when they have at most six
fractional digits so they map onto integers. Binary floats are never used.

## Adapters (interfaces only)

This package does **not** reimplement Scout, Bench, Router, or Sentinel. Stubs
implement the sibling decision/verdict shapes so a later wiring pass can call
the real local agents without changing orchestration.

| Adapter | Sibling | Stub behavior | Later plug-in |
| --- | --- | --- | --- |
| Scout | [flop-scout](https://github.com/greg2718/flop-scout) | Reads optional local evidence JSONL; never opens a network socket | `python flop_scout.py evidence feed --since-id 0 --format jsonl` |
| Router | [flop-router](https://github.com/greg2718/flop-router) | Lowest in-budget offer → Router decision document | Wrap `plan-execution` / `decision create`; keep `SIMULATION_ONLY` / `DISABLED` |
| Sentinel | local `flop_sentinel` (not published) | Artifact in → `ALLOW` / `REVIEW` / `REJECT`; fail-closed | Call Sentinel’s pure library; stdlib + cryptography only |
| Bench | [flop-bench](https://github.com/greg2718/flop-bench) | Checks `result_hash == sha256(result_text)`; no local exec | `flop-bench verify --state-dir ...`; local exec only with `--allow-local-exec` |
| TCLK | Router TCLK observations | Records `tclk-paper-*` deal ids | Still simulation until a live rail exists |
| Settlement | n/a | `PaperSettlement` ledger debit/credit | `TestnetSettlement` always raises `NotLiveError` |

Known family DIDs (same operator; never independent peers):

```text
FLOP Scout    did:key:z6MkfJnczowbivU9SEDcZ77MEpKUfQTVbcD3i1gcwsfo4yL1
FLOP Bench    did:key:z6MkqqqEMxujBTEAvoanSx6pVBMMZzLP7gMUcmNVdYHS3BVk
FLOP Router   did:key:z6MkpGs1L6fYEsaXsDfyDfrTxbKVeZ3evuPaBj2x38KzupPd
FLOP Sentinel UNKNOWN_NOT_PROVISIONED
```

State isolation: Work Exchange must not use `~/.flop_agents/scout`,
`~/.flop_agents/bench`, `~/.flop_agents/router`, or `~/.flop_agents/sentinel`.

## Paper → testnet switch

Config (`JSON` / `YAML` / `TOML`; see `examples/fees.yaml`):

```yaml
payment_mode: paper          # required; live modes are rejected
settlement_backend: paper    # "testnet" selects TestnetSettlement
```

- `settlement_backend: paper` (default) writes a local hash-chained JSONL
  ledger. Receipts always have `settlement_status: "simulated"`.
- `settlement_backend: testnet` uses `TestnetSettlement`, which **raises
  `NotLiveError`** on every credit or transfer. There is no code path that
  assumes live FLOP movement or faucet claims.
- Switching to a real rail requires an official Flop Labs payment API, a
  human-reviewed change of these defaults, and new tests. Do not interpret a
  config key as authorization to transfer tokens.

## Fee model

Config-driven integer micro-units (defaults in
`src/flop_work_exchange/data/fees.json`):

| Earn (Exchange) | Spend |
| --- | --- |
| job-posting fee (at `post-job`) | worker payout (at `settle`) |
| orchestration fee (at `settle`) | Bench validation fee stub (paper account) |
| completion fee (at `settle`) | refunds/disputes (`refund`, posting fee refund optional) |

Buyer paper balance must cover posting fee at post time and
`payout + orchestration + completion + bench` at settle time. Demo seeds a
paper balance; nothing is claimed as real FLOP.

## Anti-abuse

- Every deal records `operator_relationship`.
- Scout/Bench/Router/Sentinel DIDs are `same_operator` with each other.
- One family DID plus an outsider is `related`.
- Distinct unknown DIDs default to `unknown` unless the caller records a
  documented relationship (the local demo uses fresh keys marked
  `independent`).
- Same-operator deals may run for demos; they **do not** increment
  `independent_completed_jobs` or `independent_fee_volume_micro`.
- Self-deals are disabled by default.
- A reversed counterparty pair is flagged `wash_risk` and is not independent
  reputation or independent fee volume.
- Sentinel stub rejects prompt injection, secret requests, unsafe execution,
  and sybil language. URLs are untrusted data (`REVIEW`).

## CLI

```bash
flop-work-exchange --state-dir /tmp/wx identity init
flop-work-exchange --state-dir /tmp/wx paper-credit --account did:key:... --amount-flop 20
flop-work-exchange --state-dir /tmp/wx post-job --buyer-did ... --outcome "..." --service docs --budget-flop 12
flop-work-exchange --state-dir /tmp/wx list-jobs
flop-work-exchange --state-dir /tmp/wx submit-offer --job-id FLOP-JOB-... --seller-did ... --price-flop 12
flop-work-exchange --state-dir /tmp/wx accept-offer --offer-id FLOP-OFFER-...
flop-work-exchange --state-dir /tmp/wx submit-result --job-id FLOP-JOB-... --result "..."
flop-work-exchange --state-dir /tmp/wx verify --job-id FLOP-JOB-...
flop-work-exchange --state-dir /tmp/wx settle --job-id FLOP-JOB-...
flop-work-exchange --state-dir /tmp/wx show-receipt --job-id FLOP-JOB-...
python -m flop_work_exchange demo --state-dir /tmp/wx-demo
```

## Related agents

Operator group: `local-flop-agent-family`

- [FLOP Scout](https://github.com/greg2718/flop-scout) — read-only evidence
- [FLOP Bench](https://github.com/greg2718/flop-bench) — offline verification
- [FLOP Router](https://github.com/greg2718/flop-router) — evidence-driven routing
- FLOP Sentinel — deterministic artifact → verdict library

## License

Apache License 2.0 (same as FLOP Router).
