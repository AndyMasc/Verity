"""Dedicated OpenTelemetry log export for purpose-written PostHog logs only.

This is not a second copy of core.error_tracking.LogCapture, it covers a
different level of logging. That handler takes every WARNING and above from the
app and reports them to error tracking, where they keep their traceback. This
one exports the INFO-level structured logs written deliberately to the
"posthog.export" logger, as queryable records in PostHog's logs product.

The two cannot double-report: this logger sets propagate = False below, so
records sent here never reach root and LogCapture never sees them. LogCapture
in turn skips the "posthog" namespace for the same reason.
"""

import atexit
import logging

from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

POSTHOG_LOGGER_NAME = "posthog.export"
posthog_logger = logging.getLogger(POSTHOG_LOGGER_NAME)


def configure_posthog_log_export(project_token: str, host: str) -> None:
    """Export only records written to the dedicated PostHog logger."""
    if any(
        getattr(handler, "_posthog_log_export", False)
        for handler in posthog_logger.handlers
    ):
        return

    logger_provider = LoggerProvider()
    set_logger_provider(logger_provider)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(
                endpoint=f"{host.rstrip('/')}/i/v1/logs",
                headers={"Authorization": f"Bearer {project_token}"},
            )
        )
    )
    handler = LoggingHandler(logger_provider=logger_provider)
    handler._posthog_log_export = True
    posthog_logger.addHandler(handler)
    posthog_logger.setLevel(logging.INFO)
    posthog_logger.propagate = False
    atexit.register(logger_provider.shutdown)

    posthog_logger.info(
        "PostHog log export configured",
        extra={"event": "posthog_log_export_configured"},
    )
