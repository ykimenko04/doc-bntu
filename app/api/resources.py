"""JSON API used by the React client.

Handlers in this module are deliberately thin adapters around the services
used by the Jinja UI.  The OpenAPI document therefore describes executable
behaviour rather than future or mock contracts.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Security, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    AdditionalAgreement,
    AppUser,
    Application,
    AuditLog,
    Contract,
    Faculty,
    Order,
    OrderItem,
    Organization,
    SessionLocal,
    Specialty,
)
from ..services.application_service import create_application, save_application_item, update_application
from ..services.audit_metadata import ACTION_LABELS, ENTITY_FILTERS
from ..services.audit_registry_service import get_audit_registry
from ..services.audit_service import AuditActor
from ..services.auth_service import create_user, reset_password, set_user_active, update_user
from ..services.document_registry_service import application_registry, contract_registry
from ..services.document_status_service import (
    StatusTransitionError,
    change_agreement_status,
    change_application_status,
    change_contract_status,
)
from ..services.import_service import import_xlsx
from ..services.order_history_service import compare_revisions, get_order_history
from ..services.organization_service import (
    InactiveOrderRevisionError,
    create_contract,
    delete_item,
    register_additional_agreement,
    registry as organization_registry,
    save_item,
    update_contract,
)
from ..services.reconciliation_service import load_report, reconcile_xlsx, report_xlsx
from ..services.registry_export_service import applications_xlsx, contracts_xlsx
from ..services.specialty_service import specialty_registry, update_specialty
from ..services.statistics_service import registry_statistics
from .auth import require_api_user
from .schemas import AuthenticatedUserResponse, ErrorResponse


router = APIRouter()

ContractStatusFilter = Literal["Активен", "Закрыт"]
ApplicationStatusFilter = Literal["Заявка", "Закрыт"]
ContractUrgencyFilter = Literal["due_30", "due_90", "due_over"]
ApplicationUrgencyFilter = Literal["due_30", "due_90", "due_over"]


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def actor(request: Request, user: AppUser) -> AuditActor:
    return AuditActor(user.id, request.client.host if request.client else None)


def require_admin_user(user: AppUser = Security(require_api_user)) -> AppUser:
    if user.role != "ADMIN":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Раздел доступен только администратору.")
    return user


ERRORS = {
    400: {"model": ErrorResponse, "description": "Нарушено бизнес-правило"},
    401: {"model": ErrorResponse, "description": "Требуется авторизация"},
    403: {"model": ErrorResponse, "description": "Недостаточно прав"},
    404: {"model": ErrorResponse, "description": "Объект не найден"},
    409: {"model": ErrorResponse, "description": "Конфликт состояния или уникальности"},
    422: {"model": ErrorResponse, "description": "Ошибка проверки входных данных"},
}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
BINARY_RESPONSE = {
    200: {"description": "Файл XLSX", "content": {XLSX: {"schema": {"type": "string", "format": "binary"}}}},
    **ERRORS,
}


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class FacultyResponse(ApiModel):
    id: int = Field(examples=[1])
    name: str = Field(examples=["Автотракторный"])
    code: str | None = Field(default=None, examples=["АТФ"])


class SpecialtyResponse(ApiModel):
    id: int = Field(examples=[1])
    code: str = Field(examples=["1-37 01 03"])
    name: str = Field(
        examples=["1-37 01 03"],
        description="Название; если выгрузка не содержит названия, хранится и возвращается код специальности.",
    )
    profile: str | None = Field(default=None, examples=["Проектирование автомобилей"])
    qualification: str | None = Field(default=None, examples=["Инженер-механик"])
    faculty: str | None = Field(default=None, examples=["Автотракторный"])


class SpecialtyUpdate(BaseModel):
    name: str = Field(default="", examples=["Автомобилестроение"])
    profile: str = Field(default="", examples=["Проектирование автомобилей"])
    qualification: str = Field(default="", examples=["Инженер-механик"])


class OrderItemResponse(ApiModel):
    id: int
    faculty_id: int | None
    faculty: str | None
    specialty_id: int
    specialty_code: str
    specialty_name: str
    profile: str | None
    qualification: str | None
    years: dict[str, int] = Field(examples=[{"2026": 15, "2027": 16}])


class OrderResponse(ApiModel):
    id: int
    revision: int
    is_current: bool
    status: str
    created_at: datetime
    created_by: int | None
    contract_id: int | None
    additional_agreement_id: int | None
    application_id: int | None
    items: list[OrderItemResponse]

    model_config = ConfigDict(from_attributes=True, json_schema_extra={"example": {
        "id": 1, "revision": 1, "is_current": True, "status": "CURRENT",
        "created_at": "2026-09-30T10:00:00+03:00", "created_by": 1,
        "contract_id": 1, "additional_agreement_id": None, "application_id": None,
        "items": [{"id": 1, "faculty_id": 1, "faculty": "Автотракторный", "specialty_id": 37,
                   "specialty_code": "1-37 01 03", "specialty_name": "Тракторостроение",
                   "profile": "Проектирование тракторов", "qualification": "Инженер-механик",
                   "years": {"2026": 15, "2027": 16}}],
    }})


class OrderItemWrite(BaseModel):
    faculty_id: int = Field(examples=[1])
    specialty_id: int = Field(examples=[37])
    profile: str | None = Field(default=None, examples=["Проектирование тракторов"])
    qualification: str | None = Field(default=None, examples=["Инженер-механик"])
    years: dict[str, int] = Field(default_factory=dict, examples=[{"2026": 15, "2027": 16}])

    @field_validator("years")
    @classmethod
    def validate_years(cls, values: dict[str, int]):
        for year, quantity in values.items():
            if not year.isdigit() or not 2026 <= int(year) <= 2036:
                raise ValueError("Допустимые годы потребности: 2026–2036.")
            if quantity < 0:
                raise ValueError("Потребность не может быть отрицательной.")
        return values


class AdditionalAgreementResponse(ApiModel):
    id: int
    number: str = Field(examples=["1"])
    date: dt.date = Field(examples=["2025-05-06"])
    status: str = Field(examples=["Активен"])
    activated_at: datetime | None


class ContractListItem(ApiModel):
    id: int
    organization_id: int
    organization: str = Field(examples=['ОАО "МТЗ"'])
    number: str = Field(examples=["221-АТФ/280"])
    date_start: date = Field(examples=["2020-10-01"])
    date_end: date | None = Field(examples=["2030-12-31"])
    status: Literal["Активен", "Закрыт"] = Field(examples=["Активен"])
    faculties: list[FacultyResponse]
    specialty_count: int = Field(examples=[60])
    has_signed_scan: bool


class ContractResponse(ContractListItem):
    current_order: OrderResponse | None
    additional_agreements: list[AdditionalAgreementResponse]

    model_config = ConfigDict(from_attributes=True, json_schema_extra={"example": {
        "id": 1, "organization_id": 1, "organization": 'ОАО "МТЗ"', "number": "221-АТФ/280",
        "date_start": "2020-10-01", "date_end": "2030-12-31", "status": "Активен",
        "faculties": [{"id": 1, "name": "Автотракторный", "code": "АТФ"}],
        "specialty_count": 60, "has_signed_scan": False,
        "current_order": {"id": 1, "revision": 1, "is_current": True, "status": "CURRENT",
                          "created_at": "2026-09-30T10:00:00+03:00", "created_by": 1,
                          "contract_id": 1, "additional_agreement_id": None, "application_id": None,
                          "items": []},
        "additional_agreements": [{"id": 1, "number": "1", "date": "2025-05-06",
                                     "status": "Активен", "activated_at": "2026-09-30T10:00:00+03:00"}],
    }})


class ContractWrite(BaseModel):
    organization_id: int = Field(examples=[1])
    number: str = Field(min_length=1, examples=["221-АТФ/280"])
    date_start: date = Field(examples=["2020-10-01"])
    date_end: date | None = Field(default=None, examples=["2030-12-31"])
    faculty_ids: list[int] = Field(min_length=1, examples=[[1, 3, 6]])

    @model_validator(mode="after")
    def dates_are_ordered(self):
        if self.date_end and self.date_end < self.date_start:
            raise ValueError("Дата окончания не может быть раньше даты начала.")
        return self


class ContractUpdate(ContractWrite):
    organization_id: int | None = Field(default=None, exclude=True)


class ApplicationListItem(ApiModel):
    id: int
    organization_id: int
    organization: str = Field(examples=['ОАО "МТЗ"'])
    number: str | None = Field(examples=["З-2026/15"])
    signed_date: date | None = Field(examples=["2026-09-25"])
    date_end: date | None = Field(examples=["2027-09-25"])
    status: Literal["Заявка", "Закрыт"] = Field(examples=["Заявка"])
    faculties: list[FacultyResponse]
    specialty_count: int
    has_signed_scan: bool


class ApplicationResponse(ApplicationListItem):
    current_order: OrderResponse | None

    model_config = ConfigDict(from_attributes=True, json_schema_extra={"example": {
        "id": 1, "organization_id": 1, "organization": 'ОАО "МТЗ"', "number": "З-2026/15",
        "signed_date": "2026-09-25", "date_end": "2027-09-25", "status": "Заявка",
        "faculties": [{"id": 1, "name": "Автотракторный", "code": "АТФ"}],
        "specialty_count": 2, "has_signed_scan": False, "current_order": None,
    }})


class ApplicationWrite(BaseModel):
    organization_id: int = Field(examples=[1])
    number: str = Field(min_length=1, examples=["З-2026/15"])
    signed_date: date | None = Field(default=None, examples=["2026-09-25"])
    date_end: date | None = Field(default=None, examples=["2027-09-25"])
    faculty_ids: list[int] = Field(default_factory=list, examples=[[1, 3]])

    @model_validator(mode="after")
    def dates_are_ordered(self):
        if self.signed_date and self.date_end and self.date_end < self.signed_date:
            raise ValueError("Дата окончания не может быть раньше даты подписания.")
        return self


class ApplicationUpdate(BaseModel):
    number: str = Field(min_length=1)
    signed_date: date | None = None
    date_end: date | None = None

    @model_validator(mode="after")
    def dates_are_ordered(self):
        if self.signed_date and self.date_end and self.date_end < self.signed_date:
            raise ValueError("Дата окончания не может быть раньше даты подписания.")
        return self


class OrganizationRegistryItem(ApiModel):
    organization_id: int
    organization: str = Field(examples=['ОАО "МТЗ"'])
    contract_id: int
    contract_number: str = Field(examples=["221-АТФ/280"])
    faculty: FacultyResponse
    specialties: list[str] = Field(examples=[["1-37 01 03", "1-36 01 01"]])
    status: str
    date_end: date | None


class OrganizationResponse(ApiModel):
    id: int
    unp: str = Field(examples=["100354447"])
    short_name: str = Field(examples=['ОАО "МТЗ"'])
    full_name: str = Field(examples=["Открытое акционерное общество «Минский тракторный завод»"])
    legal_address: str | None
    authority: str | None
    phone: str | None
    contracts: list[ContractListItem]
    applications: list[ApplicationListItem]

    model_config = ConfigDict(from_attributes=True, json_schema_extra={"example": {
        "id": 1, "unp": "100316761", "short_name": 'ОАО "МТЗ"',
        "full_name": "Открытое акционерное общество «Минский тракторный завод»",
        "legal_address": "г. Минск", "authority": None, "phone": "+375 17 000-00-00",
        "contracts": [], "applications": [],
    }})


class PageMeta(ApiModel):
    total: int
    page: int
    per_page: int = 50


class OrganizationPage(PageMeta):
    items: list[OrganizationRegistryItem]


class ContractPage(PageMeta):
    items: list[ContractListItem]

    model_config = ConfigDict(from_attributes=True, json_schema_extra={"example": {
        "items": [{"id": 1, "organization_id": 1, "organization": 'ОАО "МТЗ"',
                   "number": "221-АТФ/280", "date_start": "2020-10-01", "date_end": "2030-12-31",
                   "status": "Активен", "faculties": [{"id": 1, "name": "Автотракторный", "code": "АТФ"}],
                   "specialty_count": 60, "has_signed_scan": False}],
        "total": 1, "page": 1, "per_page": 50,
    }})


class ApplicationPage(PageMeta):
    items: list[ApplicationListItem]

    model_config = ConfigDict(from_attributes=True, json_schema_extra={"example": {
        "items": [{"id": 1, "organization_id": 1, "organization": 'ОАО "МТЗ"', "number": "З-2026/15",
                   "signed_date": "2026-09-25", "date_end": "2027-09-25", "status": "Заявка",
                   "faculties": [{"id": 1, "name": "Автотракторный", "code": "АТФ"}],
                   "specialty_count": 2, "has_signed_scan": False}],
        "total": 1, "page": 1, "per_page": 50,
    }})


class SpecialtyPage(PageMeta):
    items: list[SpecialtyResponse]


class StatusChangeRequest(BaseModel):
    status: Literal["Активен", "Заявка", "Закрыт"] = Field(examples=["Закрыт"])
    comment: str | None = Field(default=None, examples=["Срок действия завершён"])


class StatusResponse(BaseModel):
    status: str


class AgreementCreate(BaseModel):
    number: str = Field(min_length=1, examples=["1"])
    date: dt.date = Field(examples=["2025-05-06"])


class RevisionResponse(BaseModel):
    order_id: int
    revision: int
    created_at: datetime
    origin: str
    origin_kind: str
    is_current: bool
    created_by: str
    item_count: int


class RevisionHistoryResponse(BaseModel):
    document_type: str
    document_id: int
    title: str
    revisions: list[RevisionResponse]


class RevisionDiffRow(BaseModel):
    faculty: str
    specialty: str
    change: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None


class RevisionComparisonResponse(BaseModel):
    before_order_id: int
    after_order_id: int
    years: list[int]
    rows: list[RevisionDiffRow]


class AuditUserResponse(BaseModel):
    id: int | None
    full_name: str


class AuditResponse(BaseModel):
    id: int
    created_at: datetime
    user: AuditUserResponse
    action: str
    entity_type: str
    entity_id: int | None
    entity_label: str
    diff: dict[str, Any]
    comment: str | None


class AuditPage(PageMeta):
    items: list[AuditResponse]


class AuditMetadataResponse(BaseModel):
    actions: dict[str, str]
    entity_types: dict[str, str]


class UserResponse(ApiModel):
    id: int
    username: str
    email: str | None
    full_name: str | None
    role: Literal["ADMIN", "HEAD"]
    is_active: bool
    need_password_change: bool
    need_email: bool
    created_at: datetime
    last_login_at: datetime | None


class UserPage(PageMeta):
    items: list[UserResponse]


class UserCreate(BaseModel):
    full_name: str = Field(min_length=1, examples=["Петров Пётр Петрович"])
    username: str = Field(min_length=3, examples=["petrov"])
    email: EmailStr = Field(examples=["petrov@bntu.by"])
    role: Literal["ADMIN", "HEAD"] = "HEAD"
    initial_password: str = Field(min_length=8, examples=["temporary-2026"])


class UserUpdate(BaseModel):
    full_name: str = Field(min_length=1)
    role: Literal["ADMIN", "HEAD"]


class UserStatusUpdate(BaseModel):
    is_active: bool = Field(description="true — активировать, false — деактивировать")


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=8, examples=["temporary-2026"])


class ImportResponse(BaseModel):
    rows: int
    organizations: int
    organizations_created: int
    organizations_updated: int
    contracts: int
    contracts_created: int
    contracts_updated: int
    applications: int
    applications_created: int
    applications_updated: int
    faculties: int
    specialties: int
    order_items_created: int
    order_items_updated: int
    skipped_rows: int
    skipped_organizations: int
    skipped_names: list[str]


class ReconciliationDifference(BaseModel):
    kind: str
    organization: str
    faculty: str
    specialty: str
    year: int
    ours: int
    ais: int
    note: str


class ReconciliationResponse(BaseModel):
    token: str
    filename: str
    organizations_checked: int
    organizations_skipped: int
    skipped_names: list[str]
    differences: list[ReconciliationDifference]


class ImportHistoryItem(BaseModel):
    filename: str
    created_at: datetime
    user: str
    counters: dict[str, Any]
    no_changes: bool


class ImportHistoryResponse(BaseModel):
    items: list[ImportHistoryItem]


class StatisticsResponse(ApiModel):
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
    attention_total: int


def faculty_data(value: Faculty) -> dict:
    return {"id": value.id, "name": value.name, "code": value.code}


def specialty_data(value: Specialty) -> dict:
    return {
        "id": value.id, "code": value.code, "name": value.name, "profile": value.profile,
        "qualification": value.qualification, "faculty": value.faculty,
    }


def item_data(value: OrderItem) -> dict:
    return {
        "id": value.id,
        "faculty_id": value.faculty_id,
        "faculty": value.faculty.name if value.faculty else None,
        "specialty_id": value.specialty_id,
        "specialty_code": value.specialty_ref.code,
        "specialty_name": value.specialty_ref.name,
        "profile": value.profile,
        "qualification": value.qualification_value or value.specialty_ref.qualification,
        "years": {str(row.year): row.quantity for row in value.annual_demands},
    }


def order_data(value: Order | None) -> dict | None:
    if value is None:
        return None
    return {
        "id": value.id,
        "revision": value.revision,
        "is_current": value.is_current,
        "status": value.status,
        "created_at": value.created_at,
        "created_by": value.created_by,
        "contract_id": value.contract_id,
        "additional_agreement_id": value.additional_agreement_id,
        "application_id": value.application_id,
        "items": [item_data(row) for row in value.items],
    }


def agreement_data(value: AdditionalAgreement) -> dict:
    return {
        "id": value.id, "number": value.number, "date": value.date,
        "status": value.status, "activated_at": value.activated_at,
    }


def contract_data(value: Contract, *, detail: bool = False, effective_status: str | None = None, specialty_count: int | None = None) -> dict:
    result = {
        "id": value.id,
        "organization_id": value.organization_id,
        "organization": value.organization.short_name,
        "number": value.number,
        "date_start": value.start_date,
        "date_end": value.end_date,
        "status": effective_status or value.status,
        "faculties": [faculty_data(link.faculty) for link in value.faculty_links],
        "specialty_count": specialty_count if specialty_count is not None else len(set(value.specialty_codes)),
        "has_signed_scan": value.has_signed_scan,
    }
    if detail:
        result.update({
            "current_order": order_data(value.current_order),
            "additional_agreements": [agreement_data(row) for row in value.agreements],
        })
    return result


def application_data(value: Application, *, detail: bool = False) -> dict:
    result = {
        "id": value.id,
        "organization_id": value.organization_id,
        "organization": value.organization.short_name,
        "number": value.number,
        "signed_date": value.signed_date,
        "date_end": value.date_end,
        "status": value.status,
        "faculties": [faculty_data(link.faculty) for link in value.faculty_links],
        "specialty_count": len({row.specialty_id for row in value.items}),
        "has_signed_scan": value.has_signed_scan,
    }
    if detail:
        result["current_order"] = order_data(value.current_order)
    return result


def user_data(value: AppUser) -> dict:
    return {
        "id": value.id,
        "username": value.username,
        "email": value.email,
        "full_name": value.full_name,
        "role": value.role,
        "is_active": value.is_active,
        "need_password_change": value.must_change_password,
        "need_email": not bool(value.email),
        "created_at": value.created_at,
        "last_login_at": value.last_login_at,
    }


def faculty_names(session: Session, ids: list[int]) -> list[str]:
    rows = session.scalars(select(Faculty).where(Faculty.id.in_(set(ids))).order_by(Faculty.name)).all() if ids else []
    if len(rows) != len(set(ids)):
        raise HTTPException(422, "Выбран неизвестный факультет.")
    return [row.name for row in rows]


def mutation_error(error: Exception):
    message = str(error)
    code = 409 if "существует" in message or isinstance(error, InactiveOrderRevisionError) else 400
    raise HTTPException(code, message) from error


@router.get("/faculties", tags=["specialties"], response_model=list[FacultyResponse], responses=ERRORS,
            summary="Получить справочник факультетов")
def faculties(_user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    return [faculty_data(row) for row in session.scalars(select(Faculty).order_by(Faculty.name)).all()]


@router.get("/specialties", tags=["specialties"], response_model=SpecialtyPage, responses=ERRORS,
            summary="Получить справочник специальностей")
def specialties(q: str = Query("", examples=["1-37"]), page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
                _user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    result = specialty_registry(session, q, page, per_page)
    return {"items": [specialty_data(row) for row in result.items], "total": result.total,
            "page": result.page, "per_page": result.per_page}


@router.put("/specialties/{specialty_id}", tags=["specialties"], response_model=SpecialtyResponse, responses=ERRORS,
            summary="Изменить специальность",
            description="Только ADMIN. Код остаётся неизменным; пустые name/profile/qualification допустимы.")
def save_specialty_api(specialty_id: int, payload: SpecialtyUpdate, request: Request,
                       user: AppUser = Security(require_admin_user), session: Session = Depends(get_session)):
    specialty = session.get(Specialty, specialty_id)
    if not specialty:
        raise HTTPException(404, "Специальность не найдена.")
    update_specialty(session, specialty, payload.name, payload.profile, payload.qualification, audit_actor=actor(request, user))
    return specialty_data(specialty)


@router.get("/organizations", tags=["organizations"], response_model=OrganizationPage, responses=ERRORS,
            summary="Получить факультетские строки реестра организаций")
def organizations(q: str = Query("", examples=["МТЗ"]), faculty_id: int | None = None,
                  status_value: ContractStatusFilter | None = Query(None, alias="status"),
                  end_year: int | None = None, urgency: ContractUrgencyFilter | None = Query(None),
                  page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
                  _user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    faculty = session.get(Faculty, faculty_id) if faculty_id else None
    if faculty_id and not faculty:
        raise HTTPException(404, "Факультет не найден.")
    rows, *_ = organization_registry(session, q, faculty.name if faculty else "", str(end_year or ""), urgency or "")
    if status_value:
        rows = [row for row in rows if (
            row.contract.active_agreement.status if row.contract.active_agreement else row.contract.status
        ) == status_value]
    total = len(rows)
    rows = rows[(page - 1) * per_page:page * per_page]
    return {
        "items": [{
            "organization_id": row.contract.organization_id,
            "organization": row.contract.organization.short_name,
            "contract_id": row.contract.id,
            "contract_number": row.contract.number,
            "faculty": faculty_data(row.faculty),
            "specialties": row.specialty_codes,
            "status": row.contract.active_agreement.status if row.contract.active_agreement else row.contract.status,
            "date_end": row.contract.end_date,
        } for row in rows],
        "total": total, "page": page, "per_page": per_page,
    }


@router.get("/organizations/{organization_id}", tags=["organizations"], response_model=OrganizationResponse, responses=ERRORS,
            summary="Получить карточку организации с договорами и заявками")
def organization_card(organization_id: int, _user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    value = session.get(Organization, organization_id)
    if not value:
        raise HTTPException(404, "Организация не найдена.")
    return {
        "id": value.id, "unp": value.unp, "short_name": value.short_name, "full_name": value.full_name,
        "legal_address": value.legal_address, "authority": value.authority, "phone": value.phone,
        "contracts": [contract_data(row) for row in value.contracts],
        "applications": [application_data(row) for row in value.applications],
    }


@router.get("/statistics", tags=["organizations"], response_model=StatisticsResponse, responses=ERRORS,
            summary="Получить статистику реестра")
def statistics(faculty_id: int | None = None, _user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    value = registry_statistics(session, faculty_id)
    return {**asdict(value), "attention_total": value.attention_total}


@router.get("/contracts", tags=["contracts"], response_model=ContractPage, responses=ERRORS,
            summary="Получить сквозной реестр договоров")
def contracts(q: str = Query("", examples=["МТЗ"]), faculty_ids: list[int] = Query(default=[]),
              status_value: ContractStatusFilter | None = Query(None, alias="status"), end_year: int | None = None,
              urgency: ContractUrgencyFilter | None = Query(None), page: int = Query(1, ge=1),
              per_page: int = Query(50, ge=1, le=100),
              _user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    names = faculty_names(session, faculty_ids)
    result, *_ = contract_registry(session, query_text=q, faculty=names, status=status_value or "",
                                   end_year=str(end_year or ""), urgency=urgency or "", page=None)
    rows = result.rows[(page - 1) * per_page:page * per_page]
    return {"items": [contract_data(row.contract, effective_status=row.effective_status,
                                     specialty_count=row.specialty_count) for row in rows],
            "total": result.total, "page": page, "per_page": per_page}


@router.get("/contracts/{contract_id}", tags=["contracts"], response_model=ContractResponse, responses=ERRORS,
            summary="Получить карточку договора")
def contract_card(contract_id: int, _user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    value = session.get(Contract, contract_id)
    if not value:
        raise HTTPException(404, "Договор не найден.")
    return contract_data(value, detail=True)


@router.post("/contracts", tags=["contracts"], response_model=ContractResponse, status_code=201, responses=ERRORS,
             summary="Создать договор")
def create_contract_api(payload: ContractWrite, request: Request, user: AppUser = Security(require_api_user),
                        session: Session = Depends(get_session)):
    if not session.get(Organization, payload.organization_id):
        raise HTTPException(404, "Организация не найдена.")
    try:
        value = create_contract(session, payload.organization_id, faculty_names(session, payload.faculty_ids),
                                payload.number, payload.date_end.isoformat() if payload.date_end else "",
                                payload.date_start.isoformat(), audit_actor=actor(request, user))
        return contract_data(value, detail=True)
    except ValueError as error:
        mutation_error(error)


@router.put("/contracts/{contract_id}", tags=["contracts"], response_model=ContractResponse, responses=ERRORS,
            summary="Изменить договор")
def update_contract_api(contract_id: int, payload: ContractUpdate, request: Request,
                        user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    value = session.get(Contract, contract_id)
    if not value:
        raise HTTPException(404, "Договор не найден.")
    try:
        update_contract(session, value, payload.number, payload.date_start.isoformat(),
                        payload.date_end.isoformat() if payload.date_end else "", faculty_names(session, payload.faculty_ids),
                        audit_actor=actor(request, user))
        return contract_data(value, detail=True)
    except ValueError as error:
        mutation_error(error)


@router.post("/contracts/{contract_id}/status", tags=["contracts"], response_model=StatusResponse, responses=ERRORS,
             summary="Сменить статус договора")
def contract_status(contract_id: int, payload: StatusChangeRequest, request: Request,
                    user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    value = session.get(Contract, contract_id)
    if not value:
        raise HTTPException(404, "Договор не найден.")
    try:
        change_contract_status(session, value, payload.status, user.role, payload.comment or "", audit_actor=actor(request, user))
    except StatusTransitionError as error:
        raise HTTPException(400, str(error)) from error
    return {"status": value.status}


@router.post("/contracts/{contract_id}/additional-agreements", tags=["contracts"], response_model=AdditionalAgreementResponse,
             status_code=201, responses=ERRORS, summary="Зарегистрировать и активировать дополнительное соглашение",
             description="Операция атомарно регистрирует д.с., копирует текущий заказ в новую редакцию и активирует её.")
def create_agreement_api(contract_id: int, payload: AgreementCreate, request: Request,
                         user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    contract = session.get(Contract, contract_id)
    if not contract:
        raise HTTPException(404, "Договор не найден.")
    try:
        return register_additional_agreement(session, contract, payload.number, payload.date, user.id,
                                             audit_actor=actor(request, user))
    except ValueError as error:
        mutation_error(error)


@router.post("/additional-agreements/{agreement_id}/activate", tags=["contracts"], response_model=StatusResponse,
             responses=ERRORS, summary="Повторно активировать зарегистрированное д.с.")
def activate_agreement_api(agreement_id: int, request: Request, user: AppUser = Security(require_api_user),
                           session: Session = Depends(get_session)):
    value = session.get(AdditionalAgreement, agreement_id)
    if not value:
        raise HTTPException(404, "Дополнительное соглашение не найдено.")
    try:
        change_agreement_status(session, value, "Активен", user.role, "", audit_actor=actor(request, user))
    except StatusTransitionError as error:
        raise HTTPException(400, str(error)) from error
    return {"status": value.status}


@router.post("/additional-agreements/{agreement_id}/status", tags=["contracts"], response_model=StatusResponse,
             responses=ERRORS, summary="Сменить статус дополнительного соглашения")
def agreement_status(agreement_id: int, payload: StatusChangeRequest, request: Request,
                     user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    value = session.get(AdditionalAgreement, agreement_id)
    if not value:
        raise HTTPException(404, "Дополнительное соглашение не найдено.")
    try:
        change_agreement_status(session, value, payload.status, user.role, payload.comment or "", audit_actor=actor(request, user))
    except StatusTransitionError as error:
        raise HTTPException(400, str(error)) from error
    return {"status": value.status}


def history_data(value) -> dict:
    return {"document_type": value.document_type, "document_id": value.document_id, "title": value.title,
            "revisions": [{"order_id": row.order.id, "revision": row.number, "created_at": row.created_at,
                           "origin": row.origin_label, "origin_kind": row.origin_kind, "is_current": row.is_current,
                           "created_by": row.creator_name, "item_count": row.item_count} for row in value.revisions]}


@router.get("/contracts/{contract_id}/order-history", tags=["contracts", "orders"], response_model=RevisionHistoryResponse,
            responses=ERRORS, summary="Получить историю редакций заказа договора")
def contract_history(contract_id: int, _user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    value = get_order_history(session, "contract", contract_id)
    if not value:
        raise HTTPException(404, "История заказа не найдена.")
    return history_data(value)


@router.get("/additional-agreements/{agreement_id}/order-history", tags=["contracts", "orders"],
            response_model=RevisionHistoryResponse, responses=ERRORS, summary="Получить историю редакций заказа д.с.")
def agreement_history(agreement_id: int, _user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    value = get_order_history(session, "additional_agreement", agreement_id)
    if not value:
        raise HTTPException(404, "История заказа не найдена.")
    return history_data(value)


@router.get("/contracts/{contract_id}/order-comparison", tags=["contracts", "orders"],
            response_model=RevisionComparisonResponse, responses=ERRORS, summary="Сравнить две редакции заказа")
def compare_contract_orders(contract_id: int, first_id: int, second_id: int,
                            _user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    history = get_order_history(session, "contract", contract_id)
    comparison = compare_revisions(session, history, first_id, second_id) if history else None
    if not comparison:
        raise HTTPException(404, "Редакции для сравнения не найдены.")
    before, after, rows, years = comparison
    return {"before_order_id": before.order.id, "after_order_id": after.order.id, "years": years,
            "rows": [{"faculty": row["faculty"], "specialty": row["specialty"], "change": row["change"],
                      "before": row["before"], "after": row["after"]} for row in rows]}


@router.get("/applications", tags=["applications"], response_model=ApplicationPage, responses=ERRORS,
            summary="Получить сквозной реестр заявок",
            description="Поддерживает фильтр по году и бакету срочности поля «Действует до».")
def applications(q: str = Query("", examples=["МТЗ"]), faculty_ids: list[int] = Query(default=[]),
                 status_value: ApplicationStatusFilter | None = Query(None, alias="status"),
                 end_year: int | None = Query(None), urgency: ApplicationUrgencyFilter | None = Query(None),
                 page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
                 _user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    result, *_ = application_registry(session, query_text=q, faculty=faculty_names(session, faculty_ids),
                                      status=status_value or "", end_year=str(end_year or ""),
                                      urgency=urgency or "", page=None)
    rows = result.rows[(page - 1) * per_page:page * per_page]
    return {"items": [application_data(row) for row in rows], "total": result.total, "page": page, "per_page": per_page}


@router.get("/applications/{application_id}", tags=["applications"], response_model=ApplicationResponse, responses=ERRORS,
            summary="Получить карточку заявки")
def application_card(application_id: int, _user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    value = session.get(Application, application_id)
    if not value:
        raise HTTPException(404, "Заявка не найдена.")
    return application_data(value, detail=True)


@router.post("/applications", tags=["applications"], response_model=ApplicationResponse, status_code=201, responses=ERRORS,
             summary="Создать заявку")
def create_application_api(payload: ApplicationWrite, request: Request, user: AppUser = Security(require_api_user),
                           session: Session = Depends(get_session)):
    if not session.get(Organization, payload.organization_id):
        raise HTTPException(404, "Организация не найдена.")
    value = create_application(session, payload.organization_id, faculty_names(session, payload.faculty_ids), payload.number,
                               payload.signed_date.isoformat() if payload.signed_date else "",
                               payload.date_end.isoformat() if payload.date_end else "", user.id,
                               audit_actor=actor(request, user))
    return application_data(value, detail=True)


@router.put("/applications/{application_id}", tags=["applications"], response_model=ApplicationResponse, responses=ERRORS,
            summary="Изменить заявку")
def update_application_api(application_id: int, payload: ApplicationUpdate, request: Request,
                           user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    value = session.get(Application, application_id)
    if not value:
        raise HTTPException(404, "Заявка не найдена.")
    update_application(session, value, payload.number, payload.signed_date.isoformat() if payload.signed_date else "",
                       payload.date_end.isoformat() if payload.date_end else "", audit_actor=actor(request, user))
    return application_data(value, detail=True)


@router.post("/applications/{application_id}/status", tags=["applications"], response_model=StatusResponse,
             responses=ERRORS, summary="Сменить статус заявки")
def application_status(application_id: int, payload: StatusChangeRequest, request: Request,
                       user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    value = session.get(Application, application_id)
    if not value:
        raise HTTPException(404, "Заявка не найдена.")
    try:
        change_application_status(session, value, payload.status, user.role, payload.comment or "", audit_actor=actor(request, user))
    except StatusTransitionError as error:
        raise HTTPException(400, str(error)) from error
    return {"status": value.status}


@router.get("/orders/{order_id}", tags=["orders"], response_model=OrderResponse, responses=ERRORS,
            summary="Получить редакцию кадрового заказа")
def get_order(order_id: int, _user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    value = session.get(Order, order_id)
    if not value:
        raise HTTPException(404, "Редакция заказа не найдена.")
    return order_data(value)


def order_form(payload: OrderItemWrite) -> dict[str, str]:
    return {"faculty_id": str(payload.faculty_id), "profile": payload.profile or "",
            **{f"demand_{year}": str(quantity) for year, quantity in payload.years.items()}}


def save_order_item(session: Session, order: Order, payload: OrderItemWrite, request: Request, user: AppUser,
                    item: OrderItem | None = None) -> OrderItem:
    specialty = session.get(Specialty, payload.specialty_id)
    if not specialty:
        raise HTTPException(422, "Выберите специальность из справочника.")
    try:
        if order.application_id:
            application = session.get(Application, order.application_id)
            return save_application_item(session, application, specialty.code, payload.qualification or "", order_form(payload),
                                         item, catalog_only=True, audit_actor=actor(request, user))
        if order.contract_id:
            return save_item(session, order.contract_id, specialty.code, payload.qualification or "", order_form(payload), item,
                             user.id, catalog_only=True, audit_actor=actor(request, user))
        raise HTTPException(409, "Редакция не связана с документом.")
    except (InactiveOrderRevisionError, ValueError) as error:
        mutation_error(error)


@router.post("/orders/{order_id}/items", tags=["orders"], response_model=OrderItemResponse, status_code=201,
             responses=ERRORS, summary="Добавить строку в действующую редакцию заказа")
def create_order_item(order_id: int, payload: OrderItemWrite, request: Request, user: AppUser = Security(require_api_user),
                      session: Session = Depends(get_session)):
    order = session.get(Order, order_id)
    if not order:
        raise HTTPException(404, "Редакция заказа не найдена.")
    if not order.is_current:
        raise HTTPException(409, "Заменённую редакцию заказа нельзя изменять.")
    return item_data(save_order_item(session, order, payload, request, user))


@router.post("/contracts/{contract_id}/order-items", tags=["contracts", "orders"],
             response_model=OrderItemResponse, status_code=201, responses=ERRORS,
             summary="Добавить первую или очередную строку заказа договора",
             description="Используйте этот маршрут, когда у нового договора current_order ещё равен null.")
def create_contract_order_item(contract_id: int, payload: OrderItemWrite, request: Request,
                               user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    contract = session.get(Contract, contract_id)
    if not contract:
        raise HTTPException(404, "Договор не найден.")
    specialty = session.get(Specialty, payload.specialty_id)
    if not specialty:
        raise HTTPException(422, "Выберите специальность из справочника.")
    try:
        item = save_item(
            session, contract.id, specialty.code, payload.qualification or "", order_form(payload),
            user_id=user.id, catalog_only=True, audit_actor=actor(request, user),
        )
    except (InactiveOrderRevisionError, ValueError) as error:
        mutation_error(error)
    return item_data(item)


@router.put("/order-items/{item_id}", tags=["orders"], response_model=OrderItemResponse, responses=ERRORS,
            summary="Изменить строку действующей редакции заказа")
def update_order_item(item_id: int, payload: OrderItemWrite, request: Request, user: AppUser = Security(require_api_user),
                      session: Session = Depends(get_session)):
    item = session.get(OrderItem, item_id)
    if not item:
        raise HTTPException(404, "Строка заказа не найдена.")
    return item_data(save_order_item(session, item.order, payload, request, user, item))


@router.delete("/order-items/{item_id}", tags=["orders"], status_code=204, responses=ERRORS,
               summary="Удалить строку действующей редакции заказа",
               description="Успешный ответ 204 не содержит тела — это штатно.")
def delete_order_item(item_id: int, request: Request, user: AppUser = Security(require_api_user),
                      session: Session = Depends(get_session)):
    item = session.get(OrderItem, item_id)
    if not item:
        raise HTTPException(404, "Строка заказа не найдена.")
    try:
        delete_item(session, item, audit_actor=actor(request, user))
    except InactiveOrderRevisionError as error:
        raise HTTPException(409, str(error)) from error
    return None


@router.get("/audit", tags=["audit"], response_model=AuditPage, responses=ERRORS,
            summary="Получить журнал действий")
def audit(user_id: int | None = Query(None, examples=[1]), entity_type: str = "", action: str = "",
          entity_id: int | None = None, date_from: date | None = None, date_to: date | None = None,
          search: str = "", page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
          _user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    result = get_audit_registry(session, user=str(user_id or ""), entity=entity_type, entity_id=entity_id, action=action,
                                date_from=date_from, date_to=date_to, query=search, page=page)
    records = {row.id: session.get(AuditLog, row.id) for row in result.rows}
    items = [{
        "id": row.id, "created_at": row.timestamp,
        "user": {"id": records[row.id].user_id, "full_name": row.employee},
        "action": row.action, "entity_type": row.entity_type, "entity_id": records[row.id].entity_id,
        "entity_label": row.entity_label, "diff": records[row.id].diff or {"old": {}, "new": {}},
        "comment": row.comment,
    } for row in result.rows]
    return {"items": items, "total": result.total, "page": result.page, "per_page": per_page}


@router.get("/audit/metadata", tags=["audit"], response_model=AuditMetadataResponse, responses=ERRORS,
            summary="Получить русские подписи кодов журнала")
def audit_metadata(_user: AppUser = Security(require_api_user)):
    return {"actions": ACTION_LABELS, "entity_types": {key: value[0] for key, value in ENTITY_FILTERS.items()}}


@router.get("/users", tags=["users"], response_model=UserPage, responses=ERRORS, summary="Получить пользователей",
            description="Только ADMIN.")
def users(page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100),
          _admin: AppUser = Security(require_admin_user), session: Session = Depends(get_session)):
    statement = select(AppUser).where(AppUser.role.in_(("ADMIN", "HEAD"))).order_by(AppUser.full_name, AppUser.id)
    total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    values = session.scalars(statement.offset((page - 1) * per_page).limit(per_page)).all()
    return {"items": [user_data(row) for row in values], "total": total, "page": page, "per_page": per_page}


@router.post("/users", tags=["users"], response_model=UserResponse, status_code=201, responses=ERRORS,
             summary="Создать пользователя", description="Только ADMIN. Email обязателен и уникален.")
def create_user_api(payload: UserCreate, request: Request, admin: AppUser = Security(require_admin_user),
                    session: Session = Depends(get_session)):
    try:
        value = create_user(session, payload.full_name, payload.username, str(payload.email), payload.role,
                            payload.initial_password, audit_actor=actor(request, admin))
    except ValueError as error:
        mutation_error(error)
    return user_data(value)


@router.put("/users/{user_id}", tags=["users"], response_model=UserResponse, responses=ERRORS,
            summary="Изменить ФИО и роль пользователя", description="Только ADMIN.")
def update_user_api(user_id: int, payload: UserUpdate, request: Request, admin: AppUser = Security(require_admin_user),
                    session: Session = Depends(get_session)):
    value = session.get(AppUser, user_id)
    if not value:
        raise HTTPException(404, "Пользователь не найден.")
    try:
        update_user(session, value, payload.full_name, payload.role, audit_actor=actor(request, admin))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return user_data(value)


@router.patch("/users/{user_id}/status", tags=["users"], response_model=UserResponse, responses=ERRORS,
              summary="Активировать или деактивировать пользователя",
              description=("Только ADMIN. Нельзя деактивировать себя или последнего активного "
                           "администратора. Деактивированный пользователь немедленно теряет доступ."))
def update_user_status_api(user_id: int, payload: UserStatusUpdate, request: Request,
                           admin: AppUser = Security(require_admin_user),
                           session: Session = Depends(get_session)):
    value = session.get(AppUser, user_id)
    if not value:
        raise HTTPException(404, "Пользователь не найден.")
    try:
        set_user_active(
            session,
            value,
            payload.is_active,
            actor_user_id=admin.id,
            audit_actor=actor(request, admin),
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return user_data(value)


@router.post("/users/{user_id}/reset-password", tags=["users"], status_code=204, responses=ERRORS,
             summary="Сбросить пароль пользователя", description="Только ADMIN. Ответ 204 без тела — это штатно.")
def reset_user_password(user_id: int, payload: PasswordReset, request: Request,
                        admin: AppUser = Security(require_admin_user), session: Session = Depends(get_session)):
    value = session.get(AppUser, user_id)
    if not value:
        raise HTTPException(404, "Пользователь не найден.")
    reset_password(session, value, payload.new_password, audit_actor=actor(request, admin))
    return None


@router.get("/settings/profile", tags=["settings"], response_model=AuthenticatedUserResponse, responses=ERRORS,
            summary="Получить профиль текущего сотрудника (устаревший дубль)", deprecated=True,
            description="Используйте GET /api/auth/me. Маршрут временно сохранён для совместимости фронтенда.")
def profile(user: AppUser = Security(require_api_user)):
    return {"id": user.id, "username": user.username, "email": user.email, "full_name": user.full_name,
            "role": user.role, "need_password_change": user.must_change_password, "need_email": not bool(user.email)}


async def uploaded_xlsx(file: UploadFile) -> tuple[Path, str]:
    filename = Path(file.filename or "").name
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "Нужен файл Excel .xlsx")
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    path = Path(handle.name)
    try:
        while chunk := await file.read(1024 * 1024):
            handle.write(chunk)
    finally:
        handle.close()
    return path, filename


@router.post("/import", tags=["import-export"], response_model=ImportResponse, responses=ERRORS,
             summary="Импортировать договоры, д.с. или заявки из Excel")
async def import_file(request: Request, file: UploadFile = File(...), create_new: bool = Form(False),
                      user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    path, filename = await uploaded_xlsx(file)
    try:
        try:
            result = import_xlsx(session, path, user.id, filename, create_unknown_organizations=create_new,
                                 audit_actor=actor(request, user))
        except (ValueError, KeyError) as error:
            raise HTTPException(422, str(error)) from error
        return {
            "rows": result.rows_processed, "organizations": result.organizations,
            "organizations_created": result.organizations_created, "organizations_updated": result.organizations_updated,
            "contracts": result.contracts, "contracts_created": result.contracts_created,
            "contracts_updated": result.contracts_updated, "applications": result.applications,
            "applications_created": result.applications_created, "applications_updated": result.applications_updated,
            "faculties": result.faculties, "specialties": result.specialties,
            "order_items_created": result.order_items_created, "order_items_updated": result.order_items_updated,
            "skipped_rows": result.skipped_rows, "skipped_organizations": result.skipped_organizations,
            "skipped_names": list(result.skipped_names),
        }
    finally:
        path.unlink(missing_ok=True)


@router.get("/reconciliation/{token}/export", tags=["import-export"], responses=BINARY_RESPONSE,
            summary="Скачать отчёт сверки с АИС")
def export_reconciliation(token: str, _user: AppUser = Security(require_api_user)):
    try:
        report = load_report(token)
    except (FileNotFoundError, ValueError, OSError):
        raise HTTPException(404, "Отчёт сверки не найден.")
    return StreamingResponse(report_xlsx(report), media_type=XLSX,
                             headers={"Content-Disposition": 'attachment; filename="reconciliation.xlsx"'})


@router.post("/reconciliation", tags=["import-export"], response_model=ReconciliationResponse, responses=ERRORS,
             summary="Сверить данные с выгрузкой АИС без изменения БД")
async def reconciliation(request: Request, file: UploadFile = File(...), user: AppUser = Security(require_api_user),
                         session: Session = Depends(get_session)):
    path, filename = await uploaded_xlsx(file)
    try:
        try:
            report = reconcile_xlsx(session, path, filename, audit_actor=actor(request, user))
        except (ValueError, KeyError) as error:
            raise HTTPException(422, str(error)) from error
        return {"token": report.token, "filename": report.filename,
                "organizations_checked": report.organizations_checked,
                "organizations_skipped": report.organizations_skipped, "skipped_names": report.skipped_names,
                "differences": [asdict(row) for row in report.differences]}
    finally:
        path.unlink(missing_ok=True)


@router.get("/imports", tags=["import-export"], response_model=ImportHistoryResponse, responses=ERRORS,
            summary="Получить последние десять импортов")
def import_history(_user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    result = session.execute(
        select(AuditLog, AppUser.full_name, AppUser.username).outerjoin(AppUser, AppUser.id == AuditLog.user_id)
        .where(AuditLog.action == "FILE_UPLOAD", AuditLog.entity_type == "excel_import")
        .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc()).limit(10)
    ).all()
    keys = ("organizations_created", "organizations_updated", "contracts_created", "contracts_updated",
            "applications_created", "applications_updated", "order_items_created", "order_items_updated")
    return {"items": [{"filename": ((record.diff or {}).get("new") or {}).get("filename") or record.entity_label,
                       "created_at": record.timestamp, "user": full_name or username or "Система",
                       "counters": (record.diff or {}).get("new") or {},
                       "no_changes": not any((((record.diff or {}).get("new") or {}).get(key, 0)) for key in keys)}
                      for record, full_name, username in result]}


@router.get("/export/contracts", tags=["import-export"], responses=BINARY_RESPONSE,
            summary="Экспортировать отфильтрованные договоры построчно")
def export_contracts_api(q: str = "", faculty_ids: list[int] = Query(default=[]),
                         status_value: ContractStatusFilter | None = Query(None, alias="status"),
                         end_year: int | None = None, urgency: ContractUrgencyFilter | None = Query(None),
                         _user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    names = faculty_names(session, faculty_ids)
    result, *_ = contract_registry(session, query_text=q, faculty=names, status=status_value or "",
                                   end_year=str(end_year or ""), urgency=urgency or "", page=None)
    return StreamingResponse(contracts_xlsx(result, names), media_type=XLSX,
                             headers={"Content-Disposition": 'attachment; filename="contracts.xlsx"'})


@router.get("/export/applications", tags=["import-export"], responses=BINARY_RESPONSE,
            summary="Экспортировать отфильтрованные заявки построчно",
            description="Учитывает фильтры по году и бакету срочности поля «Действует до».")
def export_applications_api(q: str = "", faculty_ids: list[int] = Query(default=[]),
                            status_value: ApplicationStatusFilter | None = Query(None, alias="status"),
                            end_year: int | None = Query(None), urgency: ApplicationUrgencyFilter | None = Query(None),
                            _user: AppUser = Security(require_api_user), session: Session = Depends(get_session)):
    names = faculty_names(session, faculty_ids)
    result, *_ = application_registry(session, query_text=q, faculty=names, status=status_value or "",
                                      end_year=str(end_year or ""), urgency=urgency or "", page=None)
    return StreamingResponse(applications_xlsx(result, names), media_type=XLSX,
                             headers={"Content-Disposition": 'attachment; filename="applications.xlsx"'})
