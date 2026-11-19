# On-call Policy

## Rotation

One week, handing over Wednesday at 10:00 UTC, with a primary and a secondary.
The secondary is paged only if the primary has not acknowledged within 10 minutes.

## What pages

Only automated alerts and declared Sev-1 incidents page on-call. Support tickets
never page, regardless of how a customer describes the urgency. A customer saying
"this is urgent" is a Sev-3 until the severity criteria are actually met.

## Paging budget

If a single service pages more than four times in one rotation, it goes on the
reliability backlog and feature work on it stops until the paging rate falls
below the threshold.

## Compensation

400 USD per week served, and on-call engineers carry no more than half their
normal sprint load.
