from __future__ import annotations

import logging
from typing import Protocol

from app.models import UsageEvent

logger = logging.getLogger(__name__)


class MetricsExporter(Protocol):
    def export(self, event: UsageEvent) -> None: ...


class NullMetricsExporter:
    def export(self, event: UsageEvent) -> None:
        return None


def build_metrics_exporter() -> MetricsExporter:
    return NullMetricsExporter()
