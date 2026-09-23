# PostHog Self-driving setup report

## Summary

PostHog Self-driving is configured with native health, error-tracking, and support signal sources, a focused six-scout troop, and two Replay Vision monitors that can send corroborated findings to the inbox. Findings should begin appearing in the [Self-driving inbox](https://us.posthog.com/project/531322/inbox) within about 30 minutes.

## AI data processing

Approved. The organization-level approval gate was satisfied before this setup ran.

## GitHub

GitHub was already connected before this setup. GitHub Issues was not selected as a connected-tool source.

## Products enabled

| Product | Status | Notes |
|---|---|---|
| Session Replay | Already enabled | Recent web recordings exist. No `posthog-js` initialization was found in this Django repository, so no client-side override could be checked here. |
| Error Tracking | Enabled | Exception autocapture was enabled server-side. Existing Python exception telemetry is present. |
| Support / Conversations | Already enabled | The signal source is enabled; connect an inbound email, inbox, or Slack channel before support tickets can arrive. |

## Signal sources

| Source product | Source type | Action |
|---|---|---|
| `health_checks` | `health_issue` | Enabled — setup and instrumentation health findings. |
| `error_tracking` | `issue_created` | Enabled. |
| `error_tracking` | `issue_reopened` | Enabled. |
| `error_tracking` | `issue_spiking` | Enabled. |
| `conversations` | `ticket` | Enabled — remains idle until an inbound support channel is connected. |
| `signals_scout` | `cross_source_issue` | Deliberately skipped — enabled by default without a configuration row. |
| `session_replay` | `session_analysis_cluster` | Deliberately skipped — retired; replay coverage is provided by Replay Vision scanners. |
| `replay_vision` | scanner configuration | Deliberately skipped — each scanner self-authorizes with `emits_signals: true`. |

## Connected tools

No connected tools were selected in the integration prompt. Sentry was detected in the repository but was not enabled as a source.

## Scout troop

The verified project budget is **100 runs/day** with **0 used today** and **100 remaining**. Banner: “Scouts are in early access. Each project gets up to 100 scout runs a day. Contact team-self-driving@posthog.com if you need more.”

### Enabled scouts (6)

| Scout | Coverage |
|---|---|
| `signals-scout-general` | Cross-product patterns and surfaces without a focused specialist. |
| `signals-scout-product-analytics` | Product-flow conversion, retention, lifecycle, and path regressions. |
| `signals-scout-revenue-analytics` | Revenue data health, payment-data regressions, and configuration drift. |
| `signals-scout-web-analytics` | Traffic, attribution, landing-page health, and web journey changes. |
| `signals-scout-document-intake-completion` | Custom check for confirmed document uploads that fail to become records after the normal OCR and confirmation delay. |
| `signals-scout-subscription-checkout-completion` | Custom check for a material decline from subscription checkout start to successful activation. |

### Disabled built-in scouts (23)

| Scout | Reason left disabled |
|---|---|
| AI observability | No PostHog LLM telemetry evidence. |
| Anomaly detection | No established saved-insight watchlist was available; the focused scouts cover the known priority surfaces. |
| APM | No tracing / APM evidence. |
| Conversations | No inbound support channel is configured. |
| CSP violations | No CSP-reporting evidence. |
| Customer analytics | No account/group analytics evidence. |
| Data pipelines | No CDP or export pipeline evidence. |
| Data warehouse | No warehouse source is connected. |
| Error tracking | Covered by the native error-tracking source. |
| Experiments | No active experiment evidence. |
| Feature flags | No active flag evidence. |
| Inbox validation | Fresh setup with no resolved Self-driving fixes to validate. |
| Insight alerts | No configured alert surface was identified. |
| Logs | No PostHog logs evidence. |
| MCP tool calls | Not a product surface for this application. |
| Observability gaps | Not selected over the higher-confidence product, revenue, and web coverage. |
| PR follow-up | No active Self-driving PR workflow to follow. |
| Replay Vision | Replay monitors are newly created; there are not yet accumulated observations for a trend scout. |
| Session replay | Covered by the Replay Vision monitors below. |
| Skills store | No team skills-store stewardship need identified. |
| Surveys | No surveys are configured. |
| Tasks | No PostHog Tasks surface identified. |
| Web vitals | Web analytics was prioritized; enable later if Core Web Vitals becomes a monitoring priority. |

## Custom scouts

| Scout | What it watches | Discriminator | Why it is distinct |
|---|---|---|---|
| `signals-scout-document-intake-completion` | Document upload through OCR-backed record creation. | The closed-window completion rate from confirmed upload to record creation against its own baseline, with enough volume. | The product analytics scout partly covers flow health, but does not focus on the document-specific upload/OCR/record handoff. |
| `signals-scout-subscription-checkout-completion` | Subscription checkout through confirmed activation. | Closed-cohort checkout-to-activation conversion against its own baseline. | Revenue and product scouts partly overlap, but neither is scoped to this exact checkout handoff. |

The bank-linking flow and reimbursement payment path were considered but not given dedicated scouts because the repository currently exposes only success-side analytics events, not a reliable start-to-completion pair. If a custom scout becomes noisy, set its configuration `emit` field to `false` in PostHog to switch it to dry-run.

## Replay Vision scanners

A scanner is an LLM that watches individual session recordings on a schedule and pushes eligible visible defects to the inbox. These are the only items in this setup that consume Replay Vision quota. Findings arrive at half weight and require corroboration before becoming an inbox report.

| Scanner | Status | What it watches | Query scope | Sampling | Estimate |
|---|---|---|---|---:|---:|
| Document intake breakage | Created | Visible failures during document upload, confirmation, OCR waiting, and record creation. | Recordings whose URL contains `/documents/`, the document-upload completion flow defined in `documents/urls.py`. | 50% | 0 observations / 0 credits per month from the current 7-day estimate. |
| Financial record frustration | Created | Visible user struggle with document upload, extraction, records, or bank connection. | Recordings with `$rageclick` only; no URL filter, preserving a distinct frustration monitor. | 100% | 0 observations / 0 credits per month from the current 7-day estimate. |

Both scanners are enabled and use `emits_signals: true`. Replay recordings already exist, but neither proposed query matched a recent recording in the estimate window. Rate the first resulting observations in Replay Vision to receive configuration recommendations.

## Follow-ups

- [ ] Connect an inbound Support / Conversations channel (email, inbox, or Slack) in PostHog so the enabled support responder can process tickets.
- [ ] Grant the MCP connection `property_definition:read` if you want future setup runs to validate analytics event schemas directly from PostHog; this run verified the custom-scout event pairs from repository instrumentation instead.
- [ ] Consider enabling the Sentry connected-tool source later if Sentry issues should be read by Self-driving; it was detected but not selected.
- [ ] Review the first Replay Vision observations and rate them to tune the scanner prompts if necessary.

## What happens next

The scout coordinator picks up fresh configurations within about 30 minutes. Runs draw from the verified 100/day project budget, and matching findings are clustered into reports in the [Self-driving inbox](https://us.posthog.com/project/531322/inbox). Immediately actionable reports can then start coding tasks.

## Files modified or created

- Created `posthog-self-driving-report.md`.
- No application source files were modified.
