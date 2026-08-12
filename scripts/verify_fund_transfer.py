"""Verify that DisputeArbiter ACTUALLY MOVES FUNDS on studionet.

Unlike onchain_test.py (which only checks tx success + state flags), this script
checks real on-chain balances before/after every value transfer:

  deposit(claimant/respondent)   -> contract balance grows, sender balance falls
  withdraw_payout(winner)        -> winner's EOA balance grows
  withdraw_protocol_fees(owner)  -> owner's EOA balance grows
  refund(party)                  -> party's EOA balance grows

Usage:
    python scripts/verify_fund_transfer.py
"""

import hashlib
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from genlayer_py import create_client, create_account
from genlayer_py.chains import studionet
from genlayer_py.types.transactions import TransactionStatus
from gltest.assertions import tx_execution_succeeded

STUDIO_API = studionet.rpc_urls["default"]["http"][0]
CONTRACT_PATH = Path(__file__).resolve().parent.parent / "dispute_arbiter.py"

CLAIM_URL = "https://raw.githubusercontent.com/genlayerlabs/genlayer-py/main/README.md"
RESP_URL = "https://raw.githubusercontent.com/genlayerlabs/genlayer-py/main/LICENSE"
DEPOSIT = 1_000_000  # wei
FEE_BPS = 500


def rpc(method, params):
    resp = requests.post(STUDIO_API, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=60)
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"{method} error: {body['error']}")
    return body.get("result")


def bal(address):
    return int(rpc("eth_getBalance", [address, "latest"]), 16)


def fund(account, amount=10 ** 21):
    rpc("sim_fundAccount", [account.address, amount])


def evidence(url):
    raw = requests.get(url, timeout=30).content
    return json.dumps([{"url": url, "hash": hashlib.sha256(raw).hexdigest()}])


def send_tx(client, tx_hash, label, interval=4000, retries=90):
    receipt = client.wait_for_transaction_receipt(
        tx_hash, status=TransactionStatus.ACCEPTED, interval=interval, retries=retries
    )
    ok = tx_execution_succeeded(receipt)
    print(f"  {label}: tx={tx_hash} success={ok}")
    if not ok:
        leader = receipt["consensus_data"]["leader_receipt"][0]
        print(f"    stderr: {leader.get('genvm_result', {}).get('stderr', '')[:300]}")
    return ok


def wait_balance(address, target, label, timeout_s=90):
    """Poll an EOA balance until it reaches the expected value."""
    deadline = time.time() + timeout_s
    last = bal(address)
    while time.time() < deadline:
        last = bal(address)
        if last >= target:
            print(f"  {label}: balance={last} (target {target}) OK")
            return last
        time.sleep(5)
    print(f"  {label}: balance={last} but expected >= {target} TIMEOUT")
    return last


def main():
    print("=== Fresh accounts + funding ===")
    owner = create_account()
    alice = create_account()
    bob = create_account()
    accounts = {"owner": owner, "alice": alice, "bob": bob}
    for name, acct in accounts.items():
        fund(acct)

    client = create_client(chain=studionet, account=owner)
    print("\n=== Deploy fresh contract ===")
    deploy_hash = client.deploy_contract(code=CONTRACT_PATH.read_text(encoding="utf-8"), account=owner)
    receipt = client.wait_for_transaction_receipt(deploy_hash, status=TransactionStatus.ACCEPTED, interval=4000, retries=90)
    assert tx_execution_succeeded(receipt)
    addr = receipt["to_address"]
    print(f"  contract at {addr}")

    contract_bal0 = bal(addr)
    print(f"  contract balance at deploy: {contract_bal0}")
    assert contract_bal0 == 0, "contract should start with 0 balance"

    read = lambda fn, *args, **kw: client.read_contract(addr, fn, args=list(args), **kw)
    write = lambda fn, account, args=None, value=0: client.write_contract(addr, fn, account=account, args=args or [], value=value)

    # ---- create + deposit: does the money ENTER the contract? ----
    print("\n=== deposit: funds must ENTER the contract ===")
    alice_bal0 = bal(alice.address)
    bob_bal0 = bal(bob.address)
    args = [
        bob.address, "Loan repayment", "I lent 100 GEN; respondent acknowledged the debt.",
        "I acknowledge the debt.", "Rule for claimant if the respondent acknowledges the debt.",
        evidence(CLAIM_URL), evidence(RESP_URL), DEPOSIT, FEE_BPS,
    ]
    assert send_tx(client, write("create_dispute", alice, args=args), "create_dispute")
    assert send_tx(client, write("deposit", alice, args=["d0"], value=DEPOSIT), "deposit alice")
    assert send_tx(client, write("deposit", bob, args=["d0"], value=DEPOSIT), "deposit bob")

    contract_bal1 = bal(addr)
    print(f"  contract balance after both deposits: {contract_bal1} (expected {2 * DEPOSIT})")
    assert contract_bal1 >= 2 * DEPOSIT, "contract did not receive the deposits"
    assert bal(alice.address) <= alice_bal0, "alice balance did not decrease"
    assert bal(bob.address) <= bob_bal0, "bob balance did not decrease"
    print("  deposit fund movement CONFIRMED (sender -> contract)")

    # ---- adjudicate + withdraw: does the winner actually RECEIVE funds? ----
    print("\n=== withdraw_payout: winner must actually RECEIVE funds ===")
    assert send_tx(client, write("adjudicate", bob, args=["d0"]), "adjudicate", retries=120)
    dispute = read("get_dispute", "d0")
    winner = alice if dispute["claimant_payout"] > 0 else bob
    payout = dispute["claimant_payout"] if winner is alice else dispute["respondent_payout"]
    print(f"  verdict={dispute['verdict']} payout_to_{'alice' if winner is alice else 'bob'}={payout}")

    winner_bal0 = bal(winner.address)
    assert send_tx(client, write("withdraw_payout", winner, args=["d0"]), "withdraw_payout")
    winner_bal1 = wait_balance(winner.address, winner_bal0 + payout, f"winner balance after withdraw")
    assert winner_bal1 >= winner_bal0 + payout, "winner did not receive the payout!"
    print(f"  winner fund movement CONFIRMED (contract -> winner EOA): +{winner_bal1 - winner_bal0}")

    contract_bal2 = bal(addr)
    expected_fee = 2 * DEPOSIT * FEE_BPS // 10000
    print(f"  contract balance after payout: {contract_bal2} (expected fee {expected_fee})")
    assert contract_bal2 >= expected_fee, "fee was not retained in the contract"

    # ---- protocol fees: does the OWNER actually receive funds? ----
    print("\n=== withdraw_protocol_fees: owner must actually RECEIVE funds ===")
    fee = read("get_protocol_fees")
    owner_bal0 = bal(owner.address)
    assert send_tx(client, write("withdraw_protocol_fees", owner, args=[]), "withdraw_protocol_fees")
    owner_bal1 = wait_balance(owner.address, owner_bal0 + fee, "owner balance after fee withdrawal")
    assert owner_bal1 >= owner_bal0 + fee, "owner did not receive the fees!"
    print(f"  fee fund movement CONFIRMED (contract -> owner EOA): +{owner_bal1 - owner_bal0}")

    # ---- cancel + refund: does the depositor actually get funds back? ----
    print("\n=== cancel + refund: depositor must actually RECEIVE funds back ===")
    args = [
        bob.address, "Never proceeds", "s1", "s2", "rule",
        evidence(RESP_URL), evidence(CLAIM_URL), DEPOSIT, FEE_BPS,
    ]
    assert send_tx(client, write("create_dispute", alice, args=args), "create d1")
    assert send_tx(client, write("deposit", alice, args=["d1"], value=DEPOSIT), "deposit d1 alice")
    assert send_tx(client, write("cancel", bob, args=["d1"]), "cancel d1")

    alice_bal0 = bal(alice.address)
    assert send_tx(client, write("refund", alice, args=["d1"]), "refund d1")
    alice_bal1 = wait_balance(alice.address, alice_bal0 + DEPOSIT, "alice balance after refund")
    assert alice_bal1 >= alice_bal0 + DEPOSIT, "alice did not receive the refund!"
    print(f"  refund fund movement CONFIRMED (contract -> alice EOA): +{alice_bal1 - alice_bal0}")

    print("\n=== ALL FUND TRANSFERS VERIFIED ON-CHAIN ===")


if __name__ == "__main__":
    main()
