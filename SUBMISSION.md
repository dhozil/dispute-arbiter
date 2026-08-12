# GenLayer Portal Submission — DisputeArbiter

## Title

**DisputeArbiter — Trustless AI Arbitration with Verifiable, Tamper-Resistant Evidence**

## Notes (1000 characters)

DisputeArbiter arbitrates disputes between two parties (or AI agents) on-chain. Parties deposit a symmetric escrow and submit statements plus evidence commitments (URL + sha256 hash). The contract verifies the evidence, runs AI-validator consensus, records a binding verdict and pays via pull withdrawals.

Consensus (Equivalence Principle): leader/validator pair under run_nondet_unsafe. Validators independently re-run adjudication, requiring the verdict to match, confidence above a minimum floor within tolerance, and every cited URL to match its committed hash - rejecting hallucinated or tampered citations.

Security: evidence cannot be swapped after funding; prompt-injection defenses; fee capped at 10%; binding escrow; idempotent payouts; mutual settlement has no fee; after a bounded timeout either party can unilaterally withdraw their deposit, nothing is locked.

Verified: 69 unit tests; deployed on GenLayer Studio, every method tested with web + LLM; fund transfers verified on-chain.

---

## Useful submission links

- Live contract: https://explorer-studio.genlayer.com/address/0xCB95BF5A863AbCF7779920975DaBeca55Cf31dBC
- Repo README (docs, design, threat model): see `README.md`
- Direct-mode tests: `pytest contracts/dispute-arbiter/tests/test_dispute_arbiter.py`
- On-chain test scripts: `contracts/dispute-arbiter/scripts/onchain_test.py`, `contracts/dispute-arbiter/scripts/test_deployed.py`, `contracts/dispute-arbiter/scripts/verify_fund_transfer.py`
