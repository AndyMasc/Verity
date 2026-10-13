from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HistoryEntry:
    """Unified history entry that normalizes django-simple-history and MergeLog into one timeline.
    RecordHistoryView merges three data sources (Record history, DocumentData history,
    MergeLog) into a single chronological list.
    """

    source_type: str
    history_type: str
    history_date: Any
    history_user: Any = None
    merge: Any = None
    instance: Any = None
    changed_fields: dict[str, Any] = field(default_factory=dict)
