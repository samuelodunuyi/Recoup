# Decline-code recovery knowledge base

Each section is one decline code. The heading format is
`## <decline_code> — <Processor>, <Processor>` so ingestion can tag chunks with
the code and processors for retrieval filtering.

## insufficient_funds — Paystack, Flutterwave, Mobile Money
The customer's account or card had insufficient funds at charge time. This is the
most common and least sensitive failure. Do not imply the customer is broke.
Strategy: acknowledge lightly ("looks like the payment didn't go through"), and
propose retrying around payday. Ask when their next salary lands and offer to
schedule the retry for then, or send a fresh link if they can top up now. In
Nigeria many salaries land on the 25th–28th; offering a payday-timed retry
materially lifts recovery.

## card_declined — Paystack, Flutterwave
The issuing bank declined the card for an unspecified reason (risk rules, channel
limits, international block). Reassure the customer this is common and rarely about
them. Strategy: suggest trying a different card or bank, or enabling online/
international transactions in their banking app. Send a fresh payment link to retry
immediately, and offer an alternative processor if the same bank keeps failing.

## expired_card — Paystack, Flutterwave
The card on file has expired. Strategy: ask the customer to pay with a current
card. Send a link to update their card details, keep it light and quick, and
confirm the subscription resumes as soon as the new card is charged.

## do_not_honour — Paystack
The bank returned "do not honour", a generic refusal that usually means a temporary
bank-side block or risk flag. Strategy: advise the customer to call their bank or
try again in a few hours, or use a different card. Offer to schedule a retry for
later today and send a link for an immediate attempt with another card.

## transaction_limit — Flutterwave, Mobile Money
The charge exceeded a per-transaction or daily limit (common on mobile money
wallets). Strategy: explain the limit, suggest raising it in-app or paying with a
card instead, and offer to split or reschedule. Send a card link as the fast path.

## mobile_money_timeout — Mobile Money
The mobile-money prompt was not approved in time (USSD/app timeout). Strategy:
reassure, explain the prompt expired, and offer to resend it now. Confirm the phone
number and that they should approve the prompt when it arrives.
