# PostHog Self-driving setup report

## Summary

PostHog Self-driving is configured for Verity. Session Replay, Error Tracking, and Support are enabled; health, error-tracking, and support signal sources are enabled; and focused scouts plus two Replay Vision monitors are active.

Findings should begin appearing in the [Self-driving inbox](https://us.posthog.com/project/626107/inbox) within about 30 minutes as new data and recordings arrive.

## AI data processing

Approved by the wizard gate before setup.

## GitHub

GitHub was already connected before this run through the PostHog GitHub App.

## Products enabled

| Product | Result | Notes |
|---|---|---|
| Session Replay | Already enabled | This is a web app. The browser initialization does not disable session recording. No recordings were found yet. |
| Error Tracking | Enabled | Browser initialization does not disable exception capture. |
| Support (Conversations) | Already enabled | An inbound email, inbox, or Slack channel is still required before tickets arrive. |

## Signal sources

| Source product | Source type | Action |
|---|---|---|
| `health_checks` | `health_issue` | Enabled (source config `01a0d30c-27f3-7f21-93d9-74ad5ca842c4`). |
| `error_tracking` | `issue_created` | Enabled (source config `01a0d30c-27fa-7757-b61e-273ba89fbeb4). |
| `error_tracking` | `issue_reopened` | Enabled (source config `01a0d30c-27f2-7638-93d5-e4807c4cb542). |
| `error_tracking` | `issue_spiking` | Enabled (source config `01a0d30c-27f9-79c8-a512-ae56e7b1ee85). |
| `conversations` | `ticket` | Enabled (source config `01a0d30c-27f2-7f77-9b03-36ad5ff5ce97). |
| `signals_scout` | `cross_source_issue` | No row created; scout findings are enabled by default. |
| `session_replay` | `session_analysis_cluster` | Deliberately skipped; Replay Vision scanners provide the Replay route. |
| `replay_vision` | — | No source row is needed; each scanner is self-authorized through `emits_signals: true`. |

## Connected tools

The connected-tools selection was dismissed, so no external-tool responders were enabled. GitHub remains connected but GitHub Issues was not selected as a Self-driving source. Sentry was detected in the repository but was not enabled as an external responder without confirmation.

## Scout troop

The verified budget is **100 runs/day**, with **0 runs used today** and 100 remaining. The project announcement says: “Scouts are in early access. Each project gets up to 100 scout runs a day. Contact team-self-driving@posthog.com if you need more.”

### Active scouts (7)

| Scout | Why it is active |
|---|---|
| General | Cross-product correlations and surfaces without a specialist. |
| Product analytics | Core product flows, retention, lifecycle, and paths. |
| Web analytics | Traffic, attribution, landing-page health, bounce, and 404 patterns. |
| Revenue analytics | Stripe and revenue-capture health. |
| Observability gaps | Important event streams without dashboards, insights, or alerts. |
| Document-to-record flow | Custom coverage for the document upload-to-record handoff. |
| Bank-link liveness | Custom coverage for Plaid bank-link completion health. |

### Disabled built-in scouts (22)

| Scout | Reason it remains disabled |
|---|---|
| AI observability | No LLM analytics evidence. |
| Anomaly detection | No established dashboard/insight usage was available to rank it above chosen specialists. |
| APM | No tracing/APM evidence. |
| Conversations | Support channel has not yet been connected. |
| CSP violations | No CSP reporting evidence. |
| Customer analytics | No account/group analytics evidence. |
| Data pipelines | No CDP, batch-export, or Hog-flow evidence. |
| Data warehouse | No warehouse source was selected. |
| Error tracking | Covered by the native Error Tracking source. |
| Experiments | No active experiment evidence. |
| Feature flags | No active feature-flag evidence. |
| Inbox validation | Fresh setup; no resolved Self-driving fixes to validate yet. |
| Insight alerts | No configured alert evidence. |
| Logs | No confirmed PostHog Logs product usage. |
| MCP tool calls | No MCP telemetry product surface. |
| PR follow-up | No existing Self-driving delivery history to follow. |
| Replay Vision | The monitor layer is configured below; aggregate scanner analysis can be enabled later after observations accumulate. |
| Session replay | Covered by Replay Vision scanners. |
| Skills store | No team skills-store usage beyond setup-created scouts. |
| Surveys | Surveys are not enabled. |
| Tasks | No PostHog Tasks usage evidence. |
| Web vitals | No Web Vitals evidence. |

## Custom scouts

| Scout | What it watches | Discriminator | Why it is additional coverage |
|---|---|---|---|
| `signals-scout-document-record-flow` | Confirmed document uploads progressing into document-created records. | A sustained fall in the upload-to-record ratio while uploads and broader product activity hold. | The product-analytics scout is broad; this targets Verity’s document ingestion and matching handoff in `documents/views/upload.py` and `records/views/create.py`. |
| `signals-scout-bank-link-liveness` | Successful Plaid bank links. | A sustained multi-day collapse in completed bank links while broader authenticated activity stays near baseline. | Web analytics covers traffic; this monitors the distinct Plaid-link and initial-sync handoff in `plaid_integration/views/link.py`. |

Considered but not added: reimbursement payment completion, because the current telemetry has creation and checkout-start events but no reliable terminal success/failure event to form a high-signal discriminator; record sharing, because it is a secondary workflow already covered by broad product analytics; and subscription billing, because Revenue analytics owns that surface.

To make either custom scout observe without posting findings, set its configuration’s `emit` value to `false` in PostHog (dry-run mode).

## Replay Vision scanners

A Replay Vision scanner is an LLM that watches individual session recordings on a schedule and pushes qualifying findings to the Self-driving inbox. These are the only configuration in this setup that spend Replay Vision quota. Each finding arrives at half weight, so independent corroboration is required before it is promoted into a report.

| Status | Scanner | Watches | Query scope | Sampling | Estimate |
|---|---|---|---|---:|---:|
| Created | Document upload and record creation breakage | Visible document-upload, processing, and record-creation breakage. | URLs containing `/documents/`, the product’s upload and confirmation flow; this is the most direct browser completion path from document upload toward a usable record. | 50% | 0 observations / 0 credits per month from the current one-day estimate. |
| Created | Financial workflow frustration | Visible user struggle across document, bank-link, record, and reimbursement actions. | Recordings containing `$rageclick` only; no URL scope was added so it remains distinct from the completion-flow monitor. | 100% | 0 observations / 0 credits per month from the current one-day estimate. |

Replay Vision has a 2,500-credit budget for the current period, with 2,495 credits remaining at setup time. No recordings were available yet, so both scanners are armed and will begin working as soon as recordings arrive.

## Follow-ups

- [ ] Connect an inbound Support channel (email, inbox, or Slack) in PostHog so the enabled Conversations ticket source can receive tickets.
- [ ] Produce a few real browser sessions. Session Replay is enabled but no recordings were present at setup time; this will activate the Replay Vision monitors’ useful work and improve their estimates.
- [ ] Review the first scanner observations and rate them with thumbs up/down in Replay Vision to receive configuration recommendations: [document-flow monitor](https://us.posthog.com/project/626107/replay-vision/01a0d312-1bef-7220-ad07-8c57ef15866d) and [frustration monitor](https://us.posthog.com/project/626107/replay-vision/01a0d312-1b3a-7456-971c-70f1d3b1449f).
- [ ] If you want Self-driving to read Sentry, GitHub Issues, Linear, Jira, Zendesk, or another external tool, enable it later from the integration setup flow; no external tool was selected in this run.

## What happens next

The scout coordinator picks up fresh configurations within about 30 minutes. Scouts draw from the daily run budget, findings cluster into reports in the Self-driving inbox, and immediately-actionable reports can start coding tasks.
