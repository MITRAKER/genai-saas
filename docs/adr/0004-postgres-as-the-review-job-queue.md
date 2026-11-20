# 4. Use Postgres as the job queue for GitHub reviews

- Status: Accepted
- Date: 2026-11-19

## Context

GitHub sends a webhook when a pull request is opened or updated, and waits
at most 10 seconds for the answer. A review takes from seconds to minutes:
fetch the diff, index the code, wait for the model, post the comment. So the
webhook must only record the work, and something else must do it later.

That "something else" needs a queue that survives restarts, lets several
workers share the work without two of them taking the same job, retries
failures later, and never runs (and bills) the same review twice: GitHub can
deliver the same event more than once.

The options: Redis, which we run already, but which needs streams and
consumer groups to stop a crashed worker's job from being lost; a message
broker (RabbitMQ, SQS), one more system to run, secure and back up; or
Postgres, which we run already, with transactions.

## Decision

The queue is a table, `review_jobs`, in the Postgres we already have.

- One row per commit to review, unique on (repository, pull request, head
  commit): a repeated webhook finds the row and does nothing.
- A worker claims the oldest ready job with `SELECT ... FOR UPDATE SKIP
  LOCKED`, so workers never wait for or take each other's jobs.
- A failure that may pass (GitHub 5xx, a timeout) puts the job back with a
  growing delay, 30 seconds doubling up to 30 minutes, for at most
  `JOB_MAX_ATTEMPTS` tries. A job left `running` by a crashed worker is
  claimed again after twice the job timeout.
- The row keeps what happened (`posted`, `skipped`, `failed`, with a reason),
  which is also how the daily limit per installation is counted.

## Consequences

- No new infrastructure. Queueing a job is an ordinary insert, and the
  history of every job is one SQL query away.
- Postgres handles thousands of jobs per second this way; we need a few per
  minute. If that ever changes, a broker can replace `app/jobs.py` alone.
- Workers poll every `WORKER_POLL_SECONDS` (2 by default), so a review may
  start up to 2 seconds late. `LISTEN/NOTIFY` could remove that delay.
- Finished rows stay in the table. They are small, but a retention job will
  be needed one day.
