# ReLoop Dispute Resolution Tool — how it works

## What this is

A small, self-contained Python tool that reads three data sources for a buyer/seller
dispute — the order record, the carrier's tracking record, and the support chat
transcript — and produces a verdict for each one: **Refund**, **Deny**, or
**Escalate**, plus a confidence level and a plain-English reason tied to specific
evidence.

There's exactly one file that matters: `resolve_disputes.py`. No external
dependencies, no paid APIs, no network calls — everything runs offline with the
Python standard library. That's a deliberate choice: the logic is a direct,
readable translation of `Dispute_Guidelines.md`, and every verdict should be
defensible by pointing at one or two concrete facts, not a black box.

## How to run it

```bash
# From inside this folder:
python3 resolve_disputes.py
```

This reads `data/orders.csv`, `data/carrier_tracking.csv`, and
`data/support_chat.txt`, resolves all 20 sample disputes, writes `results.csv`,
and prints a one-line-per-case summary to the terminal.


**Resolve a brand-new case that isn't in any of the three files at all** —
```bash
python3 resolve_disputes.py --new-case sample_new_case.json
```
`sample_new_case.json` shows the expected shape: an `order` block (same fields as
a row of `orders.csv`), a `tracking` block (same fields as a row of
`carrier_tracking.csv`), and a `chat` list of `{date, speaker, text}` messages. 


## The three-layer structure

**1. Parsing (turn three file formats into one record per dispute).**
`parse_orders`, `parse_tracking`, and `parse_chat` each read one file and key it by
`order_id`. `parse_chat` splits `support_chat.txt` on its `=== ID ===` markers and
then on the `[date] Speaker: text` line format. `build_cases` joins the three into
one `Case` object per dispute — this is the only place that needs to know about
file formats; everything downstream just works with `Case` objects, so it doesn't
care whether the case came from the sample files or a hand-written JSON blob.

**2. Signal extraction (turn free text into yes/no facts).**
A handful of regexes turn chat text into structured signals:
- `NON_RECEIPT_RE` — does a message claim the item never arrived?
- `RECEIPT_CONFIRM_RE` — does a message imply the buyer already has the item
  ("just got," "love the," "looks great")?
- `FAULT_ADMIT_RE` — does the seller admit an error (mix-up, mislabeled, my
  fault, too-thin packaging, "is covered")?
- `SELLER_REBUTTAL_RE` — does the seller push back on the buyer's account instead
  of agreeing?
- `RESOLUTION_RE` — does the thread end on a satisfied note ("never mind, found
  it," "no problem")?
- `ADDRESS_CHANGE_RE` — was a shipping-address change discussed in chat at all?
- `COUNTERFEIT_RE` / `CONDITION_RE` — does the dispute turn on authenticity or
  item condition rather than delivery?

`detect_contradiction` walks the message list in order and flags it when a buyer
message confirming receipt is followed later by a buyer message claiming
non-receipt (this is what catches D1001 and D1018). `detect_resolved` only looks
at the *last* message in the thread, specifically so a mid-conversation "never
mind, found it" that gets reopened later (D1009) doesn't get mistaken for a
settled case.

**3. The decision cascade (`resolve_case`) — the guidelines as code.**
This is a straight if/elif chain, checked in this order:

| # | Guideline rule | Code check | Verdict |
|---|---|---|---|
| 1 | Never delivered / lost / label never scanned | `delivery_status` is `In Transit`, `Label Created`, `Lost`, or `Returned to Sender` | Refund, High |
| 2 | Seller acknowledges a fulfillment/listing/product-fault error | `FAULT_ADMIT_RE` on seller text, or seller says "refund" and doesn't rebut, for any dispute type that isn't itself a non-receipt claim | Refund, High |
| 5 | Already resolved directly between the parties | Last message in the thread is a resolution phrase | Deny (no live claim), High |
| 3 | Confirmed delivery + non-receipt claim | Real signature present, **or** buyer's own earlier words confirm receipt | Deny, High |
| 3b | "Delivered" but *not* credibly confirmed | Status is Delivered, no signature, no corroboration either way | **Escalate**, Low |
| 4 | Everything else — genuine conflict or thin evidence | Authenticity claims, disputed condition, or no rule cleanly fits | Escalate, Low/Medium |

Rule 6 (history/pattern) is intentionally **not** in this table — it never
participates in the verdict. It's computed separately (`build_history_flags`) and
attached to the output as an informational `history_note` column only, because the
guideline is explicit that a pattern is a reason to look closer, not a reason to
skip the evidence in front of you.


