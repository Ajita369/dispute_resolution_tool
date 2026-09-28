# Write-up: ReLoop dispute resolution tool

## Where a simple rule would get it wrong

- **"Delivered" is not one signal, it's two, and they disagree in strength.** A carrier
  scan of "Delivered" with a valid signature is strong evidence. A "Delivered, no
  signature, left at door" scan is much weaker — it just means the driver scanned it,
  not that the buyer has it. A rule that treats both the same way (e.g. "delivered →
  deny non-receipt claims") would incorrectly deny four cases in this batch (D1003,
  D1008, D1013, D1017) that have no other evidence at all. I split this into two
  tiers: a real signature (or the buyer's own words) is decisive; a bare unsigned scan
  on its own is not enough to override a claim, and escalates instead.

- **"Seller offered a refund" and "seller admitted fault" aren't quite the same
  signal, but they usually travel together.** Most refund cases here have the seller
  explicitly saying "refund"/"refunding." One (D1002) doesn't — the seller only
  says "I think I mixed up your order," with no promised refund at all. A rule keyed
  purely on the word "refund" would miss it. I treat an explicit fault-admission
  phrase (mix-up, mislabeled, my fault, etc.) as sufficient on its own, and only fall
  back to "seller said refund + didn't dispute the claim" when there's no explicit
  admission phrase.

- **A pattern in someone's history is not itself a verdict.** One buyer handle
  (`quick_flip99`) filed 4 of the 20 disputes, and all four happen to be exactly the
  weak "delivered, unsigned, no other evidence" pattern above. It would be tempting to
  let that repetition tip the scale toward "deny" as suspicious. The guidelines are
  explicit that a pattern is a reason to look closer, not a reason to skip the
  evidence in front of you — so the tool surfaces this as a separate flag for a human
  reviewer, and it never changes the verdict itself.

## Cases I was least confident about, and why

- **D1015 (address change mid-shipment).** The buyer asked to ship to a PO box, the
  seller said they'd updated it, and the package was later marked delivered to "the
  street address on file at ship time." That phrasing implies the update either didn't
  take effect before shipment, or the tracking system's own address record never
  matched what the seller told the buyer. That's a genuine conflict between two
  internal systems (chat log vs. shipping record), not something resolvable from this
  data — it needs someone to pull the actual address history on the order, not just
  reread the chat.

- **D1009 (reopened dispute).** The buyer thought the package was missing, found it,
  said so, then reopened the case saying they think they mixed it up with a *different*
  package and are still missing a needle. There's no seller reply anywhere in the
  thread. I don't have enough to tell whether this is a second, genuinely separate
  shipping problem or buyer confusion across two orders — it needs someone to check
  the buyer's other recent orders, which isn't in this dataset.

- **D1006 (condition dispute).** "Multiple scratches, feels used" vs. the seller's "one
  small scuff before I shipped it" is a pure he-said-she-said with no photo evidence in
  the data to check either account against. Guideline #3 covers confirmed *delivery*
  disputes, not disputed *condition* — there's no rule that cleanly applies here at all,
  which is itself worth flagging: this may be a gap in the guidelines rather than a gap
  in the tool.

## What the data genuinely doesn't let us resolve

- Whether an unsigned "Delivered" scan means porch theft, a misdelivery, or the buyer
  simply not looking hard enough — the data has no photo-on-delivery, no GPS pin, no
  correlating account signal.
- Item authenticity (D1014) — this needs physical inspection or proof of purchase from
  the seller, neither of which shows up in chat or tracking.
- Whether a signature actually belongs to the buyer versus a housemate/neighbor/porch
  package thief who forged a name — I use the signer's name only as a soft, informational
  cross-check against the buyer's handle, never as a gate on the verdict, since the
  guideline treats *any* valid signature as sufficient.
