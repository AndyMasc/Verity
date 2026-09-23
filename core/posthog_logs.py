"""Dedicated OpenTelemetry log export for purpose-written PostHog logs."""

import atexit
import logging
from urllib.parse import urlsplit, urlunsplit

POSTHOG_LOGGER_NAME = "verity.posthog"


def configure_posthog_log_export(project_token: str, host: str) -> None:
    """Export only records emitted through the dedicated PostHog logger."""
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    parsed_host = urlsplit(host)
    endpoint = urlunsplit((parsed_host.scheme, parsed_host.netloc, "/i/v1/logs", "", ""))
    logger_provider = LoggerProvider(resource=Resource.create({"service.name": "verity"}))
    set_logger_provider(logger_provider)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(
            OTLPLogExporter(
                endpoint=endpoint,
                headers={"Authorization": f"Bearer {project_token}"},
            )
        )
    )
    atexit.register(logger_provider.shutdown)

    posthog_logger = logging.getLogger(POSTHOG_LOGGER_NAME)
    posthog_logger.setLevel(logging.INFO)
    posthog_logger.addHandler(LoggingHandler(logger_provider=logger_provider))
    posthog_logger.propagate = False
