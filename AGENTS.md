# Project instructions

These instructions apply throughout this repository. Follow explicit user
instructions when they change the scope or behavior described here.

## Spec-driven workflow

The product contract is [spec/telegram-calendar-bot-spec.md](spec/telegram-calendar-bot-spec.md).
[README.md](README.md) provides user examples, setup, and operational guidance.

1. Read the relevant spec sections before changing behavior. Trace the affected
   implementation, its callers, and existing tests. Check `git status --short`
   and preserve unrelated work already in the tree.
2. Identify the requested behavior and acceptance examples: the user's input,
   expected response, state changes, and external operations. For conversation
   flows, include cancellation, expiry, and subsequent replies where relevant.
3. Update the relevant spec sections to describe the intended behavior before
   implementing a feature or behavior change. Keep proposed or incomplete work
   clearly distinguished from implemented behavior. For a bug that violates
   an existing contract, retain the contract and add a regression example or
   clarification if useful.
4. Implement the smallest change that satisfies that contract. Reuse existing
   parsers, pending-state patterns, formatting, and service clients. Avoid
   unrelated refactors, new dependencies, and speculative infrastructure.
5. Add or adapt focused regression tests for behavior changes. Test observable
   outcomes and relevant failure paths rather than mirroring implementation.
6. Reconcile the final implementation, tests, spec, and affected README examples.
   Remove contradictory statements and document material limitations. Do not
   mark live deployment or integration verification complete based on mocks.
7. Report what changed, validation results, remaining limitations, and useful
   example conversations for new user-facing flows.

The spec describes intended behavior; code is evidence of current behavior.
Do not silently rewrite the spec to justify a bug. An explicit user request
can change the contract: update it as part of that work without requesting
routine approval again. Ask only when a material ambiguity cannot reasonably
be resolved from the request, spec, and existing behavior.

Implement only the requested scope. List other issues discovered separately
without fixing them unless the user authorizes the additional work. A request
for suggestions or review alone does not authorize edits.

## Repository map

- `app/main.py`: webhook authorization, intent routing, conversation state,
  scheduling flows, and reply formatting.
- `app/parser.py`: Groq prompts and structured parsing.
- `app/models.py`: validated request and calendar data contracts.
- `app/calendar_client.py`: Google Calendar operations and event reminders.
- `app/cron_client.py`: independent reminder jobs.
- `app/telegram_client.py`: Telegram messages and callback acknowledgements.
- `app/config.py`: configuration and fixed user/calendar mappings.
- `tests/`: pytest regression coverage, primarily using mocked services.
- `spec/telegram-calendar-bot-spec.md`: architecture, contracts, flows,
  constraints, and implementation status.

## Behavioral constraints

- Preserve per-user calendar isolation. Private chats support management;
  the configured group supports authorized, read-only schedule access.
  Validate callback authorization as well as message authorization.
- Keep deterministic routing for established commands and simple forms; use
  Groq for interpretation-heavy requests and validate its structured output.
- Resolve dates in the configured home timezone. Preserve native all-day date
  boundaries, timed-event durations, and explicit date/year semantics.
- For pending conversations, document scope, expiry, cancellation, replacement,
  and restart behavior. Prevent stale choices from acting on a newer request.
  For callbacks that write data, guard against repeated submission.
- Preserve conflict checks, calendar write permissions, and confirmation
  requirements. Independent reminders must not create calendar events.
- Do not introduce persistent storage merely for short-lived conversation
  state; follow the spec's current in-memory model unless scope requires more.
- Keep replies concise and actionable. Preserve supplied details when asking
  follow-up questions. Document new controls and give realistic examples.

## Validation

Use the project's available Python environment and installed dependencies.
The standard full-suite command from the repository root is:

```powershell
python -m pytest -q
```

Run affected tests during development and the full suite for application
behavior changes before reporting completion. Run `git diff --check` and
review the final diff. Documentation-only changes need content/link review,
not an unnecessary test run.

Mock Telegram, Google Calendar, Groq, and cron-job.org in automated tests.
Use fixed dates/clocks for date-sensitive tests where practical, clean up
global pending state, and cover authorization, expiry, stale callbacks, or
write failures when the change affects those paths. State explicitly when
live integrations were not tested. If a check cannot run, report the actual
blocker rather than claiming it passed.

## Secrets and delivery

Never commit or print credentials, `.env` contents, OAuth client secrets,
refresh tokens, or bot/API tokens. Use `.env.example` for configuration names
and placeholders; keep operational credentials outside version control.

Commit and push when requested or already authorized for the current work.
Stage only the intended files, include the corresponding spec/test changes,
and preserve unrelated edits. Do not force-push or alter global Git settings.
Report the commit and push result accurately; a successful push is not proof
of a successful deployment. Do not send live Telegram messages or perform
live calendar/reminder mutations solely to test without authorization.
