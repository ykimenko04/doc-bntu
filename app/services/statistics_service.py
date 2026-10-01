from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..models import Application, ApplicationFaculty, Contract, ContractFaculty, Faculty, Order, OrderItem, Organization
from .status_service import STATUS_ACTIVE, STATUS_APPLICATION, URGENCY_DUE_30, expiry_urgency


@dataclass(frozen=True)
class RegistryStatistics:
    faculty_id: int | None
    faculty_name: str | None
    total_organizations: int
    organizations_month: int
    active_contracts: int
    total_contracts: int
    organizations_with_contracts: int
    organizations_with_applications: int
    organizations_with_applications_month: int
    active_applications: int
    total_applications: int
    total_active_documents: int
    attention_contracts: int
    attention_applications: int

    @property
    def attention_total(self) -> int:
        return self.attention_contracts + self.attention_applications


def _faculty_ids(document) -> set[int]:
    order = document.current_order
    item_ids = {item.faculty_id for item in order.items if item.faculty_id is not None} if order else set()
    return item_ids or {link.faculty_id for link in document.faculty_links}


def _is_active_contract(contract: Contract) -> bool:
    """A relationship remains active while the contract or one of its d.s. is active."""
    return contract.status == STATUS_ACTIVE or contract.active_agreement is not None


def _active_contract_end_date(contract: Contract):
    # Additional agreements currently have no validity-end field. Therefore an
    # active d.s. supersedes the base contract term and cannot be classified as
    # overdue by the closed contract's old end date.
    return None if contract.active_agreement is not None else contract.end_date


def registry_statistics(
    session,
    faculty_id: int | None = None,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> RegistryStatistics:
    today = today or date.today()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    faculty = session.get(Faculty, faculty_id) if faculty_id is not None else None

    contracts = session.scalars(select(Contract).options(
        selectinload(Contract.faculty_links).selectinload(ContractFaculty.faculty),
        selectinload(Contract.orders).selectinload(Order.items).selectinload(OrderItem.faculty),
        selectinload(Contract.agreements),
    )).unique().all()
    applications = session.scalars(select(Application).options(
        selectinload(Application.faculty_links).selectinload(ApplicationFaculty.faculty),
        selectinload(Application.orders).selectinload(Order.items).selectinload(OrderItem.faculty),
    )).unique().all()
    if faculty_id is not None:
        contracts = [row for row in contracts if faculty_id in _faculty_ids(row)]
        applications = [row for row in applications if faculty_id in _faculty_ids(row)]

    contract_orgs = {row.organization_id for row in contracts}
    application_orgs = {row.organization_id for row in applications}
    scoped_org_ids = contract_orgs | application_orgs
    organizations = session.scalars(select(Organization)).all()
    if faculty_id is not None:
        organizations = [row for row in organizations if row.id in scoped_org_ids]

    recent_application_orgs = {
        row.organization_id for row in applications
        if row.created_at is not None and row.created_at >= cutoff
    }
    active_contracts = [row for row in contracts if _is_active_contract(row)]
    active_applications = [row for row in applications if row.status == STATUS_APPLICATION]
    attention_contracts = sum(
        expiry_urgency(end_date, today).bucket == URGENCY_DUE_30
        for row in active_contracts if (end_date := _active_contract_end_date(row)) is not None
    )
    attention_applications = sum(
        expiry_urgency(row.date_end, today).bucket == URGENCY_DUE_30
        for row in active_applications if row.date_end is not None
    )
    return RegistryStatistics(
        faculty_id=faculty_id,
        faculty_name=faculty.name if faculty else None,
        total_organizations=len(organizations),
        organizations_month=sum(
            row.created_at is not None and row.created_at >= cutoff for row in organizations
        ),
        active_contracts=len(active_contracts),
        total_contracts=len(contracts),
        organizations_with_contracts=len(contract_orgs),
        organizations_with_applications=len(application_orgs),
        organizations_with_applications_month=len(recent_application_orgs),
        active_applications=len(active_applications),
        total_applications=len(applications),
        total_active_documents=len(active_contracts) + len(active_applications),
        attention_contracts=attention_contracts,
        attention_applications=attention_applications,
    )
