# PostHog error tracking

## What you still need to do

No additional setup is required from this run. Source-map upload was not wired because this Django/Python application produces readable Python stack traces on this platform, so there is no CI API key or source-map secret to add.

## What error tracking does now

Uncaught errors reach PostHog through the existing Python SDK configuration:

- `core/apps.py` initializes the instance-based PostHog client with `enable_exception_autocapture=True`, which installs the SDK’s exception hooks.
- `Verity/settings/base.py` includes `posthog.integrations.django.PosthogContextMiddleware` after Django’s `AuthenticationMiddleware`, so Django view exceptions captured as error responses include request context.

The PostHog SDK is already initialized in the application; no duplicate global handlers or custom route wrappers were added.

Source-map upload was not applicable here because Python stack traces are readable on this platform. The production build command was left untouched.

## How to verify

Trigger any application error, then open [PostHog Error Tracking](https://us.posthog.com/project/638558/error_tracking) and look for the captured exception. Uploaded symbol sets, if applicable to another instrumented build, appear at [Error Tracking configuration](https://us.posthog.com/project/638558/error_tracking/configuration).
