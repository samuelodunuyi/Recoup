# Recoup eval scorecard

_Run: 2026-09-23 17:52 UTC · 28 scenarios_

## Summary

| Metric | Value |
| --- | --- |
| Routing accuracy | 100% |
| Action correctness | 100% |
| Avg latency | 3690.4 ms |
| Total cost | $0.3231 |
| Avg cost / conversation | $0.0115 |
| Avg tone score | 0.876 |
| Language pass rate | 1.0 |

## Per-scenario

| Scenario | Route (exp→act) | ✓ | Action (exp→act) | ✓ | Tone | Lang |
| --- | --- | :-: | --- | :-: | :-: | :-: |
| new_insufficient_paystack_en | new_failure→new_failure | ✅ | SEND_PAYMENT_LINK→SEND_PAYMENT_LINK | ✅ | 0.97 | ✅ |
| new_card_declined_flutterwave_en | new_failure→new_failure | ✅ | SEND_PAYMENT_LINK→SEND_PAYMENT_LINK | ✅ | 1.0 | ✅ |
| new_expired_card_paystack_en | new_failure→new_failure | ✅ | SEND_PAYMENT_LINK→SEND_PAYMENT_LINK | ✅ | 1.0 | ✅ |
| new_insufficient_pidgin | new_failure→new_failure | ✅ | SEND_PAYMENT_LINK→SEND_PAYMENT_LINK | ✅ | 0.95 | ✅ |
| new_mobile_money_timeout_pidgin | new_failure→new_failure | ✅ | SEND_PAYMENT_LINK→SEND_PAYMENT_LINK | ✅ | 0.85 | ✅ |
| already_paid_en | already_paid→already_paid | ✅ | NONE→NONE | ✅ | 0.85 | ✅ |
| already_paid_pidgin | already_paid→already_paid | ✅ | NONE→NONE | ✅ | 0.75 | ✅ |
| pay_later_friday_en | pay_later→pay_later | ✅ | SCHEDULE_RETRY→SCHEDULE_RETRY | ✅ | 0.95 | ✅ |
| pay_later_payday_en | pay_later→pay_later | ✅ | SCHEDULE_RETRY→SCHEDULE_RETRY | ✅ | 0.95 | ✅ |
| pay_later_pidgin | pay_later→pay_later | ✅ | SCHEDULE_RETRY→SCHEDULE_RETRY | ✅ | 1.0 | ✅ |
| pay_now_willing_en | pay_later→pay_later | ✅ | SEND_PAYMENT_LINK→SEND_PAYMENT_LINK | ✅ | 0.95 | ✅ |
| dispute_why_charged_en | dispute→dispute | ✅ | ESCALATE_TO_HUMAN→ESCALATE_TO_HUMAN | ✅ | 0.55 | ✅ |
| dispute_unknown_charge_en | dispute→dispute | ✅ | ESCALATE_TO_HUMAN→ESCALATE_TO_HUMAN | ✅ | 0.6 | ✅ |
| needs_human_fraud_en | needs_human→needs_human | ✅ | ESCALATE_TO_HUMAN→ESCALATE_TO_HUMAN | ✅ | 0.85 | ✅ |
| needs_human_abuse_en | needs_human→needs_human | ✅ | ESCALATE_TO_HUMAN→ESCALATE_TO_HUMAN | ✅ | 0.9 | ✅ |
| do_not_honour_en | new_failure→new_failure | ✅ | SEND_PAYMENT_LINK→SEND_PAYMENT_LINK | ✅ | 0.85 | ✅ |
| transaction_limit_en | new_failure→new_failure | ✅ | SEND_PAYMENT_LINK→SEND_PAYMENT_LINK | ✅ | 0.7 | ✅ |
| pay_later_confused_then_ok_en | pay_later→pay_later | ✅ | SCHEDULE_RETRY→SCHEDULE_RETRY | ✅ | 1.0 | ✅ |
| honour_promise_followup_en | pay_later→pay_later | ✅ | SEND_PAYMENT_LINK→SEND_PAYMENT_LINK | ✅ | 0.55 | ✅ |
| already_paid_followup_en | already_paid→already_paid | ✅ | NONE→NONE | ✅ | 1.0 | ✅ |
| new_card_declined_pidgin | new_failure→new_failure | ✅ | SEND_PAYMENT_LINK→SEND_PAYMENT_LINK | ✅ | 0.9 | ✅ |
| pay_later_vague_en | pay_later→pay_later | ✅ | SCHEDULE_RETRY→SCHEDULE_RETRY | ✅ | 1.0 | ✅ |
| dispute_pidgin | dispute→dispute | ✅ | ESCALATE_TO_HUMAN→ESCALATE_TO_HUMAN | ✅ | 0.7 | ✅ |
| new_insufficient_flutterwave_en | new_failure→new_failure | ✅ | SEND_PAYMENT_LINK→SEND_PAYMENT_LINK | ✅ | 0.95 | ✅ |
| pay_now_pidgin | pay_later→pay_later | ✅ | SEND_PAYMENT_LINK→SEND_PAYMENT_LINK | ✅ | 1.0 | ✅ |
| expired_card_followup_en | pay_later→pay_later | ✅ | SEND_PAYMENT_LINK→SEND_PAYMENT_LINK | ✅ | 0.9 | ✅ |
| needs_human_legal_pidgin | needs_human→needs_human | ✅ | ESCALATE_TO_HUMAN→ESCALATE_TO_HUMAN | ✅ | 0.85 | ✅ |
| pay_later_specific_date_en | pay_later→pay_later | ✅ | SCHEDULE_RETRY→SCHEDULE_RETRY | ✅ | 1.0 | ✅ |
