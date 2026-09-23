# Going live: processors and WhatsApp

Every integration is off until its keys are set. Until then the app runs in demo
mode: a browser chat thread, a simulated checkout at `/demo/checkout/…`, and
reminders instead of card re-charges.

## The recovery lifecycle

```
Paystack invoice.payment_failed ─┐                        ┌─ WhatsApp: opening template
Flutterwave charge.completed ────┼─▶ open conversation ───┤   (name, amount, checkout link)
POST /events/payment-failed ─────┘   (background turn)    └─ outcome: pending
                                              │
         customer replies on WhatsApp ───────▶ turn ─▶ reply on WhatsApp
                                              │
                    SCHEDULE_RETRY ──▶ retry queue ──(due)──▶ re-charge saved card (Paystack)
                                                              or remind with a fresh link
                                              │
Paystack charge.success / Flutterwave successful ─▶ outcome: recovered, retries cancelled,
POST /events/payment-succeeded                       thank-you message
```

All webhooks are signature-verified, size-capped, idempotent on the sender's event
id, and disabled until their secret is configured.

## 1. Paystack

1. Set `PAYSTACK_SECRET_KEY` (a test key, `sk_test_…`, works).
2. In the Paystack dashboard, set the webhook URL to `https://<host>/webhooks/paystack`.

Paystack signs webhooks with the secret key, so nothing else is needed. The
customer's email comes from the event and is required to create checkout links.
A reusable card authorization from the failure event lets scheduled retries
re-charge the card directly.

## 2. Flutterwave

1. Set `FLUTTERWAVE_SECRET_KEY`.
2. Choose a secret hash in the Flutterwave dashboard and set the same value as
   `FLUTTERWAVE_SECRET_HASH`.
3. Set the webhook URL to `https://<host>/webhooks/flutterwave`.
4. Set `PAYMENT_REDIRECT_URL` for the page customers see after checkout.

Flutterwave retries send a reminder with a fresh link (no tokenized re-charge).

## 3. WhatsApp Cloud API

1. From a Meta Business app, set `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
   `WHATSAPP_APP_SECRET`, and a `WHATSAPP_VERIFY_TOKEN` of your choice.
2. Subscribe the webhook to `https://<host>/webhooks/whatsapp` (the `messages` field).
3. Create and get approval for a message template whose body takes three parameters —
   `{{1}}` customer name, `{{2}}` amount, `{{3}}` payment link — and set its name as
   `WHATSAPP_OPENING_TEMPLATE`.

WhatsApp only lets a business open a conversation with an approved template, so the
first message uses it; replies after the customer answers are free text written by
the agent. Customers are matched to conversations by the phone number in the
processor event (`DEFAULT_COUNTRY_CODE` normalises local numbers like `0803…`).

## Processor-neutral events

Gateways without a native adapter can post to `/events/payment-failed` and
`/events/payment-succeeded`, signed with HMAC-SHA512 of the body using
`WEBHOOK_SECRET` (`x-signature` header).
