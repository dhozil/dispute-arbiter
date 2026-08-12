"""Direct-mode tests for the DisputeArbiter intelligent contract.

Covers the full lifecycle:
create_dispute -> deposit -> adjudicate (with AI consensus mocks) ->
withdraw_payout / refund / settle / protocol fees, plus the validator logic
of the Equivalence Principle (run_validator) and the security hardening
(evidence content-hash pinning, confidence gate, mutual settlement).
"""

import hashlib
import json

import pytest

from gltest.direct import create_address

CLAIM_BODY = "Delivery logs and agreed specification."
RESP_BODY = "Rebuttal notes and payment records."
CLAIM_URL = "https://evidence.example.com/claim"
RESP_URL = "https://evidence.example.com/response"


def _evidence(url, body):
    """Build an evidence commitment entry (URL + sha256 content hash)."""
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return json.dumps([{"url": url, "hash": h}])


def _make_dispute(contract, vm, claimant, respondent, **overrides):
    vm.sender = claimant
    args = dict(
        respondent=respondent.as_hex,
        subject="Freelance payment for the landing page",
        claimant_statement="I delivered the landing page exactly per the specification.",
        respondent_statement="The landing page was not delivered on time.",
        rule=(
            "The claimant wins if the deliverable matches the agreed "
            "specification and was delivered on time. Otherwise the respondent wins."
        ),
        claimant_evidence=_evidence(CLAIM_URL, CLAIM_BODY),
        respondent_evidence=_evidence(RESP_URL, RESP_BODY),
        required_deposit=1000,
        fee_bps=500,
    )
    args.update(overrides)
    return contract.create_dispute(**args)


def _fund(contract, vm, dispute_id, alice, bob):
    vm.sender = alice
    vm.value = 1000
    contract.deposit(dispute_id)
    vm.sender = bob
    vm.value = 1000
    contract.deposit(dispute_id)


def _setup_adjudication_mocks(
    vm, verdict="claimant", confidence=80, refs=None, tampered_claim=False
):
    claim_body = "MODIFIED EVIDENCE CONTENT" if tampered_claim else CLAIM_BODY
    vm.mock_web(r"evidence\.example\.com/claim.*", {"status": 200, "body": claim_body})
    vm.mock_web(r"evidence\.example\.com/response.*", {"status": 200, "body": RESP_BODY})
    if refs is None:
        refs = [CLAIM_URL]
    vm.mock_llm(
        r"impartial AI arbitrator",
        json.dumps(
            {
                "verdict": verdict,
                "confidence": confidence,
                "reasoning": "The evidence supports this side under the stated rule.",
                "evidence_references": refs,
            }
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# create_dispute (deterministic)
# ──────────────────────────────────────────────────────────────────────


def test_create_dispute(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")

    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    assert dispute_id == "d0"
    d = contract.get_dispute(dispute_id)
    assert d["claimant"] == alice.as_hex
    assert d["respondent"] == bob.as_hex
    assert d["subject"] == "Freelance payment for the landing page"
    assert d["status"] == "pending"
    assert d["required_deposit"] == 1000
    assert d["fee_bps"] == 500
    assert d["claimant_evidence"][0]["url"] == CLAIM_URL
    assert d["claimant_evidence"][0]["hash"] == hashlib.sha256(
        CLAIM_BODY.encode("utf-8")
    ).hexdigest()
    assert d["respondent_evidence"][0]["url"] == RESP_URL
    assert d["verdict"] == ""
    assert d["references"] == []
    assert d["claimant_deposit"] == 0
    assert d["respondent_deposit"] == 0
    assert d["claimant_wants_settle"] is False
    assert d["respondent_wants_settle"] is False


def test_create_dispute_increments_ids(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")

    first = _make_dispute(contract, direct_vm, alice, bob)
    second = _make_dispute(
        contract,
        direct_vm,
        alice,
        bob,
        subject="Another dispute",
        claimant_statement="a",
        respondent_statement="b",
    )
    assert first == "d0"
    assert second == "d1"
    assert len(contract.get_disputes()) == 2


def test_create_dispute_rejects_same_party(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")

    with direct_vm.expect_revert("Claimant and respondent must differ"):
        _make_dispute(contract, direct_vm, alice, alice)


def test_create_dispute_rejects_empty_subject(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")

    with direct_vm.expect_revert("Subject and both statements are required"):
        _make_dispute(contract, direct_vm, alice, bob, subject="")


def test_create_dispute_rejects_zero_deposit(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")

    with direct_vm.expect_revert("Required deposit must be positive"):
        _make_dispute(contract, direct_vm, alice, bob, required_deposit=0)


def test_create_dispute_rejects_excessive_fee(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")

    # fee_bps is capped at 1000 (10%) so a creator cannot grief the escrow.
    with direct_vm.expect_revert("Fee basis points must be between 0 and 1000"):
        _make_dispute(contract, direct_vm, alice, bob, fee_bps=1001)


def test_create_dispute_rejects_invalid_evidence_json(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")

    with direct_vm.expect_revert("Evidence must be a valid JSON array"):
        _make_dispute(contract, direct_vm, alice, bob, claimant_evidence="not-json")


def test_create_dispute_rejects_non_http_evidence(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")

    entry = json.dumps(
        [{"url": "ftp://legacy.example.com/file", "hash": "ab" * 32}]
    )
    with direct_vm.expect_revert("Evidence URLs must start with http"):
        _make_dispute(contract, direct_vm, alice, bob, claimant_evidence=entry)


def test_create_dispute_rejects_missing_hash(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")

    entry = json.dumps([{"url": CLAIM_URL}])
    with direct_vm.expect_revert("64-char sha256 content hash"):
        _make_dispute(contract, direct_vm, alice, bob, claimant_evidence=entry)


def test_create_dispute_rejects_bad_hash(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")

    entry = json.dumps([{"url": CLAIM_URL, "hash": "zz" * 32}])
    with direct_vm.expect_revert("64-char sha256 content hash"):
        _make_dispute(contract, direct_vm, alice, bob, claimant_evidence=entry)


def test_create_dispute_rejects_too_many_evidence_urls(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")

    entries = [
        {"url": f"https://evidence.example.com/{i}", "hash": "ab" * 32}
        for i in range(5)
    ]
    with direct_vm.expect_revert("Too many evidence URLs"):
        _make_dispute(
            contract, direct_vm, alice, bob, claimant_evidence=json.dumps(entries)
        )


def test_create_dispute_rejects_duplicate_urls_within_party(direct_vm, direct_deploy):
    """A party cannot submit the same evidence URL twice."""
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")

    dup = json.dumps(
        [
            {"url": CLAIM_URL, "hash": hashlib.sha256(b"a").hexdigest()},
            {"url": CLAIM_URL, "hash": hashlib.sha256(b"b").hexdigest()},
        ]
    )
    with direct_vm.expect_revert("Duplicate evidence URL"):
        _make_dispute(contract, direct_vm, alice, bob, claimant_evidence=dup)


def test_create_dispute_rejects_shared_url_between_parties(direct_vm, direct_deploy):
    """The same evidence URL cannot be attributed to both parties."""
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")

    same = _evidence(CLAIM_URL, CLAIM_BODY)
    with direct_vm.expect_revert("cannot be submitted by both parties"):
        _make_dispute(
            contract,
            direct_vm,
            alice,
            bob,
            claimant_evidence=same,
            respondent_evidence=same,
        )


def test_create_dispute_allows_distinct_evidence_per_party(direct_vm, direct_deploy):
    """Different URLs per party are allowed and stored separately."""
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")

    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    d = contract.get_dispute(dispute_id)
    assert d["claimant_evidence"][0]["url"] == CLAIM_URL
    assert d["respondent_evidence"][0]["url"] == RESP_URL


def test_create_dispute_rejects_long_evidence_url(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")

    entry = json.dumps(
        [{"url": "https://evidence.example.com/" + "a" * 600, "hash": "ab" * 32}]
    )
    with direct_vm.expect_revert("Evidence URL is too long"):
        _make_dispute(contract, direct_vm, alice, bob, claimant_evidence=entry)


def test_create_dispute_rejects_zero_address_respondent(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")

    zero = "0x" + "0" * 40
    direct_vm.sender = alice
    with direct_vm.expect_revert("Respondent cannot be the zero address"):
        contract.create_dispute(
            zero,
            "Freelance payment",
            "statement a",
            "statement b",
            "rule",
            _evidence(CLAIM_URL, CLAIM_BODY),
            _evidence(RESP_URL, RESP_BODY),
            1000,
            500,
        )


def test_create_dispute_rejects_contract_as_respondent(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")

    from genlayer.py.types import Address

    contract_addr = Address(direct_vm._contract_address).as_hex
    direct_vm.sender = alice
    with direct_vm.expect_revert("Respondent cannot be the arbitration contract"):
        contract.create_dispute(
            contract_addr,
            "Freelance payment",
            "statement a",
            "statement b",
            "rule",
            _evidence(CLAIM_URL, CLAIM_BODY),
            _evidence(RESP_URL, RESP_BODY),
            1000,
            500,
        )


# ──────────────────────────────────────────────────────────────────────
# deposit (payable)
# ──────────────────────────────────────────────────────────────────────


def test_deposit_funds_dispute_and_transitions_to_funded(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.sender = alice
    direct_vm.value = 1000
    contract.deposit(dispute_id)

    d = contract.get_dispute(dispute_id)
    assert d["claimant_deposit"] == 1000
    assert d["status"] == "pending"  # only one side so far

    direct_vm.sender = bob
    direct_vm.value = 1000
    contract.deposit(dispute_id)

    d = contract.get_dispute(dispute_id)
    assert d["respondent_deposit"] == 1000
    assert d["status"] == "funded"


def test_deposit_third_party_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    charlie = create_address("charlie")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.sender = charlie
    direct_vm.value = 1000
    with direct_vm.expect_revert("Only the claimant or respondent can deposit"):
        contract.deposit(dispute_id)


def test_deposit_zero_value_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.sender = alice
    direct_vm.value = 0
    with direct_vm.expect_revert("Send some value to deposit"):
        contract.deposit(dispute_id)


def test_deposit_overpay_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.sender = alice
    direct_vm.value = 2000
    with direct_vm.expect_revert("Exceeds required deposit"):
        contract.deposit(dispute_id)


def test_cannot_deposit_after_cancel(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.sender = alice
    contract.cancel(dispute_id)

    direct_vm.value = 1000
    with direct_vm.expect_revert("Dispute is not open for deposits"):
        contract.deposit(dispute_id)


def test_deposit_in_installments_sums_and_funds(direct_vm, direct_deploy):
    """Deposits may be made in several transactions; the sum is capped at the
    required amount and the dispute only funds once BOTH sides have fully paid."""
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.sender = alice
    direct_vm.value = 600
    contract.deposit(dispute_id)
    direct_vm.value = 400
    contract.deposit(dispute_id)
    assert contract.get_dispute(dispute_id)["claimant_deposit"] == 1000
    assert contract.get_dispute(dispute_id)["status"] == "pending"

    direct_vm.sender = bob
    direct_vm.value = 1000
    contract.deposit(dispute_id)
    assert contract.get_dispute(dispute_id)["status"] == "funded"


def test_deposit_second_installment_overpay_rejected(direct_vm, direct_deploy):
    """A later installment cannot push a party past the required amount."""
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.sender = alice
    direct_vm.value = 600
    contract.deposit(dispute_id)

    direct_vm.value = 500
    with direct_vm.expect_revert("Exceeds required deposit"):
        contract.deposit(dispute_id)

    assert contract.get_dispute(dispute_id)["claimant_deposit"] == 600


# ──────────────────────────────────────────────────────────────────────
# cancel / refund
# ──────────────────────────────────────────────────────────────────────


def test_cancel_pending_by_party(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.sender = bob
    contract.cancel(dispute_id)

    assert contract.get_dispute(dispute_id)["status"] == "cancelled"


def test_cancel_third_party_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    charlie = create_address("charlie")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.sender = charlie
    with direct_vm.expect_revert("Only a party to the dispute can cancel"):
        contract.cancel(dispute_id)


def test_cancel_funded_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    direct_vm.sender = alice
    with direct_vm.expect_revert("Only pending disputes can be cancelled"):
        contract.cancel(dispute_id)


def test_refund_returns_deposits_after_cancel(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.sender = alice
    direct_vm.value = 1000
    contract.deposit(dispute_id)

    direct_vm.sender = bob
    contract.cancel(dispute_id)
    assert contract.get_dispute(dispute_id)["status"] == "cancelled"

    direct_vm.sender = alice
    contract.refund(dispute_id)
    d = contract.get_dispute(dispute_id)
    assert d["claimant_withdrawn"] is True
    assert d["claimant_deposit"] == 0


def test_refund_twice_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.sender = alice
    direct_vm.value = 1000
    contract.deposit(dispute_id)
    contract.cancel(dispute_id)
    contract.refund(dispute_id)

    with direct_vm.expect_revert("Nothing to refund"):
        contract.refund(dispute_id)


def test_refund_without_cancel_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.sender = alice
    direct_vm.value = 1000
    contract.deposit(dispute_id)

    with direct_vm.expect_revert("Nothing to refund for this dispute"):
        contract.refund(dispute_id)


def test_refund_returns_exact_partial_deposit(direct_vm, direct_deploy):
    """A party recovers exactly what they deposited (partial installments too)."""
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.sender = alice
    direct_vm.value = 600
    contract.deposit(dispute_id)
    direct_vm.value = 400
    contract.deposit(dispute_id)

    direct_vm.sender = bob
    contract.cancel(dispute_id)

    direct_vm.sender = alice
    contract.refund(dispute_id)
    d = contract.get_dispute(dispute_id)
    assert d["claimant_withdrawn"] is True
    assert d["claimant_deposit"] == 0


# ──────────────────────────────────────────────────────────────────────
# adjudicate (AI consensus)
# ──────────────────────────────────────────────────────────────────────


def test_adjudicate_claimant_wins(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=80)
    direct_vm.sender = alice
    result = contract.adjudicate(dispute_id)

    assert result["verdict"] == "claimant"
    d = contract.get_dispute(dispute_id)
    assert d["status"] == "adjudicated"
    assert d["verdict"] == "claimant"
    assert d["confidence"] == 80
    assert d["references"] == [CLAIM_URL]
    # total = 2000, fee = 500 bps = 100, net = 1900 -> all to claimant
    assert d["claimant_payout"] == 1900
    assert d["respondent_payout"] == 0
    assert contract.get_protocol_fees() == 100


def test_adjudicate_respondent_wins(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    _setup_adjudication_mocks(
        direct_vm, verdict="respondent", confidence=70, refs=[RESP_URL]
    )
    direct_vm.sender = alice
    contract.adjudicate(dispute_id)

    d = contract.get_dispute(dispute_id)
    assert d["verdict"] == "respondent"
    assert d["claimant_payout"] == 0
    assert d["respondent_payout"] == 1900


def test_adjudicate_split(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    _setup_adjudication_mocks(
        direct_vm, verdict="split", confidence=65, refs=[CLAIM_URL, RESP_URL]
    )
    direct_vm.sender = bob
    contract.adjudicate(dispute_id)

    d = contract.get_dispute(dispute_id)
    assert d["verdict"] == "split"
    assert d["claimant_payout"] == 950
    assert d["respondent_payout"] == 950


def test_adjudicate_not_funded_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.sender = alice
    direct_vm.value = 1000
    contract.deposit(dispute_id)

    _setup_adjudication_mocks(direct_vm)
    with direct_vm.expect_revert("Dispute must be fully funded before adjudication"):
        contract.adjudicate(dispute_id)


def test_adjudicate_twice_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    _setup_adjudication_mocks(direct_vm, verdict="claimant")
    contract.adjudicate(dispute_id)

    with direct_vm.expect_revert("Dispute must be fully funded before adjudication"):
        contract.adjudicate(dispute_id)


def test_adjudicate_rejects_invalid_leader_verdict(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    _setup_adjudication_mocks(direct_vm, verdict="maybe")
    with direct_vm.expect_revert("Adjudicator returned an invalid verdict"):
        contract.adjudicate(dispute_id)


def test_adjudicate_low_confidence_reverts(direct_vm, direct_deploy):
    """An ambiguous, low-confidence verdict is never made binding."""
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=40)
    with direct_vm.expect_revert("Adjudication inconclusive"):
        contract.adjudicate(dispute_id)

    # The escrow is untouched: the dispute is still funded and can be retried.
    d = contract.get_dispute(dispute_id)
    assert d["status"] == "funded"
    assert contract.get_protocol_fees() == 0


# ──────────────────────────────────────────────────────────────────────
# validator logic (Equivalence Principle)
# ──────────────────────────────────────────────────────────────────────


def test_validator_agrees(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=80)
    contract.adjudicate(dispute_id)

    assert direct_vm.run_validator() is True


def test_validator_disagrees_on_verdict(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=80)
    contract.adjudicate(dispute_id)

    direct_vm.clear_mocks()
    _setup_adjudication_mocks(direct_vm, verdict="respondent", confidence=80)
    assert direct_vm.run_validator() is False


def test_validator_rejects_confidence_drift(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=90)
    contract.adjudicate(dispute_id)

    # |90 - 65| = 25 > 20 tolerance -> reject (both still >= MIN_CONFIDENCE)
    direct_vm.clear_mocks()
    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=65)
    assert direct_vm.run_validator() is False


def test_validator_rejects_low_confidence_leader(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=80)
    contract.adjudicate(dispute_id)

    low_confidence = {
        "verdict": "claimant",
        "confidence": 40,
        "reasoning": "Weak reasoning.",
        "evidence_references": [CLAIM_URL],
    }
    assert direct_vm.run_validator(leader_result=low_confidence) is False


def test_validator_rejects_when_own_confidence_below_floor(direct_vm, direct_deploy):
    """A validator must not agree when its OWN independent confidence is below
    the floor, even if the verdict matches and the drift is within tolerance."""
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=65)
    contract.adjudicate(dispute_id)

    # Same verdict, drift |65 - 50| = 15 <= 20, but the validator's own
    # confidence (50) is below MIN_CONFIDENCE -> reject.
    direct_vm.clear_mocks()
    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=50)
    assert direct_vm.run_validator() is False


def test_validator_rejects_oversized_evidence(direct_vm, direct_deploy):
    """Evidence whose fetched content exceeds the size cap is treated as
    invalid, so a leader cannot cite it."""
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    big_body = "x" * 70000  # > MAX_FETCH_BYTES
    direct_vm.mock_web(
        r"evidence\.example\.com/claim.*", {"status": 200, "body": big_body}
    )
    direct_vm.mock_web(
        r"evidence\.example\.com/response.*", {"status": 200, "body": RESP_BODY}
    )
    direct_vm.mock_llm(
        r"impartial AI arbitrator",
        json.dumps(
            {
                "verdict": "claimant",
                "confidence": 80,
                "reasoning": "The evidence supports the claimant.",
                "evidence_references": [CLAIM_URL],
            }
        ),
    )
    contract.adjudicate(dispute_id)

    # The oversized claim evidence is invalid -> the citation is rejected.
    assert direct_vm.run_validator() is False


def test_validator_rejects_hallucinated_citation(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=80)
    contract.adjudicate(dispute_id)

    # Structurally valid, but cites a URL that was never submitted.
    malicious_leader = {
        "verdict": "claimant",
        "confidence": 80,
        "reasoning": "The evidence clearly supports the claimant.",
        "evidence_references": ["https://evil.example.com/not-submitted"],
    }
    assert direct_vm.run_validator(leader_result=malicious_leader) is False


def test_validator_rejects_citation_of_tampered_evidence(direct_vm, direct_deploy):
    """A leader cannot cite evidence whose content no longer matches its
    committed hash — the validator independently re-verifies the hash."""
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=80)
    contract.adjudicate(dispute_id)

    # The leader cited the claim URL while it was authentic...
    assert direct_vm.run_validator() is True

    # ...but by the time the validator re-checks, the claim URL content has
    # changed (hash no longer matches the commitment) -> tampered -> reject.
    direct_vm.clear_mocks()
    _setup_adjudication_mocks(
        direct_vm, verdict="claimant", confidence=80, tampered_claim=True
    )
    assert direct_vm.run_validator() is False


def test_validator_rejects_malformed_leader_output(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=80)
    contract.adjudicate(dispute_id)

    malformed = {
        "verdict": "not-a-verdict",
        "confidence": 999,
        "reasoning": "",
        "evidence_references": "nope",
    }
    assert direct_vm.run_validator(leader_result=malformed) is False


def test_validator_rejects_leader_error(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=80)
    contract.adjudicate(dispute_id)

    assert direct_vm.run_validator(leader_error=ValueError("LLM timeout")) is False


# ──────────────────────────────────────────────────────────────────────
# withdraw_payout
# ──────────────────────────────────────────────────────────────────────


def _adjudicate_claimant_wins(contract, direct_vm, dispute_id):
    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=80)
    contract.adjudicate(dispute_id)


def test_winner_withdraws_payout(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)
    _adjudicate_claimant_wins(contract, direct_vm, dispute_id)

    assert contract.get_payout_claim(dispute_id, alice.as_hex) == 1900
    direct_vm.sender = alice
    contract.withdraw_payout(dispute_id)

    d = contract.get_dispute(dispute_id)
    assert d["claimant_withdrawn"] is True


def test_payout_claim_invalid_address_returns_zero(direct_vm, direct_deploy):
    """Malformed party input to a view never crashes or leaks state."""
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)
    _adjudicate_claimant_wins(contract, direct_vm, dispute_id)

    assert contract.get_payout_claim(dispute_id, "not-an-address") == 0


def test_loser_cannot_withdraw(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)
    _adjudicate_claimant_wins(contract, direct_vm, dispute_id)

    assert contract.get_payout_claim(dispute_id, bob.as_hex) == 0
    direct_vm.sender = bob
    with direct_vm.expect_revert("No payout to withdraw"):
        contract.withdraw_payout(dispute_id)


def test_double_withdraw_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)
    _adjudicate_claimant_wins(contract, direct_vm, dispute_id)

    direct_vm.sender = alice
    contract.withdraw_payout(dispute_id)

    with direct_vm.expect_revert("Nothing to withdraw"):
        contract.withdraw_payout(dispute_id)


def test_split_both_parties_withdraw(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    _setup_adjudication_mocks(
        direct_vm, verdict="split", confidence=65, refs=[CLAIM_URL, RESP_URL]
    )
    contract.adjudicate(dispute_id)

    direct_vm.sender = alice
    contract.withdraw_payout(dispute_id)
    direct_vm.sender = bob
    contract.withdraw_payout(dispute_id)

    d = contract.get_dispute(dispute_id)
    assert d["claimant_withdrawn"] is True
    assert d["respondent_withdrawn"] is True


# ──────────────────────────────────────────────────────────────────────
# mutual settlement (escape hatch for ambiguous cases)
# ──────────────────────────────────────────────────────────────────────


def test_mutual_settlement_returns_each_deposit(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    direct_vm.sender = alice
    contract.request_settle(dispute_id)
    direct_vm.sender = bob
    contract.request_settle(dispute_id)
    direct_vm.sender = bob
    contract.settle(dispute_id)

    d = contract.get_dispute(dispute_id)
    assert d["status"] == "adjudicated"
    assert d["verdict"] == "split"
    assert d["reason"] == "Mutual settlement (no protocol fee)"
    # Each party gets their own deposit back; no protocol fee on settlement.
    assert d["claimant_payout"] == 1000
    assert d["respondent_payout"] == 1000
    assert contract.get_protocol_fees() == 0


def test_settle_requires_both_parties(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    direct_vm.sender = alice
    contract.request_settle(dispute_id)

    with direct_vm.expect_revert("Both parties must request settlement"):
        contract.settle(dispute_id)

    d = contract.get_dispute(dispute_id)
    assert d["status"] == "funded"


def test_settle_third_party_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    charlie = create_address("charlie")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    direct_vm.sender = charlie
    with direct_vm.expect_revert("Only a party to the dispute can settle"):
        contract.request_settle(dispute_id)


def test_adjudicate_blocked_after_both_agree_to_settle(direct_vm, direct_deploy):
    """Once BOTH parties have agreed to settle, neither side can unilaterally
    force an AI adjudication instead of the agreed settlement."""
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    direct_vm.sender = alice
    contract.request_settle(dispute_id)
    direct_vm.sender = bob
    contract.request_settle(dispute_id)

    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=80)
    direct_vm.sender = alice
    with direct_vm.expect_revert("Both parties agreed to settle"):
        contract.adjudicate(dispute_id)

    # The dispute can still be finalized via settle.
    direct_vm.sender = bob
    contract.settle(dispute_id)
    assert contract.get_dispute(dispute_id)["status"] == "adjudicated"


def test_adjudicate_allowed_when_only_one_party_wants_settle(direct_vm, direct_deploy):
    """A single party signalling settlement does not block AI adjudication."""
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    direct_vm.sender = alice
    contract.request_settle(dispute_id)

    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=80)
    contract.adjudicate(dispute_id)
    assert contract.get_dispute(dispute_id)["status"] == "adjudicated"


def test_settle_only_when_funded(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.sender = alice
    with direct_vm.expect_revert("Only funded disputes can be settled"):
        contract.request_settle(dispute_id)


# ──────────────────────────────────────────────────────────────────────
# emergency_withdraw (bounded unilateral recovery after timeout)
# ──────────────────────────────────────────────────────────────────────


def test_emergency_withdraw_after_timeout(direct_vm, direct_deploy):
    """After the recovery timeout, either party can unilaterally recover their
    own deposit — escrow can never be locked forever."""
    contract = direct_deploy("contracts/dispute_arbiter.py", 60)
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.warp("2024-01-01T00:00:00Z")
    _fund(contract, direct_vm, dispute_id, alice, bob)

    # Within the window: recovery not yet allowed.
    direct_vm.warp("2024-01-01T00:00:30Z")
    direct_vm.sender = alice
    with direct_vm.expect_revert("Recovery window has not opened yet"):
        contract.emergency_withdraw(dispute_id)

    # After the window: unilateral recovery.
    direct_vm.warp("2024-01-01T00:02:00Z")  # 120s > 60s
    direct_vm.sender = alice
    contract.emergency_withdraw(dispute_id)
    d = contract.get_dispute(dispute_id)
    assert d["claimant_exited"] is True
    assert d["claimant_deposit"] == 0
    assert d["status"] == "funded"  # only one side exited so far

    direct_vm.sender = bob
    contract.emergency_withdraw(dispute_id)
    d = contract.get_dispute(dispute_id)
    assert d["respondent_exited"] is True
    assert d["respondent_deposit"] == 0
    assert d["status"] == "withdrawn"
    assert contract.get_protocol_fees() == 0  # no fee on emergency recovery


def test_emergency_withdraw_outsider_reverted(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py", 60)
    alice = create_address("alice")
    bob = create_address("bob")
    charlie = create_address("charlie")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.warp("2024-01-01T00:00:00Z")
    _fund(contract, direct_vm, dispute_id, alice, bob)
    direct_vm.warp("2024-01-01T00:02:00Z")

    direct_vm.sender = charlie
    with direct_vm.expect_revert("Nothing to emergency-withdraw"):
        contract.emergency_withdraw(dispute_id)


def test_emergency_withdraw_twice_reverted(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py", 60)
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.warp("2024-01-01T00:00:00Z")
    _fund(contract, direct_vm, dispute_id, alice, bob)
    direct_vm.warp("2024-01-01T00:02:00Z")

    direct_vm.sender = alice
    contract.emergency_withdraw(dispute_id)
    with direct_vm.expect_revert("Nothing to emergency-withdraw"):
        contract.emergency_withdraw(dispute_id)


def test_adjudicate_blocked_after_exit(direct_vm, direct_deploy):
    """Once a party has emergency-withdrawn, no adjudication can run on the
    now-unbalanced escrow."""
    contract = direct_deploy("contracts/dispute_arbiter.py", 60)
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.warp("2024-01-01T00:00:00Z")
    _fund(contract, direct_vm, dispute_id, alice, bob)
    direct_vm.warp("2024-01-01T00:02:00Z")

    direct_vm.sender = alice
    contract.emergency_withdraw(dispute_id)

    _setup_adjudication_mocks(direct_vm, verdict="claimant", confidence=80)
    with direct_vm.expect_revert("A party has exited the escrow"):
        contract.adjudicate(dispute_id)


def test_settle_blocked_after_exit(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py", 60)
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)

    direct_vm.warp("2024-01-01T00:00:00Z")
    _fund(contract, direct_vm, dispute_id, alice, bob)
    direct_vm.warp("2024-01-01T00:02:00Z")

    direct_vm.sender = bob
    contract.emergency_withdraw(dispute_id)

    direct_vm.sender = alice
    contract.request_settle(dispute_id)
    direct_vm.sender = bob
    with direct_vm.expect_revert("A party has exited the escrow"):
        contract.settle(dispute_id)


def test_get_recovery_timeout(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py", 120)
    assert contract.get_recovery_timeout() == 120


# ──────────────────────────────────────────────────────────────────────
# protocol fees / ownership
# ──────────────────────────────────────────────────────────────────────


def test_owner_withdraws_protocol_fees(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    owner = create_address("default_sender")
    alice = create_address("alice")
    bob = create_address("bob")

    assert contract.get_owner() == owner.as_hex
    assert contract.get_protocol_fees() == 0

    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)
    _adjudicate_claimant_wins(contract, direct_vm, dispute_id)

    assert contract.get_protocol_fees() == 100

    direct_vm.sender = owner
    contract.withdraw_protocol_fees()
    assert contract.get_protocol_fees() == 0


def test_non_owner_cannot_withdraw_fees(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)
    _adjudicate_claimant_wins(contract, direct_vm, dispute_id)

    direct_vm.sender = alice
    with direct_vm.expect_revert("Only the owner can withdraw protocol fees"):
        contract.withdraw_protocol_fees()


def test_no_fees_to_withdraw_rejected(direct_vm, direct_deploy):
    contract = direct_deploy("contracts/dispute_arbiter.py")
    owner = create_address("default_sender")

    direct_vm.sender = owner
    with direct_vm.expect_revert("No protocol fees accrued"):
        contract.withdraw_protocol_fees()


# ──────────────────────────────────────────────────────────────────────
# external-caller tamper resistance
# ──────────────────────────────────────────────────────────────────────


def test_external_caller_cannot_touch_others_dispute(direct_vm, direct_deploy):
    """A non-party can neither deposit into, cancel, refund, settle, nor
    withdraw from a dispute they are not part of."""
    contract = direct_deploy("contracts/dispute_arbiter.py")
    alice = create_address("alice")
    bob = create_address("bob")
    charlie = create_address("charlie")

    dispute_id = _make_dispute(contract, direct_vm, alice, bob)
    _fund(contract, direct_vm, dispute_id, alice, bob)

    # A funded dispute is the live, interference-relevant state.
    direct_vm.sender = charlie
    direct_vm.value = 1000
    with direct_vm.expect_revert("Only the claimant or respondent can deposit"):
        contract.deposit(dispute_id)
    with direct_vm.expect_revert("Only a party to the dispute can cancel"):
        contract.cancel(dispute_id)
    with direct_vm.expect_revert("Nothing to refund for this dispute"):
        contract.refund(dispute_id)
    with direct_vm.expect_revert("Only a party to the dispute can settle"):
        contract.request_settle(dispute_id)

    # An adjudicated dispute's payout belongs to the winner only.
    _adjudicate_claimant_wins(contract, direct_vm, dispute_id)
    with direct_vm.expect_revert("Nothing to withdraw"):
        contract.withdraw_payout(dispute_id)
