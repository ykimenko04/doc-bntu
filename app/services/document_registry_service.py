from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from math import ceil

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from ..models import (
    AdditionalAgreement,
    Application,
    ApplicationFaculty,
    Contract,
    ContractFaculty,
    Document,
    Faculty,
    Order,
    OrderItem,
    Organization,
)
from .status_service import (
    APPLICATION_STATUSES,
    CONTRACT_STATUSES,
    URGENCY_BUCKETS,
    URGENCY_DUE_30,
    URGENCY_DUE_90,
    URGENCY_LATER,
    ExpiryUrgency,
    expiry_urgency,
)


PAGE_SIZE = 50


@dataclass(frozen=True)
class RegistryPage:
    rows: list
    total: int
    page: int
    pages: int


@dataclass(frozen=True)
class ContractRegistryRow:
    contract: Contract
    effective_status: str
    expiry: ExpiryUrgency | None
    specialty_count: int


def _page(total: int, requested: int) -> tuple[int, int, int]:
    pages = max(1, ceil(total / PAGE_SIZE))
    page = min(max(1, requested), pages)
    return page, pages, (page - 1) * PAGE_SIZE


def _faculty_names(faculty: str | list[str]) -> list[str]:
    return [faculty] if isinstance(faculty, str) and faculty else [value for value in faculty if value]


def _contract_base_conditions(query_text: str, faculty: str | list[str], status: str, end_year: str):
    conditions = []
    needle = query_text.strip()
    if needle:
        pattern = f"%{needle}%"
        conditions.append(or_(
            Organization.short_name.ilike(pattern),
            Organization.full_name.ilike(pattern),
            Contract.number.ilike(pattern),
        ))
    selected_faculties = _faculty_names(faculty)
    if selected_faculties:
        conditions.append(exists(select(ContractFaculty.contract_id).join(Faculty).where(
            ContractFaculty.contract_id == Contract.id,
            Faculty.name.in_(selected_faculties),
        )))
    active_agreement = exists(select(AdditionalAgreement.id).where(
        AdditionalAgreement.contract_id == Contract.id,
        AdditionalAgreement.status == "Активен",
    ))
    if status in CONTRACT_STATUSES:
        if status == "Активен":
            conditions.append(or_(active_agreement, and_(~active_agreement, Contract.status == status)))
        else:
            conditions.append(and_(~active_agreement, Contract.status == status))
    if end_year.isdigit():
        conditions.append(func.extract("year", Contract.end_date) == int(end_year))
    return conditions


def _urgency_condition(bucket: str, today: date):
    if bucket == URGENCY_DUE_30:
        return Contract.end_date <= today + timedelta(days=30)
    if bucket == URGENCY_DUE_90:
        return and_(Contract.end_date > today + timedelta(days=30), Contract.end_date <= today + timedelta(days=90))
    if bucket == URGENCY_LATER:
        return Contract.end_date > today + timedelta(days=90)
    return None


def contract_registry(
    session: Session,
    *,
    query_text: str = "",
    faculty: str | list[str] = "",
    status: str = "",
    end_year: str = "",
    urgency: str = "",
    page: int | None = 1,
):
    conditions = _contract_base_conditions(query_text, faculty, status, end_year)
    today = date.today()
    urgency_counts = {}
    for key, _label in URGENCY_BUCKETS:
        urgency_counts[key] = session.scalar(
            select(func.count(Contract.id)).join(Organization).where(*conditions, _urgency_condition(key, today))
        ) or 0

    valid_urgencies = {key for key, _label in URGENCY_BUCKETS}
    selected_urgency = urgency if urgency in valid_urgencies else ""
    selected_condition = _urgency_condition(selected_urgency, today)
    selected_conditions = [*conditions, selected_condition] if selected_condition is not None else conditions
    total = session.scalar(select(func.count(Contract.id)).join(Organization).where(*selected_conditions)) or 0
    current_page, pages, offset = _page(total, page or 1)

    statement = (
        select(Contract)
        .join(Organization)
        .where(*selected_conditions)
        .options(
            joinedload(Contract.organization),
            selectinload(Contract.faculty_links).selectinload(ContractFaculty.faculty),
            selectinload(Contract.orders).selectinload(Order.items).selectinload(OrderItem.specialty_ref),
            selectinload(Contract.orders).selectinload(Order.items).selectinload(OrderItem.faculty),
            selectinload(Contract.orders).selectinload(Order.items).selectinload(OrderItem.annual_demands),
            selectinload(Contract.agreements).selectinload(AdditionalAgreement.documents).selectinload(Document.attachments),
            selectinload(Contract.documents).selectinload(Document.attachments),
        )
        .order_by(Contract.end_date.asc().nulls_last(), Contract.id.desc())
    )
    if page is not None:
        statement = statement.offset(offset).limit(PAGE_SIZE)
    contracts = session.scalars(statement).unique().all()
    rows = [
        ContractRegistryRow(
            contract=contract,
            effective_status=contract.active_agreement.status if contract.active_agreement else contract.status,
            expiry=expiry_urgency(contract.end_date, today),
            specialty_count=len(set(contract.specialty_codes)),
        )
        for contract in contracts
    ]
    faculties = session.scalars(select(Faculty.name).order_by(Faculty.name)).all()
    end_years = session.scalars(
        select(func.extract("year", Contract.end_date))
        .where(Contract.end_date.is_not(None))
        .distinct()
        .order_by(func.extract("year", Contract.end_date))
    ).all()
    return RegistryPage(rows, total, current_page, pages), faculties, end_years, urgency_counts, selected_urgency


def application_registry(
    session: Session,
    *,
    query_text: str = "",
    faculty: str | list[str] = "",
    status: str = "",
    end_year: str = "",
    urgency: str = "",
    page: int | None = 1,
):
    conditions = []
    needle = query_text.strip()
    if needle:
        pattern = f"%{needle}%"
        conditions.append(or_(
            Organization.short_name.ilike(pattern),
            Organization.full_name.ilike(pattern),
            Application.number.ilike(pattern),
        ))
    selected_faculties = _faculty_names(faculty)
    if selected_faculties:
        conditions.append(exists(select(ApplicationFaculty.application_id).join(Faculty).where(
            ApplicationFaculty.application_id == Application.id,
            Faculty.name.in_(selected_faculties),
        )))
    if status in APPLICATION_STATUSES:
        conditions.append(Application.status == status)
    if end_year.isdigit():
        conditions.append(func.extract("year", Application.date_end) == int(end_year))

    today = date.today()

    def urgency_condition(bucket: str):
        if bucket == URGENCY_DUE_30:
            return Application.date_end <= today + timedelta(days=30)
        if bucket == URGENCY_DUE_90:
            return and_(Application.date_end > today + timedelta(days=30),
                        Application.date_end <= today + timedelta(days=90))
        if bucket == URGENCY_LATER:
            return Application.date_end > today + timedelta(days=90)
        return None

    urgency_counts = {
        key: session.scalar(select(func.count(Application.id)).join(Organization).where(
            *conditions, urgency_condition(key)
        )) or 0
        for key, _label in URGENCY_BUCKETS
    }
    valid_urgencies = {key for key, _label in URGENCY_BUCKETS}
    selected_urgency = urgency if urgency in valid_urgencies else ""
    selected_condition = urgency_condition(selected_urgency)
    selected_conditions = [*conditions, selected_condition] if selected_condition is not None else conditions

    total = session.scalar(select(func.count(Application.id)).join(Organization).where(*selected_conditions)) or 0
    current_page, pages, offset = _page(total, page or 1)
    statement = (
        select(Application)
        .join(Organization)
        .where(*selected_conditions)
        .options(
            joinedload(Application.organization),
            selectinload(Application.faculty_links).selectinload(ApplicationFaculty.faculty),
            selectinload(Application.documents).selectinload(Document.attachments),
            selectinload(Application.orders).selectinload(Order.items).selectinload(OrderItem.specialty_ref),
            selectinload(Application.orders).selectinload(Order.items).selectinload(OrderItem.faculty),
            selectinload(Application.orders).selectinload(Order.items).selectinload(OrderItem.annual_demands),
        )
    )
    if selected_urgency == URGENCY_DUE_30:
        statement = statement.order_by(
            case((and_(Application.status == "Заявка", Application.date_end < today), 0), else_=1),
            Application.date_end.asc(), Application.signed_date.desc().nullslast(), Application.id.desc(),
        )
    else:
        statement = statement.order_by(Application.signed_date.desc().nullslast(), Application.id.desc())
    if page is not None:
        statement = statement.offset(offset).limit(PAGE_SIZE)
    rows = session.scalars(statement).unique().all()
    faculties = session.scalars(select(Faculty.name).order_by(Faculty.name)).all()
    end_years = session.scalars(
        select(func.extract("year", Application.date_end))
        .where(Application.date_end.is_not(None)).distinct()
        .order_by(func.extract("year", Application.date_end))
    ).all()
    return RegistryPage(rows, total, current_page, pages), faculties, end_years, urgency_counts, selected_urgency
