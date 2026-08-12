"""Deploy DisputeArbiter to GenLayer Studio (studionet, free fees) and test
every public method end-to-end with real consensus, web and LLM.

Usage:
    python scripts/onchain_test.py

Creates fresh throwaway accounts, funds them via the studionet faucet RPC
(sim_fundAccount), deploys the contract, and exercises the full lifecycle:
create_dispute, deposit, adjudicate (real web + LLM consensus), withdraw_payout,
withdraw_protocol_fees, request_settle/settle, cancel/refund, plus access-control
rejections for an outsider.
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

# Two byte-stable, publicly fetchable evidence URLs (one per party).
CLAIM_URL = "https://raw.githubusercontent.com/genlayerlabs/genlayer-py/main/README.md"
RESP_URL = "https://raw.githubusercontent.com/genlayerlabs/genlayer-py/main/LICENSE"

DEPOSIT = 1_000_000  # wei
FEE_BPS = 500  # 5%


def rpc(method, params, _retries=10):
    for attempt in range(_retries):
        resp = requests.post(STUDIO_API, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=60)
        if resp.status_code == 429:
            print("  rate limited; waiting 65s...")
            time.sleep(65)
            continue
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"{method} error: {body['error']}")
        return body.get("result")
    raise RuntimeError(f"{method}: rate limited after {_retries} retries")


def fund(account, amount=10 ** 21):
    rpc("sim_fundAccount", [account.address, amount])
    time.sleep(3)


def balance(account):
    return int(rpc("eth_getBalance", [account.address, "latest"]), 16)


def evidence(url):
    raw = requests.get(url, timeout=30).content
    return json.dumps([{"url": url, "hash": hashlib.sha256(raw).hexdigest()}])


def rate_retry(fn, *args, **kwargs):
    """Retry a client call when studionet rate-limits (30 req/min)."""
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            if "rate limit" in msg.lower() or "Rate limit" in msg or "-32029" in msg:
                print("  rate limited; waiting 65s...")
                time.sleep(65)
                continue
            raise


def wait_receipt(client, tx_hash, label, interval=8000, retries=90):
    def _wait():
        return client.wait_for_transaction_receipt(
            tx_hash, status=TransactionStatus.ACCEPTED, interval=interval, retries=retries
        )

    receipt = rate_retry(_wait)
    ok = tx_execution_succeeded(receipt)
    status_name = receipt.get("status_name", "?")
    print(f"  [{label}] tx={tx_hash} status={status_name} success={ok}")
    if not ok:
        leader = receipt["consensus_data"]["leader_receipt"][0]
        print(f"    stderr: {leader.get('genvm_result', {}).get('stderr', '')[:400]}")
    time.sleep(3)
    return receipt, ok


def main():
    print("=== Setting up accounts ===")
    owner = create_account()
    alice = create_account()
    bob = create_account()
    charlie = create_account()
    for name, acct in (("owner", owner), ("alice", alice), ("bob", bob), ("charlie", charlie)):
        fund(acct)
        bal = balance(acct)
        print(f"  {name}: {acct.address}  balance={bal} wei")
        assert bal > 0, f"{name} was not funded"

    client = create_client(chain=studionet, account=owner)
    print(f"\n=== Deploying DisputeArbiter ===")
    code = CONTRACT_PATH.read_text(encoding="utf-8")
    deploy_hash = rate_retry(lambda: client.deploy_contract(code=code, account=owner))
    receipt = rate_retry(lambda: client.wait_for_transaction_receipt(
        deploy_hash, status=TransactionStatus.ACCEPTED, interval=4000, retries=90
    ))
    assert tx_execution_succeeded(receipt), f"Deploy failed: {receipt}"
    addr = receipt["to_address"]
    print(f"  deployed at {addr}")

    read = lambda fn, *args, **kw: rate_retry(client.read_contract, addr, fn, args=list(args), **kw)
    write = lambda fn, account, args=None, value=0: rate_retry(
        client.write_contract, addr, fn, account=account, args=args or [], value=value
    )

    # ---- view: get_owner ----
    owner_hex = read("get_owner")
    assert owner_hex == owner.address, f"owner mismatch {owner_hex} != {owner.address}"
    print(f"\n[1/9] get_owner OK: {owner_hex}")

    # ---- create_dispute (alice vs bob) ----
    print("\n[2/9] create_dispute")
    d1 = "d0"
    args = [
        bob.address,
        "Repayment of an acknowledged loan",
        "I lent the respondent 100 GEN and the respondent acknowledged the debt in writing.",
        "I acknowledge the debt of 100 GEN to the claimant.",
        "Rule for the claimant if the respondent acknowledges the debt; otherwise rule for the respondent.",
        evidence(CLAIM_URL),
        evidence(RESP_URL),
        DEPOSIT,
        FEE_BPS,
    ]
    rec, ok = wait_receipt(client, write("create_dispute", alice, args=args), "create_dispute")
    assert ok
    dispute = read("get_dispute", d1)
    assert dispute["status"] == "pending" and dispute["claimant"] == alice.address
    print(f"    dispute={d1} status={dispute['status']} fee_bps={dispute['fee_bps']}")

    # ---- deposit both sides ----
    print("\n[3/9] deposit")
    _, ok = wait_receipt(client, write("deposit", alice, args=[d1], value=DEPOSIT), "deposit alice")
    assert ok
    _, ok = wait_receipt(client, write("deposit", bob, args=[d1], value=DEPOSIT), "deposit bob")
    assert ok
    dispute = read("get_dispute", d1)
    assert dispute["status"] == "funded"
    assert dispute["claimant_deposit"] == DEPOSIT and dispute["respondent_deposit"] == DEPOSIT
    print(f"    status={dispute['status']} claimant_deposit={dispute['claimant_deposit']} respondent_deposit={dispute['respondent_deposit']}")

    # ---- emergency_withdraw guard: recovery window not open yet ----
    print("\n[3b/9] emergency_withdraw (recovery window not open -> rejected)")
    _, ok = wait_receipt(client, write("emergency_withdraw", alice, args=[d1]), "emergency_withdraw early")
    assert not ok, "emergency_withdraw should be rejected before the recovery timeout"
    print("    guard OK: recovery window is bounded and not yet open")

    # ---- adjudicate (real web + LLM consensus) ----
    print("\n[4/9] adjudicate (real web + LLM, 5 validators)")
    rec, ok = wait_receipt(client, write("adjudicate", charlie, args=[d1]), "adjudicate", retries=120)
    assert ok, "adjudicate failed (the LLM may have returned low confidence; retry the script)"
    dispute = read("get_dispute", d1)
    assert dispute["status"] == "adjudicated"
    total = 2 * DEPOSIT
    fee = total * FEE_BPS // 10000
    net = total - fee
    if dispute["verdict"] == "claimant":
        assert dispute["claimant_payout"] == net and dispute["respondent_payout"] == 0
    elif dispute["verdict"] == "respondent":
        assert dispute["respondent_payout"] == net and dispute["claimant_payout"] == 0
    else:
        assert dispute["claimant_payout"] + dispute["respondent_payout"] == net
    print(f"    verdict={dispute['verdict']} confidence={dispute['confidence']} references={dispute['references']}")
    print(f"    claimant_payout={dispute['claimant_payout']} respondent_payout={dispute['respondent_payout']}")

    # ---- withdraw_payout (winner) ----
    print("\n[5/9] withdraw_payout")
    winner = alice if dispute["claimant_payout"] > 0 else bob
    _, ok = wait_receipt(client, write("withdraw_payout", winner, args=[d1]), "withdraw_payout")
    assert ok
    dispute = read("get_dispute", d1)
    assert dispute["claimant_withdrawn"] or dispute["respondent_withdrawn"]
    print(f"    withdrawn by {winner.address}")

    # ---- protocol fees ----
    print("\n[6/9] withdraw_protocol_fees")
    fees_before = read("get_protocol_fees")
    assert fees_before == fee, f"expected fee {fee}, got {fees_before}"
    _, ok = wait_receipt(client, write("withdraw_protocol_fees", owner, args=[]), "withdraw fees")
    assert ok
    assert read("get_protocol_fees") == 0
    print(f"    withdrew {fees_before} wei, remaining {read('get_protocol_fees')}")

    # ---- mutual settlement path (dispute d1) ----
    print("\n[7/9] request_settle + settle (dispute d1)")
    d2 = "d1"
    args = [
        bob.address,
        "Ambiguous delivery dispute",
        "I delivered the work on Friday.",
        "The work was delivered on Monday.",
        "Rule for the claimant if delivery happened before the weekend; otherwise respondent.",
        evidence(RESP_URL),
        evidence(CLAIM_URL),
        DEPOSIT,
        FEE_BPS,
    ]
    rec, ok = wait_receipt(client, write("create_dispute", alice, args=args), "create d1")
    assert ok
    _, ok = wait_receipt(client, write("deposit", alice, args=[d2], value=DEPOSIT), "dep d1 alice")
    assert ok
    _, ok = wait_receipt(client, write("deposit", bob, args=[d2], value=DEPOSIT), "dep d1 bob")
    assert ok
    _, ok = wait_receipt(client, write("request_settle", alice, args=[d2]), "req_settle alice")
    assert ok
    _, ok = wait_receipt(client, write("request_settle", bob, args=[d2]), "req_settle bob")
    assert ok
    _, ok = wait_receipt(client, write("settle", bob, args=[d2]), "settle")
    assert ok
    dispute = read("get_dispute", d2)
    assert dispute["status"] == "adjudicated" and dispute["verdict"] == "split"
    assert dispute["claimant_payout"] == DEPOSIT and dispute["respondent_payout"] == DEPOSIT
    print(f"    status={dispute['status']} verdict={dispute['verdict']} payouts={dispute['claimant_payout']}/{dispute['respondent_payout']} (no fee)")

    # ---- cancel + refund path (dispute d2) ----
    print("\n[8/9] cancel + refund (dispute d2)")
    d3 = "d2"
    args = [
        bob.address,
        "Dispute that never proceeds",
        "Statement from claimant.",
        "Statement from respondent.",
        "Any reasonable rule.",
        evidence(CLAIM_URL),
        evidence(RESP_URL),
        DEPOSIT,
        FEE_BPS,
    ]
    rec, ok = wait_receipt(client, write("create_dispute", alice, args=args), "create d2")
    assert ok
    _, ok = wait_receipt(client, write("deposit", alice, args=[d3], value=DEPOSIT), "dep d2 alice")
    assert ok
    _, ok = wait_receipt(client, write("cancel", bob, args=[d3]), "cancel d2")
    assert ok
    dispute = read("get_dispute", d3)
    assert dispute["status"] == "cancelled"
    _, ok = wait_receipt(client, write("refund", alice, args=[d3]), "refund d2")
    assert ok
    dispute = read("get_dispute", d3)
    assert dispute["claimant_withdrawn"] is True and dispute["claimant_deposit"] == 0
    print(f"    status={dispute['status']} claimant_withdrawn={dispute['claimant_withdrawn']}")

    # ---- access control: outsider rejected ----
    print("\n[9/9] access control (outsider rejected)")
    rec, ok = wait_receipt(client, write("deposit", charlie, args=[d2], value=DEPOSIT), "outsider deposit")
    assert not ok, "outsider deposit should fail"
    rec, ok = wait_receipt(client, write("withdraw_protocol_fees", charlie, args=[]), "outsider fees")
    assert not ok, "outsider fee withdrawal should fail"
    rec, ok = wait_receipt(client, write("withdraw_payout", charlie, args=[d2]), "outsider payout")
    assert not ok, "outsider payout should fail"
    print("    outsider deposit / fee withdrawal / payout all rejected as expected")

    print("\n=== ALL METHODS PASSED ON STUDIONET ===")


if __name__ == "__main__":
    main()
