"""Integration tests for DisputeArbiter against a live GenLayer environment.

The deterministic escrow lifecycle (create -> deposit -> cancel -> refund,
mutual settlement, protocol-fee accounting) is exercised end-to-end here. The
adjudication step relies on web + LLM consensus, which is validated
deterministically in `tests/direct/` with mocks.

Run with a live node (GLSim or Studio):

    glsim --port 4000 --validators 5          # terminal 1
    gltest tests/integration/ -v -s           # terminal 2
"""

import hashlib
import json

import pytest

from gltest import get_contract_factory, create_account, get_default_account
from gltest.assertions import tx_execution_succeeded

pytestmark = pytest.mark.integration

CLAIM_URL = "https://example.com/claim"
RESP_URL = "https://example.com/response"
CLAIM_BODY = "Delivery logs and agreed specification."
RESP_BODY = "Rebuttal notes and payment records."


def _evidence(url, body):
    return json.dumps(
        [{"url": url, "hash": hashlib.sha256(body.encode("utf-8")).hexdigest()}]
    )


def _create_dispute(contract, claimant, respondent, **overrides):
    args = dict(
        respondent=respondent.address,
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
    receipt = contract.create_dispute(args=list(args.values())).transact(
        account=claimant
    )
    assert tx_execution_succeeded(receipt)
    return receipt.get("execution_result", {}).get("return_value")


def test_create_and_fund_dispute():
    factory = get_contract_factory("DisputeArbiter")
    contract = factory.deploy(account=get_default_account())
    claimant = get_default_account()
    respondent = create_account()

    dispute_id = _create_dispute(contract, claimant, respondent)
    assert dispute_id == "d0"

    dispute = contract.get_dispute(args=[dispute_id]).call()
    assert dispute["status"] == "pending"
    assert dispute["required_deposit"] == 1000
    assert dispute["claimant_evidence"][0]["url"] == CLAIM_URL

    claimant_receipt = contract.deposit(args=[dispute_id]).transact(
        account=claimant, value=1000
    )
    assert tx_execution_succeeded(claimant_receipt)

    respondent_contract = contract.at(account=respondent)
    respondent_receipt = respondent_contract.deposit(args=[dispute_id]).transact(
        value=1000
    )
    assert tx_execution_succeeded(respondent_receipt)

    dispute = contract.get_dispute(args=[dispute_id]).call()
    assert dispute["status"] == "funded"
    assert dispute["claimant_deposit"] == 1000
    assert dispute["respondent_deposit"] == 1000


def test_cancel_and_refund_returns_deposits():
    factory = get_contract_factory("DisputeArbiter")
    contract = factory.deploy(account=get_default_account())
    claimant = get_default_account()
    respondent = create_account()

    dispute_id = _create_dispute(contract, claimant, respondent)

    claimant_receipt = contract.deposit(args=[dispute_id]).transact(
        account=claimant, value=1000
    )
    assert tx_execution_succeeded(claimant_receipt)

    # Respondent backs out before the escrow is fully funded.
    cancel_receipt = contract.cancel(args=[dispute_id]).transact(account=respondent)
    assert tx_execution_succeeded(cancel_receipt)

    dispute = contract.get_dispute(args=[dispute_id]).call()
    assert dispute["status"] == "cancelled"

    refund_receipt = contract.refund(args=[dispute_id]).transact(account=claimant)
    assert tx_execution_succeeded(refund_receipt)

    dispute = contract.get_dispute(args=[dispute_id]).call()
    assert dispute["claimant_withdrawn"] is True
    assert dispute["claimant_deposit"] == 0


def test_mutual_settlement_returns_each_deposit():
    factory = get_contract_factory("DisputeArbiter")
    contract = factory.deploy(account=get_default_account())
    claimant = get_default_account()
    respondent = create_account()

    dispute_id = _create_dispute(contract, claimant, respondent)

    assert tx_execution_succeeded(
        contract.deposit(args=[dispute_id]).transact(account=claimant, value=1000)
    )
    assert tx_execution_succeeded(
        contract.at(account=respondent)
        .deposit(args=[dispute_id])
        .transact(value=1000)
    )

    assert tx_execution_succeeded(
        contract.request_settle(args=[dispute_id]).transact(account=claimant)
    )
    assert tx_execution_succeeded(
        contract.at(account=respondent)
        .request_settle(args=[dispute_id])
        .transact()
    )
    assert tx_execution_succeeded(
        contract.at(account=respondent).settle(args=[dispute_id]).transact()
    )

    dispute = contract.get_dispute(args=[dispute_id]).call()
    assert dispute["status"] == "adjudicated"
    assert dispute["verdict"] == "split"
    assert dispute["claimant_payout"] == 1000
    assert dispute["respondent_payout"] == 1000
    assert contract.get_protocol_fees(args=[]).call() == 0


def test_third_party_cannot_deposit():
    factory = get_contract_factory("DisputeArbiter")
    contract = factory.deploy(account=get_default_account())
    claimant = get_default_account()
    respondent = create_account()
    outsider = create_account()

    dispute_id = _create_dispute(contract, claimant, respondent)

    outsider_receipt = contract.deposit(args=[dispute_id]).transact(
        account=outsider, value=1000
    )
    assert not tx_execution_succeeded(outsider_receipt)

    dispute = contract.get_dispute(args=[dispute_id]).call()
    assert dispute["claimant_deposit"] == 0
    assert dispute["respondent_deposit"] == 0
