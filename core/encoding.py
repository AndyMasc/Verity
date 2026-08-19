"""JSON-safe dramatiq message payloads and encoding.

Kept separate from ``core.tasks`` because ``DRAMATIQ_ENCODER`` is
resolved by django-dramatiq during app configuration setup — before
``dramatiq.set_broker`` runs. Importing a module that registers actors
at that point would register them on the default StubBroker, which is
discarded once the real broker is configured.
"""

from dataclasses import asdict, dataclass

from dramatiq.encoder import JSONEncoder


@dataclass(frozen=True)
class EmailTaskPayload:
    """Structured payload for asynchronous email delivery."""

    subject: str
    message: str
    from_email: str
    recipient_list: list[str]
    html_message: str | None = None


class EmailPayloadEncoder(JSONEncoder):
    """Dramatiq JSON encoder that serializes "EmailTaskPayload" dataclasses.

    Actors only carry JSON-safe arguments over the broker, so the payload is
    converted with "asdict" at enqueue time while call sites keep the typed
    dataclass. Selected via "DRAMATIQ_ENCODER" in settings.
    """

    def encode(self, data):
        return super().encode(_json_safe(data))


def _json_safe(value):
    """Recursively replace "EmailTaskPayload" instances with plain dicts."""
    if isinstance(value, EmailTaskPayload):
        return asdict(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
