#!/usr/bin/env python3
"""
resolve_disputes.py

Reads orders.csv, carrier_tracking.csv, and support_chat.txt for ReLoop disputes,
merges them per order_id, and applies the Dispute_Guidelines.md rules in order to
produce a verdict (Refund / Deny / Escalate), a confidence level, and a plain-English
reason for each case.

Design goal: simple and explainable over clever. Every verdict traces back to one or
two concrete facts you can point to (a tracking field, a specific chat line).

Usage:
    python3 resolve_disputes.py
        Runs on data/orders.csv, data/carrier_tracking.csv, data/support_chat.txt
        (paths can be overridden, see --help), writes results.csv, prints a summary.

    python3 resolve_disputes.py --case D1001
        Prints the full reasoning trail for one case only.

    python3 resolve_disputes.py --new-case sample_new_case.json
        Resolves a single case that isn't in any of the CSV/txt files at all -- the
        way you'd hand it a brand new dispute during a live review. See
        sample_new_case.json for the expected shape.
"""

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class ChatMessage:
    date: str
    speaker: str  # "Buyer" or "Seller"
    text: str


@dataclass
class Case:
    order_id: str
    buyer: str
    seller: str
    item: str
    price_usd: str
    dispute_reason: str
    filed_date: str
    tracking: dict
    messages: list


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_orders(path):
    with open(path, newline="", encoding="utf-8") as f:
        return {row["order_id"]: row for row in csv.DictReader(f)}


def parse_tracking(path):
    with open(path, newline="", encoding="utf-8") as f:
        return {row["order_id"]: row for row in csv.DictReader(f)}


_BLOCK_RE = re.compile(r"===\s*(\S+)\s*===\s*\n(.*?)(?=\n===|\Z)", re.DOTALL)
_LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\]\s*(Buyer|Seller):\s*(.*)$")


def parse_chat(path):
    text = Path(path).read_text(encoding="utf-8")
    out = {}
    for order_id, block in _BLOCK_RE.findall(text):
        messages = []
        for line in block.strip().splitlines():
            m = _LINE_RE.match(line.strip())
            if m:
                date, speaker, msg = m.groups()
                messages.append(ChatMessage(date, speaker, msg))
        out[order_id] = messages
    return out


def build_cases(orders, tracking, chats):
    cases = {}
    for order_id, order in orders.items():
        cases[order_id] = Case(
            order_id=order_id,
            buyer=order["buyer_handle"],
            seller=order["seller_handle"],
            item=order["item"],
            price_usd=order["price_usd"],
            dispute_reason=order["dispute_reason"],
            filed_date=order["dispute_filed_date"],
            tracking=tracking.get(order_id, {}),
            messages=chats.get(order_id, []),
        )
    return cases


# ---------------------------------------------------------------------------
# Evidence signals (Phase 1: turn raw text into structured facts)
# ---------------------------------------------------------------------------

NON_RECEIPT_RE = re.compile(
    r"never (received|got|arriv)|haven'?t (received|got)|don'?t see it|still (don'?t see|haven'?t)",
    re.I,
)
RECEIPT_CONFIRM_RE = re.compile(
    r"\bjust got\b|\blove the\b|\blooks great\b|\bgot (the|this|these|my)\b|\bcame in\b",
    re.I,
)
FAULT_ADMIT_RE = re.compile(
    r"mix(?:ed)?[- ]?up|mislabel|my (fault|bad|mistake)|shouldn'?t have|grabbed the wrong|"
    r"too thin|is covered|real fault|wrong (one|item) off the shelf",
    re.I,
)
REFUND_WORD_RE = re.compile(r"\brefund", re.I)
SELLER_REBUTTAL_RE = re.compile(
    r"not sure what happened|i believe it'?s genuine|don'?t have a receipt|"
    r"not sure what happened on your end",
    re.I,
)
NON_RECEIPT_DISPUTE_RE = re.compile(r"never received|unresponsive|never shipped", re.I)
RESOLUTION_RE = re.compile(
    r"never ?mind|found it|no problem|not a big deal|sorted|all good|"
    r"that'?s fine|partial refund is fine|sorry for the trouble",
    re.I,
)
ADDRESS_CHANGE_RE = re.compile(
    r"ship (this|it) to|updated the shipping address|po box|change (my|the) address",
    re.I,
)
COUNTERFEIT_RE = re.compile(r"counterfeit|authentic|genuine", re.I)
CONDITION_RE = re.compile(r"not as described|scratch|scuff|used\b", re.I)

NON_DELIVERED_STATUSES = {"in transit", "label created", "lost", "returned to sender"}


def full_text(messages, speaker=None):
    if speaker:
        return " ".join(m.text for m in messages if m.speaker == speaker)
    return " ".join(m.text for m in messages)


def detect_contradiction(messages):
    """A buyer message confirming receipt, followed later by a buyer non-receipt claim."""
    for i, m in enumerate(messages):
        if m.speaker == "Buyer" and NON_RECEIPT_RE.search(m.text):
            for earlier in messages[:i]:
                if earlier.speaker == "Buyer" and RECEIPT_CONFIRM_RE.search(earlier.text):
                    return True, earlier.text.strip()
    return False, ""


def detect_resolved(messages):
    """True only if the *last* message in the thread is a resolution, not reopened after."""
    if not messages:
        return False
    last = messages[-1]
    if NON_RECEIPT_RE.search(last.text):
        return False
    return bool(RESOLUTION_RE.search(last.text))


def signature_context(buyer_handle, signed_by):
    """Best-effort, purely informational check of whether the signature name looks like
    the buyer's own handle. Never gates the verdict -- guideline #3 doesn't require the
    signer to *be* the buyer, only that a valid signature exists."""
    if not signed_by or "_" not in buyer_handle:
        return None
    first, _, last_token = buyer_handle.partition("_")
    initial = signed_by[0].lower() if signed_by else ""
    last_initial = last_token[0].lower() if last_token else ""
    surname = signed_by.split(".")[-1].lower() if "." in signed_by else ""
    if initial == first[0].lower() and surname.startswith(last_initial):
        return "consistent with the buyer's own account name"
    return "not clearly tied to the buyer's account name"


# ---------------------------------------------------------------------------
# The decision cascade (Phase 2: Dispute_Guidelines.md as code)
# ---------------------------------------------------------------------------

REFUND, DENY, ESCALATE = "Refund", "Deny", "Escalate"


def resolve_case(case: Case):
    reason_notes = []
    dispute_reason = case.dispute_reason or ""
    is_non_receipt = bool(NON_RECEIPT_DISPUTE_RE.search(dispute_reason))

    seller_text = full_text(case.messages, "Seller")
    buyer_text = full_text(case.messages, "Buyer")

    fault_admitted = bool(FAULT_ADMIT_RE.search(seller_text)) or (
        bool(REFUND_WORD_RE.search(seller_text))
        and not is_non_receipt
        and not SELLER_REBUTTAL_RE.search(seller_text)
    )
    address_flag = bool(ADDRESS_CHANGE_RE.search(buyer_text) or ADDRESS_CHANGE_RE.search(seller_text))
    contradiction, contradiction_line = detect_contradiction(case.messages)
    resolved_in_chat = detect_resolved(case.messages)

    status = (case.tracking.get("delivery_status") or "").strip().lower()
    signed_by = (case.tracking.get("signed_by") or "").strip()
    sig_required = (case.tracking.get("signature_required") or "").strip().lower() == "yes"
    notes = case.tracking.get("delivery_notes", "") or ""

    # --- Rule 1: never actually delivered -------------------------------------------------
    if status in NON_DELIVERED_STATUSES:
        detail = f"status '{case.tracking.get('delivery_status')}'"
        if case.tracking.get("last_scan_location"):
            detail += f", last scan at {case.tracking['last_scan_location']}"
        extra = " Seller also confirmed this directly in chat." if fault_admitted or REFUND_WORD_RE.search(seller_text) else ""
        return REFUND, "High", f"Carrier tracking shows the package was never delivered ({detail}).{extra}"

    # --- Rule 2: seller acknowledges a listing/fulfillment/fault issue (non-delivery disputes only) ---
    if fault_admitted and not is_non_receipt:
        return REFUND, "High", (
            f"Seller acknowledged the problem in chat and agreed to refund "
            f"(\"{dispute_reason}\")."
        )

    # --- Rule 5: already settled directly between the parties, checked before 3/4 because
    #     it is the most specific fit when it applies -------------------------------------
    if resolved_in_chat:
        return DENY, "High", (
            "No outstanding claim -- the buyer and seller resolved this directly in chat; "
            "nothing further to adjudicate."
        )

    # --- Rule 3: delivery confirmed + non-receipt claim, IF the confirmation is credible ---
    if is_non_receipt:
        if sig_required and signed_by:
            sig_note = signature_context(case.buyer, signed_by)
            extra = f" (signature name is {sig_note})" if sig_note else ""
            return DENY, "High", (
                f"Carrier confirms a signed delivery ({signed_by}){extra}; the non-receipt "
                f"claim isn't supported by anything else in the record."
            )
        if contradiction:
            return DENY, "High", (
                f"Buyer's own chat history contradicts the claim -- an earlier message "
                f"(\"{contradiction_line[:70]}\") indicates the item had already arrived."
            )
        if status == "delivered":
            if address_flag:
                return ESCALATE, "Low", (
                    "Chat shows an address change was discussed, but the tracking record's "
                    "own notes don't clearly resolve whether that address was actually used "
                    "-- this needs a human to check the account's address history directly, "
                    "not just this order's chat log."
                )
            return ESCALATE, "Low", (
                "Carrier shows 'delivered' but there's no signature and nothing in chat "
                "corroborates either side -- an unsigned scan alone isn't strong enough to "
                "confidently override a non-receipt claim, but there's also no evidence "
                "supporting the buyer over the carrier record."
            )

    # --- Rule 4: fallback -- genuine conflict or just not enough to decide -----------------
    if COUNTERFEIT_RE.search(buyer_text) or COUNTERFEIT_RE.search(dispute_reason):
        return ESCALATE, "Low", (
            "Authenticity can't be resolved from order/tracking records -- needs physical "
            "inspection, receipts, or a specialist, none of which are in this data."
        )
    if CONDITION_RE.search(dispute_reason):
        return ESCALATE, "Low", (
            "Item condition is disputed by both sides (buyer says worse than described, "
            "seller stands by the original condition) with no photo or inspection evidence "
            "in the data to settle it either way."
        )
    return ESCALATE, "Medium", (
        "The chat and tracking evidence don't clearly satisfy any guideline rule for this "
        "case -- flagging for manual review rather than guessing."
    )


# ---------------------------------------------------------------------------
# Rule 6: repeat-pattern flag (informational only -- never changes a verdict)
# ---------------------------------------------------------------------------

def build_history_flags(cases):
    from collections import Counter
    buyer_counts = Counter(c.buyer for c in cases.values())
    seller_counts = Counter(c.seller for c in cases.values())
    flags = {}
    for order_id, c in cases.items():
        bits = []
        if buyer_counts[c.buyer] >= 2:
            bits.append(f"buyer '{c.buyer}' has {buyer_counts[c.buyer]} disputes in this batch")
        if seller_counts[c.seller] >= 2:
            bits.append(f"seller '{c.seller}' has {seller_counts[c.seller]} disputes in this batch")
        flags[order_id] = "; ".join(bits)
    return flags


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_batch(orders_path, tracking_path, chat_path):
    orders = parse_orders(orders_path)
    tracking = parse_tracking(tracking_path)
    chats = parse_chat(chat_path)
    cases = build_cases(orders, tracking, chats)
    history_flags = build_history_flags(cases)

    results = []
    for order_id in sorted(cases, key=lambda x: (len(x), x)):
        case = cases[order_id]
        verdict, confidence, reason = resolve_case(case)
        results.append({
            "order_id": order_id,
            "buyer": case.buyer,
            "seller": case.seller,
            "item": case.item,
            "price_usd": case.price_usd,
            "dispute_reason": case.dispute_reason,
            "verdict": verdict,
            "confidence": confidence,
            "reason": reason,
            "history_note": history_flags.get(order_id, ""),
        })
    return results


def print_summary(results):
    from collections import Counter
    counts = Counter(r["verdict"] for r in results)
    print(f"\n{len(results)} cases resolved: "
          + ", ".join(f"{v}={counts[v]}" for v in ("Refund", "Deny", "Escalate") if counts[v]))
    print(f"{'order_id':8} {'verdict':9} {'conf':7} reason")
    print("-" * 100)
    for r in results:
        print(f"{r['order_id']:8} {r['verdict']:9} {r['confidence']:7} {r['reason'][:80]}")


def write_csv(results, out_path):
    fieldnames = ["order_id", "buyer", "seller", "item", "price_usd", "dispute_reason",
                  "verdict", "confidence", "reason", "history_note"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)


def resolve_new_case(json_path):
    """Resolve a single case supplied as JSON, without touching the CSV/txt files at all.
    Expected shape: see sample_new_case.json."""
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    order = data["order"]
    tracking = data.get("tracking", {})
    messages = [ChatMessage(m["date"], m["speaker"], m["text"]) for m in data.get("chat", [])]
    case = Case(
        order_id=order.get("order_id", "NEW"),
        buyer=order.get("buyer_handle", ""),
        seller=order.get("seller_handle", ""),
        item=order.get("item", ""),
        price_usd=order.get("price_usd", ""),
        dispute_reason=order.get("dispute_reason", ""),
        filed_date=order.get("dispute_filed_date", ""),
        tracking=tracking,
        messages=messages,
    )
    verdict, confidence, reason = resolve_case(case)
    print(f"order_id: {case.order_id}")
    print(f"dispute:  {case.dispute_reason}")
    print(f"verdict:    {verdict}")
    print(f"confidence: {confidence}")
    print(f"reason:     {reason}")


def print_one_case(order_id, orders_path, tracking_path, chat_path):
    orders = parse_orders(orders_path)
    tracking = parse_tracking(tracking_path)
    chats = parse_chat(chat_path)
    cases = build_cases(orders, tracking, chats)
    if order_id not in cases:
        print(f"No such order_id: {order_id}", file=sys.stderr)
        sys.exit(1)
    case = cases[order_id]
    verdict, confidence, reason = resolve_case(case)
    print(f"order_id:        {case.order_id}")
    print(f"buyer / seller:  {case.buyer} / {case.seller}")
    print(f"item / price:    {case.item} / ${case.price_usd}")
    print(f"dispute reason:  {case.dispute_reason}")
    print(f"tracking:        {case.tracking}")
    print("chat:")
    for m in case.messages:
        print(f"  [{m.date}] {m.speaker}: {m.text}")
    print()
    print(f"VERDICT:    {verdict}")
    print(f"CONFIDENCE: {confidence}")
    print(f"REASON:     {reason}")


def main():
    ap = argparse.ArgumentParser(description="Resolve ReLoop disputes from orders/tracking/chat data.")
    ap.add_argument("--orders", default="data/orders.csv")
    ap.add_argument("--tracking", default="data/carrier_tracking.csv")
    ap.add_argument("--chat", default="data/support_chat.txt")
    ap.add_argument("--out", default="results.csv")
    ap.add_argument("--case", help="Print the full reasoning trail for one order_id only.")
    ap.add_argument("--new-case", help="Path to a JSON file describing a single new, unseen case.")
    args = ap.parse_args()

    if args.new_case:
        resolve_new_case(args.new_case)
        return
    if args.case:
        print_one_case(args.case, args.orders, args.tracking, args.chat)
        return

    results = run_batch(args.orders, args.tracking, args.chat)
    write_csv(results, args.out)
    print_summary(results)
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
