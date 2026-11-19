# Support SLA Policy

Northwind Analytics is a fictional company; this corpus exists so the agent has
documented policy to retrieve instead of answering from pretraining.

## First-response targets

| Plan | First response | Coverage |
|---|---|---|
| Enterprise | 60 minutes | 24/7 |
| Business | 4 business hours | business hours |
| Self-serve | 1 business day | business hours |

These are **response** targets, not resolution targets, and the two are measured
separately. A ticket that gets a fast acknowledgement and then stalls counts as a
breach of the resolution target, not of the response target.

## Resolution targets

Sev-1 is 4 hours, Sev-2 is 24 hours, Sev-3 is 5 business days. The clock pauses
while a ticket is in `waiting_on_customer`.

## Breach handling

A breached Enterprise first-response target triggers an automatic credit of 5% of
the monthly fee. Breaches are reviewed weekly; three breaches for one tenant in a
quarter escalates to the account's executive sponsor.

## Escalation to engineering

Only Sev-1 and Sev-2 tickets may be escalated to engineering, and only with a
reproduction, the tenant ID and the request IDs attached. Escalation volume above
15% of closed tickets in a month is treated as a product-quality signal, not a
support-staffing signal.
