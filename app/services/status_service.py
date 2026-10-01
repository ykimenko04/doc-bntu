from dataclasses import dataclass
from datetime import date


STATUS_ACTIVE = "Активен"
STATUS_APPLICATION = "Заявка"
STATUS_CLOSED = "Закрыт"

CONTRACT_STATUSES = (STATUS_ACTIVE, STATUS_CLOSED)
APPLICATION_STATUSES = (STATUS_APPLICATION, STATUS_CLOSED)

STATUS_CLASSES = {
    STATUS_ACTIVE: "status-active",
    STATUS_APPLICATION: "status-active",
    STATUS_CLOSED: "status-closed",
}

ORDER_CHANGE_CLASSES = {
    "Добавлено": "change-added",
    "Изменено": "change-updated",
    "Без изменений": "change-unchanged",
    "Исключено": "change-removed",
}

LEGACY_STATUS_LABELS = {"ACTIVE": STATUS_ACTIVE, "CLOSED": STATUS_CLOSED}

URGENCY_DUE_30 = "due_30"
URGENCY_DUE_90 = "due_90"
URGENCY_LATER = "due_over"
URGENCY_BUCKETS = (
    (URGENCY_DUE_30, "≤ 30 дней"),
    (URGENCY_DUE_90, "31–90 дней"),
    (URGENCY_LATER, "> 90 дней"),
)


@dataclass(frozen=True)
class ExpiryUrgency:
    bucket: str
    css_class: str
    days_left: int
    overdue: bool


def expiry_urgency(end_date: date | None, today: date | None = None) -> ExpiryUrgency | None:
    """Classify a contract end date for both filtering and color display."""
    if end_date is None:
        return None
    days_left = (end_date - (today or date.today())).days
    if days_left <= 30:
        bucket, css_class = URGENCY_DUE_30, "danger"
    elif days_left <= 90:
        bucket, css_class = URGENCY_DUE_90, "warning"
    else:
        bucket, css_class = URGENCY_LATER, "success"
    return ExpiryUrgency(bucket, css_class, days_left, days_left < 0)


def status_label(value: str | None) -> str:
    return LEGACY_STATUS_LABELS.get(value or "", value or "—")


def status_class(value: str | None) -> str:
    return STATUS_CLASSES.get(status_label(value), "status-closed")


def order_change_class(value: str | None) -> str:
    return ORDER_CHANGE_CLASSES.get(value or "", "change-unchanged")
