# Escalation Runbook

## Before escalating

Attach the reproduction, the tenant ID, the affected time window and at least one
request ID. An escalation without request IDs is returned to Tier 2 — not as
process theatre, but because the trace is what makes the first 20 minutes of
engineering time productive rather than speculative.

## Routing

Escalations land in the owning team's queue, never with on-call. On-call is for
pages, not tickets. The single exception is a suspected Sev-1, which is declared
as an incident rather than escalated as a ticket.

## Ingestion escalations specifically

Ingestion is the highest-volume escalation category and has its own pre-check:
confirm the schema registry accepted the event, confirm the Kafka topic is not
lagging, and confirm the tenant is not over its rate limit. Roughly half of
ingestion escalations historically resolved at one of those three checks without
engineering involvement.

## After resolution

The escalating agent writes a two-line summary back into the ticket, in plain
language, before closing. The summary is what the next agent reads when the same
tenant files the same ticket.
