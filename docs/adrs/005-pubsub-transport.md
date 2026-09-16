# Deliver Events via Pub/Sub Push, Behind a Single Ingest Seam

- Status: Accepted
- Date: 2026-09-16

Supersedes the transport decision in
`docs/bmo-jira-bidirectional-integration-plan.md` §1 ("Push over polling" via a
Jira Automation web request straight to `/jira_webhook`). Amends ADR 004, which
remains correct about *where* the reverse capability lives.

## Context and Problem Statement

Phase 1 delivered events straight over HTTP: BMO's webhook to
`/bugzilla_webhook`, and a Jira Automation rule to `/jira_webhook`. That works,
and it was the right thing to prove the pipeline with, but it left JBI owning
problems a message broker already solves:

- **Retries.** JBI has a file-based dead-letter queue and a separate retry
  runner. It is typed to Bugzilla payloads, so it cannot hold Jira events at
  all, and it assumes a single instance — both recorded as Phase 1
  limitations.
- **Buffering.** If JBI is down, a webhook delivery is simply lost; BMO and
  Jira Automation do not retry indefinitely.
- **Fan-out.** Only one consumer can receive an HTTP webhook.

Separately, each transport had wired itself into the core its own way, so
adding a fourth would have meant a fourth bespoke wiring.

## Decision

**1. One ingest seam.** Every transport calls
`jbi.ingest.ingest_event(InboundEvent) -> IngestResult`. The envelope carries
the source, the typed payload, and broker metadata (message id, delivery
attempt). The result carries an *acknowledgement decision*:
`HANDLED`, `IGNORED`, `RETRY`, `PERMANENT_FAILURE`.

**2. Pub/Sub push, not pull.** A push subscription POSTs to
`/pubsub_push`. JBI stays a stateless web service: no consumer process, no
second deployment unit, no graceful-shutdown logic, and the delivery policy
(backoff, max attempts, dead-letter topic) becomes reviewable configuration
rather than code.

**3. Both sources through one topic.** Bugzilla and Jira events share the
transport, so there is one retry story and one dead-letter topic. The direct
HTTP endpoints remain for compatibility and local testing.

**4. Duplicate suppression on the message id**, bounded and in-process.

## Consequences

**Acknowledgement is an HTTP status**, which forces three rules that are easy
to get wrong:

- An event JBI deliberately ignores must return **200**. Returning an error
  for "out of scope" or "self-authored" would have the subscription redeliver
  it until it expired — and those are the *majority* of events.
- An undecodable payload also returns 200 and is dropped with a log.
  Redelivery cannot fix bad base64 or a payload that does not match the
  schema.
- Only genuine transient failures return 5xx.

**The broker's dead-letter topic replaces the file queue** for
broker-delivered events, which resolves the two Phase 1 limitations above.
The file queue stays for the legacy webhook path.

**At-least-once delivery makes duplicates normal.** Most of the pipeline is
already idempotent — Invariant A stops duplicate issues, read-before-write
stops duplicate field writes — but a redelivered *comment* event would post
twice, and no field-level check catches that. Hence the message-id cache.

**The cache is a compromise we are naming, not hiding.** It is per-instance
and forgotten on restart, so across two instances a redelivered comment can
still double-post. A shared store (Redis) is the correct answer; building a
distributed dedupe store was out of proportion to this change, and a
half-built one would have been worse than an honest bounded cache. Tracked as
plan §13-11.

**Ordering is not guaranteed** unless the publisher sets an ordering key.
Out-of-order events weaken the conflict rule that compares against "the value
before this change". Recommended key: the bug id. Tracked as §13-12.

**Auth is a shared secret in the query string**, because a push subscription
cannot set request headers. This is weaker than the header-based auth used
elsewhere: the token appears in URLs and therefore potentially in logs.
Pub/Sub OIDC push authentication is the right production answer — the
subscription signs a token JBI verifies against Google's keys, with no shared
secret at all — and is deliberately left as follow-up rather than claimed as
done.

## Alternatives Considered

**Pull subscription.** More control over throughput, concurrency and ordering,
and no inbound HTTP at all. Rejected for now: it needs a long-running consumer
process, explicit ack/nack, graceful shutdown, and a second deployment unit to
run and monitor — a much larger change for benefits this traffic volume does
not yet need. The ingest seam means switching later touches only the transport.

**Keep direct webhooks, add retries in JBI.** Rejected: it means reimplementing
backoff, dead-lettering and buffering that the broker already provides, and it
would not fix the queue's single-instance assumption.

**A separate topic per source.** Rejected: two subscriptions, two retry
policies and two dead-letter topics to keep in step, for no gain — the seam
distinguishes sources from one message attribute.
