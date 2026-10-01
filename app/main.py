import json, logging, os, tempfile
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlencode
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import func, select
from .api.router import OPENAPI_TAGS, router as api_router
from .models import SessionLocal, Organization, Contract, OrderItem, AppUser, AdditionalAgreement, Application, AuditLog, DocumentAttachment, Faculty, Specialty
from .services.import_service import import_xlsx
from .services.reconciliation_service import load_report, reconcile_xlsx, report_xlsx
from .services.organization_service import InactiveOrderRevisionError, compare_agreement_order, create_contract, create_organization, delete_item, organization_contracts, register_additional_agreement, registry as get_registry, save_item, update_contract, update_organization
from .services.auth_service import (
    AccountDisabledError,
    authenticate,
    change_password,
    create_initial_admin,
    create_user,
    has_users,
    set_user_active,
    update_email,
    update_user,
    verify_password,
)
from .services.application_service import create_application, save_application_item, update_application
from .services.audit_service import AuditActor
from .services.audit_registry_service import ACTION_LABELS, ENTITY_FILTERS, get_audit_registry
from .services.document_status_service import StatusTransitionError, allowed_status_transitions, change_agreement_status, change_application_status, change_contract_status
from .services.file_service import (
    AttachmentError,
    FILE_KIND_LABELS,
    MANUAL_FILE_KINDS,
    MAX_FILE_SIZE,
    attachment_destination,
    create_attachment,
    delete_attachment,
    get_attachment_metadata,
    restore_attachment,
    stream_attachment,
)
from .services.order_history_service import compare_revisions, get_order_history, get_revision, order_table
from .services.document_registry_service import application_registry as get_application_registry, contract_registry as get_contract_registry
from .services.status_service import APPLICATION_STATUSES, CONTRACT_STATUSES, URGENCY_BUCKETS, expiry_urgency, order_change_class, status_class, status_label
from .services.specialty_service import specialty_registry, update_specialty
from .services.statistics_service import registry_statistics
from .services.registry_export_service import applications_xlsx, contracts_xlsx
from .services.form_validation import (
    AgreementForm,
    ApplicationForm,
    ContractForm,
    EmailForm,
    FormValidationError,
    OrganizationForm,
    UserCreateForm,
    validate_form,
    validate_order_form,
)
logger = logging.getLogger("uvicorn.error")

Path("data").mkdir(exist_ok=True)
app = FastAPI(
    title="Кадровый заказ API",
    description=(
        "JSON API для React-интерфейса системы «Кадровый заказ». "
        "Для проверки защищённых методов выполните POST /api/auth/login прямо в Swagger: "
        "браузер сохранит session-cookie и будет отправлять её автоматически."
    ),
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_tags=OPENAPI_TAGS,
)
app.include_router(api_router)
app.mount("/static", StaticFiles(directory="static"), name="static")
views = Jinja2Templates(directory="app/views")
views.env.filters["fromjson"] = json.loads


def _validation_response(request: Request, errors: dict[str, str]):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "; ".join(errors.values())}, status_code=422)
    return views.TemplateResponse(request, "validation_error.html", {"errors": errors}, status_code=422)


@app.exception_handler(FormValidationError)
async def form_validation_error(request: Request, error: FormValidationError):
    return _validation_response(request, error.errors)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, error: RequestValidationError):
    errors = {}
    for item in error.errors():
        field = str(item.get("loc", ("form",))[-1])
        errors[field] = "Обязательное поле не заполнено." if item.get("type") == "missing" else "Некорректное значение поля."
    return _validation_response(request, errors)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, error: Exception):
    logger.exception("Unhandled request error on %s", request.url.path, exc_info=error)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Произошла ошибка. Обратитесь к администратору."}, status_code=500)
    return views.TemplateResponse(request, "server_error.html", {}, status_code=500)


views.env.filters["document_status"] = status_label
views.env.filters["status_class"] = status_class
views.env.filters["order_change_class"] = order_change_class
views.env.globals["allowed_status_transitions"] = allowed_status_transitions
views.env.globals["file_kind_labels"] = FILE_KIND_LABELS


def human_file_size(value: int) -> str:
    size = float(value or 0)
    for unit in ("Б", "КБ", "МБ"):
        if size < 1024 or unit == "МБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} МБ"


views.env.filters["filesize"] = human_file_size


async def read_uploaded_file(file: UploadFile) -> bytes:
    content = bytearray()
    while chunk := await file.read(1024 * 1024):
        content.extend(chunk)
        if len(content) > MAX_FILE_SIZE:
            raise AttachmentError("Файл превышает допустимый размер 50 МБ.")
    return bytes(content)


def load_attachment_relations(entity) -> None:
    for attachment in entity.attachments:
        _ = attachment.uploader


def attachment_error_redirect(destination: str, error: Exception) -> RedirectResponse:
    separator = "&" if "?" in destination else "?"
    return RedirectResponse(f"{destination}{separator}file_error={quote(str(error))}", status_code=303)


def nav_is_active(request: Request, section: str) -> bool:
    """Keep sidebar selection correct for list and nested resource routes."""
    path = request.url.path
    prefixes = {
        "organizations": ("/organizations",),
        "applications": ("/applications",),
        "contracts": ("/contracts", "/additional-agreements"),
        "documents": ("/documents",),
        "audit": ("/audit",),
        "users": ("/users",),
        "settings": ("/settings",),
        "specialties": ("/specialties",),
        "import_export": ("/import-export", "/reconciliation"),
    }
    return (section == "organizations" and path == "/") or path.startswith(prefixes[section])


views.env.globals["nav_is_active"] = nav_is_active

def db(): return SessionLocal()
def audit_actor(request: Request):
    client_ip = request.client.host if request.client else None
    return AuditActor(request.state.user.id, client_ip)
def urgency_class(end):
    value = expiry_urgency(end)
    return value.css_class if value else "neutral"
views.env.globals["urgency"] = urgency_class
views.env.globals["today"] = date.today

def show_docs_enabled() -> bool:
    return os.getenv("SHOW_DOCS", "true").strip().lower() in {"1", "true", "yes", "on"}


@app.get("/docs", include_in_schema=False)
def swagger_ui():
    if not show_docs_enabled():
        raise HTTPException(404)
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} — Swagger UI",
        swagger_ui_parameters={"persistAuthorization": True},
    )


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    public_paths = {
        "/setup", "/login", "/docs", "/openapi.json",
        "/api/auth/login", "/api/health",
    }
    if path.startswith("/static") or path in public_paths:
        return await call_next(request)
    user_id = request.session.get("user_id")
    if not user_id:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Требуется авторизация."}, status_code=401)
        session = db()
        destination = "/login" if has_users(session) else "/setup"
        session.close()
        return RedirectResponse(destination, status_code=303)
    s = db(); user = s.get(AppUser, user_id)
    if not user or not user.is_active:
        configured = has_users(s)
        s.close()
        request.session.clear()
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Требуется авторизация."}, status_code=401)
        destination = "/login?disabled=1" if configured else "/setup"
        return RedirectResponse(destination, status_code=303)
    request.state.user = user
    must_change_password = user.must_change_password
    must_set_email = not bool(user.email)
    s.close()
    password_change_paths = {
        "/change-password", "/logout", "/api/auth/me",
        "/api/auth/logout", "/api/auth/change-password",
    }
    if must_change_password and path not in password_change_paths:
        if path.startswith("/api/"):
            return JSONResponse(
                {"detail": "Необходимо сменить временный пароль."},
                status_code=403,
            )
        return RedirectResponse("/change-password", status_code=303)
    email_paths = {
        "/set-email", "/logout", "/api/auth/me", "/api/auth/logout", "/api/auth/email",
    }
    if must_set_email and path not in email_paths:
        if path.startswith("/api/"):
            return JSONResponse(
                {"detail": "Необходимо указать электронную почту."},
                status_code=403,
            )
        return RedirectResponse("/set-email", status_code=303)
    return await call_next(request)

app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "change-me-before-production"), https_only=False)

@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, disabled: bool = False):
    session = db()
    configured = has_users(session)
    session.close()
    if not configured:
        return RedirectResponse("/setup", status_code=303)
    error = "Учётная запись отключена. Обратитесь к администратору" if disabled else ""
    return views.TemplateResponse(request, "login.html", {"error": error})

@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    client_ip = request.client.host if request.client else None
    s = db()
    try:
        user = authenticate(s, username, password, client_ip)
    except AccountDisabledError as error:
        s.close()
        return views.TemplateResponse(request, "login.html", {"error": str(error)}, status_code=401)
    if not user:
        s.close()
        return views.TemplateResponse(request, "login.html", {"error": "Неверный логин или пароль."}, status_code=401)
    request.session["user_id"] = user.id
    must_change_password = user.must_change_password
    must_set_email = not bool(user.email)
    s.close()
    destination = "/change-password" if must_change_password else "/set-email" if must_set_email else "/"
    return RedirectResponse(destination, status_code=303)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request):
    session = db()
    configured = has_users(session)
    session.close()
    if configured:
        return RedirectResponse("/login", status_code=303)
    return views.TemplateResponse(request, "setup.html", {"error": ""})


@app.post("/setup", response_class=HTMLResponse)
def setup(
    request: Request,
    full_name: str = Form(""),
    username: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    password_repeat: str = Form(""),
):
    validate_form(UserCreateForm, username=username, email=email, initial_password=password)
    session = db()
    if has_users(session):
        session.close()
        return RedirectResponse("/login", status_code=303)
    try:
        if password != password_repeat:
            raise ValueError("Пароли не совпадают.")
        create_initial_admin(session, full_name, username, email, password)
    except ValueError as error:
        session.close()
        return views.TemplateResponse(request, "setup.html", {
            "error": str(error), "full_name": full_name, "username": username, "email": email,
        }, status_code=422)
    session.close()
    return RedirectResponse("/login", status_code=303)


@app.get("/change-password", response_class=HTMLResponse)
def change_password_form(request: Request):
    return views.TemplateResponse(request, "change_password.html", {
        "error": "", "forced": request.state.user.must_change_password,
    })


@app.post("/change-password", response_class=HTMLResponse)
def save_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    password_repeat: str = Form(...),
):
    session = db()
    user = session.get(AppUser, request.state.user.id)
    try:
        if not verify_password(current_password, user.password_hash):
            raise ValueError("Текущий пароль указан неверно.")
        if new_password != password_repeat:
            raise ValueError("Новые пароли не совпадают.")
        change_password(session, user, new_password, audit_actor=audit_actor(request))
    except ValueError as error:
        forced = user.must_change_password
        session.close()
        return views.TemplateResponse(request, "change_password.html", {
            "error": str(error), "forced": forced,
        }, status_code=422)
    session.close()
    session = db()
    must_set_email = not bool(session.get(AppUser, request.state.user.id).email)
    session.close()
    return RedirectResponse("/set-email" if must_set_email else "/", status_code=303)


@app.get("/set-email", response_class=HTMLResponse)
def set_email_form(request: Request):
    return views.TemplateResponse(request, "set_email.html", {"error": "", "forced": True})


@app.post("/set-email", response_class=HTMLResponse)
def save_required_email(request: Request, email: str = Form("")):
    session = db()
    user = session.get(AppUser, request.state.user.id)
    try:
        validate_form(EmailForm, email=email)
        update_email(session, user, email, audit_actor=audit_actor(request))
    except (FormValidationError, ValueError) as error:
        session.close()
        return views.TemplateResponse(request, "set_email.html", {
            "error": str(error), "forced": True, "email": email,
        }, status_code=422)
    session.close()
    return RedirectResponse("/", status_code=303)


def require_admin(request: Request) -> None:
    if request.state.user.role != "ADMIN":
        raise HTTPException(403, "Раздел доступен только администратору.")


@app.get("/users", response_class=HTMLResponse)
def users_registry(request: Request):
    require_admin(request)
    session = db()
    users = session.query(AppUser).filter(AppUser.role.in_(("ADMIN", "HEAD"))).order_by(AppUser.full_name, AppUser.username).all()
    response = views.TemplateResponse(request, "users.html", {"users": users})
    session.close()
    return response


@app.get("/users/new", response_class=HTMLResponse)
def new_user_form(request: Request):
    require_admin(request)
    return views.TemplateResponse(request, "user_form.html", {"edited_user": None, "error": ""})


@app.post("/users/new", response_class=HTMLResponse)
def add_user(
    request: Request,
    full_name: str = Form(""),
    username: str = Form(""),
    email: str = Form(""),
    role: str = Form(""),
    initial_password: str = Form(""),
    password_repeat: str = Form(""),
):
    require_admin(request)
    validate_form(UserCreateForm, username=username, email=email, initial_password=initial_password)
    session = db()
    try:
        if initial_password != password_repeat:
            raise ValueError("Пароли не совпадают.")
        create_user(
            session, full_name, username, email, role, initial_password,
            audit_actor=audit_actor(request),
        )
    except ValueError as error:
        session.close()
        return views.TemplateResponse(request, "user_form.html", {
            "edited_user": None, "error": str(error), "full_name": full_name,
            "username": username, "email": email, "selected_role": role,
        }, status_code=422)
    session.close()
    return RedirectResponse("/users", status_code=303)


@app.get("/users/{user_id}/edit", response_class=HTMLResponse)
def edit_user_form(request: Request, user_id: int):
    require_admin(request)
    session = db()
    user = session.get(AppUser, user_id)
    if not user:
        session.close()
        raise HTTPException(404)
    response = views.TemplateResponse(request, "user_form.html", {"edited_user": user, "error": ""})
    session.close()
    return response


@app.post("/users/{user_id}/edit", response_class=HTMLResponse)
def save_user(request: Request, user_id: int, full_name: str = Form(...), role: str = Form(...)):
    require_admin(request)
    session = db()
    user = session.get(AppUser, user_id)
    if not user:
        session.close()
        raise HTTPException(404)
    try:
        update_user(session, user, full_name, role, audit_actor=audit_actor(request))
    except ValueError as error:
        response = views.TemplateResponse(request, "user_form.html", {
            "edited_user": user, "error": str(error), "full_name": full_name,
            "selected_role": role,
        }, status_code=400)
        session.close()
        return response
    session.close()
    return RedirectResponse("/users", status_code=303)


@app.post("/users/{user_id}/status")
def save_user_status(request: Request, user_id: int, is_active: bool = Form(...)):
    require_admin(request)
    session = db()
    user = session.get(AppUser, user_id)
    if not user:
        session.close()
        raise HTTPException(404)
    try:
        set_user_active(
            session,
            user,
            is_active,
            actor_user_id=request.state.user.id,
            audit_actor=audit_actor(request),
        )
    except ValueError as error:
        session.close()
        raise HTTPException(400, str(error)) from error
    session.close()
    return RedirectResponse("/users", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_form(request: Request):
    return views.TemplateResponse(request, "settings.html", {"email_error": ""})


@app.post("/settings/email", response_class=HTMLResponse)
def save_settings_email(request: Request, email: str = Form("")):
    session = db()
    user = session.get(AppUser, request.state.user.id)
    try:
        validate_form(EmailForm, email=email)
        update_email(session, user, email, audit_actor=audit_actor(request))
    except (FormValidationError, ValueError) as error:
        session.close()
        return views.TemplateResponse(request, "settings.html", {
            "email_error": str(error), "email": email,
        }, status_code=422)
    session.close()
    return RedirectResponse("/settings", status_code=303)


@app.get("/specialties", response_class=HTMLResponse)
def specialties_registry(request: Request, q: str = "", page: int = 1):
    session = db()
    registry = specialty_registry(session, q, page)
    pagination_query = urlencode({"q": q}) if q else ""
    response = views.TemplateResponse(request, "specialties.html", {
        "registry": registry,
        "q": q,
        "pagination_prefix": f"?{pagination_query}&" if pagination_query else "?",
    })
    session.close()
    return response


@app.post("/specialties/{specialty_id}")
def save_specialty(
    request: Request,
    specialty_id: int,
    name: str = Form(""),
    profile: str = Form(""),
    qualification: str = Form(""),
):
    require_admin(request)
    session = db()
    specialty = session.get(Specialty, specialty_id)
    if not specialty:
        session.close()
        raise HTTPException(404)
    update_specialty(
        session, specialty, name, profile, qualification,
        audit_actor=audit_actor(request),
    )
    session.close()
    return RedirectResponse("/specialties", status_code=303)


@app.get("/audit", response_class=HTMLResponse)
def audit_registry(
    request: Request,
    user: str = "",
    entity: str = "",
    entity_id: int | None = None,
    action: str = "",
    date_from: str = "",
    date_to: str = "",
    q: str = "",
    page: int = 1,
):
    def parse_date(value: str):
        try:
            return date.fromisoformat(value) if value else None
        except ValueError:
            return None

    session = db()
    registry = get_audit_registry(
        session,
        user=user,
        entity=entity,
        entity_id=entity_id,
        action=action,
        date_from=parse_date(date_from),
        date_to=parse_date(date_to),
        query=q,
        page=page,
    )
    session.close()
    pagination_query = urlencode({
        key: value for key, value in {
            "user": user,
            "entity": entity,
            "entity_id": entity_id,
            "action": action,
            "date_from": date_from,
            "date_to": date_to,
            "q": q,
        }.items() if value
    })
    return views.TemplateResponse(request, "audit.html", {
        "registry": registry,
        "action_labels": ACTION_LABELS,
        "entity_filters": ENTITY_FILTERS,
        "selected_user": user,
        "selected_entity": entity,
        "selected_entity_id": entity_id,
        "selected_action": action,
        "date_from": date_from,
        "date_to": date_to,
        "audit_query": q,
        "pagination_query": pagination_query,
        "pagination_prefix": f"?{pagination_query}&" if pagination_query else "?",
    })

@app.get("/", response_class=HTMLResponse)
def registry(
    request: Request,
    q: str = "",
    faculty: str = "",
    end_year: str = "",
    urgency: str = "",
    urgency_choice: str | None = None,
    import_notice: str = "",
):
    valid_urgencies = {key for key, _label in URGENCY_BUCKETS}
    urgency = urgency if urgency in valid_urgencies else ""
    if urgency_choice is not None:
        choice = urgency_choice if urgency_choice in valid_urgencies else ""
        selected = "" if choice == urgency else choice
        query = urlencode({
            key: value for key, value in {
                "q": q, "faculty": faculty, "end_year": end_year, "urgency": selected,
            }.items() if value
        })
        return RedirectResponse(f"/?{query}" if query else "/", status_code=303)
    s = db()
    contracts, faculties, counts, end_years, contract_count, urgency_counts = get_registry(
        s, q, faculty, end_year, urgency,
    )
    selected_faculty_id = None
    if faculty:
        selected_faculty_id = s.query(Faculty.id).filter(Faculty.name == faculty).scalar()
    statistics = registry_statistics(s, selected_faculty_id)
    response = views.TemplateResponse(request, "registry.html", {
        "contracts": contracts,
        "faculties": faculties,
        "contract_count": contract_count,
        "counts": counts,
        "q": q,
        "selected_faculty": faculty,
        "selected_faculty_id": selected_faculty_id,
        "import_notice": import_notice,
        "end_years": end_years,
        "selected_end_year": end_year,
        "urgency_buckets": URGENCY_BUCKETS,
        "urgency_counts": urgency_counts,
        "selected_urgency": urgency,
        "statistics": statistics,
    })
    s.close()
    return response

@app.get("/contracts", response_class=HTMLResponse)
def contracts_registry(
    request: Request,
    q: str = "",
    faculty: list[str] = Query(default=[]),
    status: str = "",
    end_year: str = "",
    urgency: str = "",
    urgency_choice: str | None = None,
    page: int = 1,
):
    valid_urgencies = {key for key, _label in URGENCY_BUCKETS}
    urgency = urgency if urgency in valid_urgencies else ""
    if urgency_choice is not None:
        choice = urgency_choice if urgency_choice in valid_urgencies else ""
        selected = "" if choice == urgency else choice
        query = urlencode([
            *(("faculty", value) for value in faculty),
            *((key, value) for key, value in {
                "q": q, "status": status, "end_year": end_year, "urgency": selected,
            }.items() if value),
        ])
        return RedirectResponse(f"/contracts?{query}" if query else "/contracts", status_code=303)
    session = db()
    registry, faculties, end_years, urgency_counts, selected_urgency = get_contract_registry(
        session,
        query_text=q,
        faculty=faculty,
        status=status,
        end_year=end_year,
        urgency=urgency,
        page=page,
    )
    pagination_query = urlencode([
        *(("faculty", value) for value in faculty),
        *((key, value) for key, value in {
            "q": q, "status": status, "end_year": end_year, "urgency": selected_urgency,
        }.items() if value),
    ])
    response = views.TemplateResponse(request, "contracts.html", {
        "registry": registry,
        "faculties": faculties,
        "end_years": end_years,
        "statuses": CONTRACT_STATUSES,
        "urgency_buckets": URGENCY_BUCKETS,
        "urgency_counts": urgency_counts,
        "selected_urgency": selected_urgency,
        "selected_faculties": faculty,
        "selected_status": status,
        "selected_end_year": end_year,
        "q": q,
        "pagination_prefix": f"?{pagination_query}&" if pagination_query else "?",
        "export_query": pagination_query,
    })
    session.close()
    return response


@app.get("/applications", response_class=HTMLResponse)
def applications(
    request: Request, q: str = "", faculty: list[str] = Query(default=[]), status: str = "",
    end_year: str = "", urgency: str = "", urgency_choice: str | None = None, page: int = 1,
):
    valid_urgencies = {key for key, _label in URGENCY_BUCKETS}
    urgency = urgency if urgency in valid_urgencies else ""
    if urgency_choice is not None:
        choice = urgency_choice if urgency_choice in valid_urgencies else ""
        selected = "" if choice == urgency else choice
        query = urlencode([
            *(("faculty", value) for value in faculty),
            *((key, value) for key, value in {
                "q": q, "status": status, "end_year": end_year, "urgency": selected,
            }.items() if value),
        ])
        return RedirectResponse(f"/applications?{query}" if query else "/applications", status_code=303)
    session = db()
    registry, faculties, end_years, urgency_counts, selected_urgency = get_application_registry(
        session, query_text=q, faculty=faculty, status=status, end_year=end_year,
        urgency=urgency, page=page,
    )
    pagination_query = urlencode([
        *(("faculty", value) for value in faculty),
        *((key, value) for key, value in {
            "q": q, "status": status, "end_year": end_year, "urgency": selected_urgency,
        }.items() if value),
    ])
    response = views.TemplateResponse(request, "applications.html", {
        "registry": registry,
        "faculties": faculties,
        "statuses": APPLICATION_STATUSES,
        "end_years": end_years,
        "urgency_buckets": URGENCY_BUCKETS,
        "urgency_counts": urgency_counts,
        "q": q,
        "selected_faculties": faculty,
        "selected_status": status,
        "selected_end_year": end_year,
        "selected_urgency": selected_urgency,
        "pagination_prefix": f"?{pagination_query}&" if pagination_query else "?",
        "export_query": pagination_query,
    })
    session.close()
    return response


XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@app.get("/export/contracts")
def export_contracts(q: str = "", faculty: list[str] = Query(default=[]), status: str = "", end_year: str = "", urgency: str = ""):
    session = db()
    registry, *_ = get_contract_registry(
        session, query_text=q, faculty=faculty, status=status,
        end_year=end_year, urgency=urgency, page=None,
    )
    content = contracts_xlsx(registry, faculty)
    session.close()
    return StreamingResponse(content, media_type=XLSX_MEDIA_TYPE, headers={
        "Content-Disposition": 'attachment; filename="contracts.xlsx"',
    })


@app.get("/export/applications")
def export_applications(
    q: str = "", faculty: list[str] = Query(default=[]), status: str = "",
    end_year: str = "", urgency: str = "",
):
    session = db()
    registry, *_ = get_application_registry(
        session, query_text=q, faculty=faculty, status=status, end_year=end_year,
        urgency=urgency, page=None,
    )
    content = applications_xlsx(registry, faculty)
    session.close()
    return StreamingResponse(content, media_type=XLSX_MEDIA_TYPE, headers={
        "Content-Disposition": 'attachment; filename="applications.xlsx"',
    })


@app.get("/import-export", response_class=HTMLResponse)
def import_export_page(request: Request, import_notice: str = ""):
    session = db()
    result = session.execute(
        select(AuditLog, AppUser.full_name, AppUser.username)
        .outerjoin(AppUser, AppUser.id == AuditLog.user_id)
        .where(AuditLog.action == "FILE_UPLOAD", AuditLog.entity_type == "excel_import")
        .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
        .limit(10)
    ).all()
    history = []
    for record, full_name, username in result:
        values = (record.diff or {}).get("new") or {}
        history.append({
            "filename": values.get("filename") or record.entity_label.removeprefix("Импорт Excel "),
            "timestamp": record.timestamp,
            "employee": full_name or username or "Система",
            "values": values,
            "no_changes": not any(values.get(key, 0) for key in (
                "organizations_created", "organizations_updated",
                "contracts_created", "contracts_updated",
                "applications_created", "applications_updated",
                "order_items_created", "order_items_updated",
            )),
        })
    response = views.TemplateResponse(request, "import_export.html", {
        "history": history, "import_notice": import_notice,
    })
    session.close()
    return response

@app.get("/organizations/{org_id}/applications/new", response_class=HTMLResponse)
def new_application(request: Request, org_id: int):
    s = db(); org = s.get(Organization, org_id)
    if not org: raise HTTPException(404)
    return views.TemplateResponse(request, "application_form.html", {"org": org, "faculties": s.query(Faculty).order_by(Faculty.name).all()})

@app.post("/organizations/{org_id}/applications")
def add_application(request: Request, org_id: int, faculty: list[str] = Form(default=[]), number: str = Form(""), signed_date: str = Form(""), date_end: str = Form("")):
    validate_form(ApplicationForm, number=number, signed_date=signed_date, date_end=date_end)
    if not faculty:
        raise FormValidationError({"faculty": "Выберите хотя бы один факультет."})
    s = db(); application = create_application(s, org_id, faculty, number, signed_date, date_end, request.state.user.id, audit_actor=audit_actor(request))
    application_id = application.id
    s.close()
    return RedirectResponse(f"/applications/{application_id}", status_code=303)

@app.get("/applications/{application_id}", response_class=HTMLResponse)
def application_card(request: Request, application_id: int, file_error: str = ""):
    s = db(); application = s.get(Application, application_id)
    if not application: raise HTTPException(404)
    load_attachment_relations(application)
    years = list(range(2026, 2037))
    specialties = s.query(Specialty).order_by(Specialty.code, Specialty.name).all()
    response = views.TemplateResponse(request, "application.html", {
        "application": application, "years": years, "specialties": specialties,
        "all_faculties": s.query(Faculty).order_by(Faculty.name).all(), "file_error": file_error,
    })
    s.close()
    return response

@app.post("/applications/{application_id}")
def edit_application(request: Request, application_id: int, number: str = Form(""), signed_date: str = Form(""), date_end: str = Form("")):
    validate_form(ApplicationForm, number=number, signed_date=signed_date, date_end=date_end)
    s = db(); application = s.get(Application, application_id)
    if not application: raise HTTPException(404)
    update_application(s, application, number, signed_date, date_end, audit_actor=audit_actor(request))
    return RedirectResponse(f"/applications/{application_id}", status_code=303)

@app.post("/applications/{application_id}/status")
def set_application_status(request: Request, application_id: int, status: str = Form(...), comment: str = Form("")):
    s = db(); application = s.get(Application, application_id)
    if not application: raise HTTPException(404)
    try:
        change_application_status(s, application, status, request.state.user.role, comment, audit_actor=audit_actor(request))
    except StatusTransitionError as error:
        s.close()
        return JSONResponse({"detail": str(error)}, status_code=400)
    result = {
        "status": application.status,
        "status_class": status_class(application.status),
        "transitions": allowed_status_transitions("application", application.status, request.state.user.role),
    }
    s.close()
    return result

@app.post("/applications/{application_id}/items")
async def add_application_item(request: Request, application_id: int, specialty: str = Form(""), qualification: str = Form("")):
    form = await request.form()
    validate_order_form(form, specialty)
    s = db(); application = s.get(Application, application_id)
    if not application:
        s.close()
        raise HTTPException(404)
    try:
        save_application_item(s, application, specialty, qualification, form, catalog_only=True, audit_actor=audit_actor(request))
    except InactiveOrderRevisionError as error:
        s.close()
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        s.close()
        raise FormValidationError({"order_item": str(error)}) from error
    s.close()
    return RedirectResponse(f"/applications/{application_id}", status_code=303)

@app.post("/application-items/{item_id}")
async def edit_application_item(request: Request, item_id: int, specialty: str = Form(""), qualification: str = Form("")):
    form = await request.form()
    validate_order_form(form, specialty)
    s = db(); item = s.get(OrderItem, item_id)
    if not item or not item.order.application_id:
        s.close()
        raise HTTPException(404)
    application = s.get(Application, item.order.application_id)
    try:
        save_application_item(s, application, specialty, qualification, form, item, catalog_only=True, audit_actor=audit_actor(request))
    except InactiveOrderRevisionError as error:
        s.close()
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        s.close()
        raise FormValidationError({"order_item": str(error)}) from error
    application_id = application.id
    s.close()
    return RedirectResponse(f"/applications/{application_id}", status_code=303)

@app.post("/applications/{application_id}/files")
async def add_application_file(request: Request, application_id: int, file_kind: str = Form(...), file: UploadFile = File(...)):
    s = db(); application = s.get(Application, application_id)
    if not application: raise HTTPException(404)
    try:
        if file_kind not in MANUAL_FILE_KINDS:
            raise AttachmentError("Этот тип файла нельзя загружать вручную.")
        create_attachment(
            s, application=application, filename=file.filename or "", mime_type=file.content_type or "",
            content=await read_uploaded_file(file), file_kind=file_kind, uploaded_by=request.state.user.id,
            audit_actor=audit_actor(request),
        )
    except AttachmentError as error:
        s.close()
        return attachment_error_redirect(f"/applications/{application_id}", error)
    s.close()
    return RedirectResponse(f"/applications/{application_id}", status_code=303)

@app.post("/import")
async def upload_import(
    request: Request, file: UploadFile = File(...),
    create_unknown_organizations: bool = Form(False),
):
    if not file.filename.lower().endswith(".xlsx"): raise HTTPException(400, "Нужен файл Excel .xlsx")
    original_filename = Path(file.filename).name
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    path = Path(handle.name)
    try:
        while chunk := await file.read(1024 * 1024):
            handle.write(chunk)
    finally:
        handle.close()
    s = db()
    try:
        result = import_xlsx(
            s, path, request.state.user.id, original_filename,
            create_unknown_organizations=create_unknown_organizations,
            audit_actor=audit_actor(request),
        )
        logger.info(result.log_line("веб"))
    except Exception:
        raise
    finally:
        s.close()
        path.unlink(missing_ok=True)
    notice = result.skipped_notice()
    return RedirectResponse(
        f"/import-export?{urlencode({'import_notice': notice})}" if notice else "/import-export",
        status_code=303,
    )

@app.post("/reconciliation", response_class=HTMLResponse)
async def reconcile_with_ais(request: Request, file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(400, "Нужен файл Excel .xlsx")
    original_filename = Path(file.filename).name
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    path = Path(handle.name)
    try:
        while chunk := await file.read(1024 * 1024):
            handle.write(chunk)
    finally:
        handle.close()
    s = db()
    try:
        report = reconcile_xlsx(s, path, original_filename, audit_actor=audit_actor(request))
        return views.TemplateResponse(request, "reconciliation.html", {"report": report})
    finally:
        s.close()
        path.unlink(missing_ok=True)

@app.get("/reconciliation/{token}/export")
def export_reconciliation(token: str):
    try:
        report = load_report(token)
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        raise HTTPException(404, "Отчёт сверки не найден.")
    headers = {"Content-Disposition": f'attachment; filename="ais-reconciliation-{token[:8]}.xlsx"'}
    return StreamingResponse(
        report_xlsx(report),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )

@app.get("/organizations/new", response_class=HTMLResponse)
def new_org(request: Request): return views.TemplateResponse(request, "organization_form.html", {})

@app.post("/organizations/new")
def create_org(request: Request, name: str = Form(""), full_name: str = Form(""), address: str = Form(""), department: str = Form(""), phone: str = Form("")):
    validate_form(OrganizationForm, name=name)
    s = db(); org = create_organization(s, name=name, full_name=full_name, address=address, department=department, phone=phone, audit_actor=audit_actor(request))
    org_id = org.id
    s.close()
    return RedirectResponse(f"/organizations/{org_id}", status_code=303)

@app.post("/organizations/{org_id}")
def edit_organization(request: Request, org_id: int, name: str = Form(""), unp: str = Form(""), full_name: str = Form(""), address: str = Form(""), department: str = Form(""), phone: str = Form("")):
    validate_form(OrganizationForm, name=name, unp=unp)
    s = db(); org = s.get(Organization, org_id)
    if not org: raise HTTPException(404)
    update_organization(s, org, name=name, unp=unp, full_name=full_name, address=address, department=department, phone=phone, audit_actor=audit_actor(request))
    s.close()
    return RedirectResponse(f"/organizations/{org_id}", status_code=303)

@app.get("/organizations/{org_id}", response_class=HTMLResponse)
def organization(
    request: Request,
    org_id: int,
    faculty_id: int | None = None,
    application_id: int | None = None,
    file_error: str = "",
    contract_date_error: str = "",
    contract_date_error_field: str = "",
    contract_start_date: str = "",
    contract_end_date: str = "",
    contract_number: str = "",
):
    s = db(); org = s.get(Organization, org_id)
    if not org: raise HTTPException(404)
    context_faculty = s.get(Faculty, faculty_id) if faculty_id is not None else None
    contracts = organization_contracts(org, context_faculty.id if context_faculty else None)
    for contract in contracts:
        load_attachment_relations(contract)
        for agreement in contract.agreements:
            load_attachment_relations(agreement)
    applications = sorted(
        org.applications,
        key=lambda item: (item.signed_date or date.min, item.id),
        reverse=True,
    )
    for application in applications:
        load_attachment_relations(application)
    focused_application_id = (
        application_id if any(item.id == application_id for item in applications) else None
    )
    document_years = {
        int(year)
        for document in [*org.contracts, *org.applications]
        for item in document.items
        for year in json.loads(item.demand_json).keys()
    }
    years = sorted(document_years | set(range(2026, 2037)))
    response = views.TemplateResponse(request, "organization.html", {
        "org": org,
        "contracts": contracts,
        "applications": applications,
        "focused_application_id": focused_application_id,
        "context_faculty": context_faculty,
        "years": years,
        "all_faculties": s.query(Faculty).order_by(Faculty.name).all(),
        "specialties": s.query(Specialty).order_by(Specialty.code, Specialty.name).all(),
        "file_error": file_error,
        "contract_date_error": contract_date_error,
        "contract_date_error_field": contract_date_error_field,
        "contract_start_date": contract_start_date,
        "contract_end_date": contract_end_date,
        "contract_number": contract_number,
    })
    s.close()
    return response

@app.post("/organizations/{org_id}/contract")
def add_contract(request: Request, org_id: int, faculty: list[str] = Form(default=[]), number: str = Form(""), start_date: str = Form(""), end_date: str = Form(""), allow_duplicate: bool = Form(False)):
    validate_form(ContractForm, number=number, start_date=start_date, end_date=end_date)
    if not faculty:
        raise FormValidationError({"faculty": "Выберите хотя бы один факультет."})
    s = db()
    duplicate = s.query(Contract).filter(Contract.organization_id == org_id, func.lower(Contract.number) == number.strip().lower()).first()
    if duplicate and not allow_duplicate:
        org = s.get(Organization, org_id)
        response = views.TemplateResponse(request, "duplicate_contract.html", {
            "org": org, "duplicate": duplicate, "action": f"/organizations/{org_id}/contract",
            "faculty": faculty, "number": number, "start_date": start_date, "end_date": end_date,
        })
        s.close()
        return response
    try:
        create_contract(s, org_id, faculty, number, end_date, start_date=start_date, audit_actor=audit_actor(request))
    except ValueError as error:
        s.close()
        raise FormValidationError({"contract": str(error)}) from error
    s.close()
    return RedirectResponse(f"/organizations/{org_id}", status_code=303)

@app.post("/contracts/{contract_id}")
def edit_contract(request: Request, contract_id: int, faculty: list[str] = Form(default=[]), number: str = Form(""), start_date: str = Form(""), end_date: str = Form(""), allow_duplicate: bool = Form(False)):
    validate_form(ContractForm, number=number, start_date=start_date, end_date=end_date)
    if not faculty:
        raise FormValidationError({"faculty": "Выберите хотя бы один факультет."})
    s = db(); contract = s.get(Contract, contract_id)
    if not contract: raise HTTPException(404)
    organization_id = contract.organization_id
    duplicate = s.query(Contract).filter(Contract.organization_id == organization_id, Contract.id != contract_id, func.lower(Contract.number) == number.strip().lower()).first()
    if duplicate and not allow_duplicate:
        response = views.TemplateResponse(request, "duplicate_contract.html", {
            "org": contract.organization, "duplicate": duplicate, "action": f"/contracts/{contract_id}",
            "faculty": faculty, "number": number, "start_date": start_date, "end_date": end_date,
        })
        s.close()
        return response
    try:
        update_contract(s, contract, number, start_date, end_date, faculty, audit_actor=audit_actor(request))
    except ValueError as error:
        s.close()
        raise FormValidationError({"contract": str(error)}) from error
    s.close()
    return RedirectResponse(f"/organizations/{organization_id}", status_code=303)

@app.post("/contracts/{contract_id}/status")
def set_contract_status(request: Request, contract_id: int, status: str = Form(...), comment: str = Form("")):
    s = db(); contract = s.get(Contract, contract_id)
    if not contract: raise HTTPException(404)
    try:
        change_contract_status(s, contract, status, request.state.user.role, comment, audit_actor=audit_actor(request))
    except StatusTransitionError as error:
        s.close()
        return JSONResponse({"detail": str(error)}, status_code=400)
    result = {
        "status": contract.status,
        "status_class": status_class(contract.status),
        "transitions": allowed_status_transitions("contract", contract.status, request.state.user.role),
    }
    s.close()
    return result

@app.post("/contracts/{contract_id}/additional-agreements")
def add_additional_agreement(request: Request, contract_id: int, number: str = Form(""), agreement_date: str = Form("")):
    validate_form(AgreementForm, number=number, agreement_date=agreement_date)
    s = db(); contract = s.get(Contract, contract_id)
    if not contract:
        s.close()
        raise HTTPException(404)
    register_additional_agreement(s, contract, number, date.fromisoformat(agreement_date), request.state.user.id, audit_actor=audit_actor(request))
    organization_id = contract.organization_id
    s.close()
    return RedirectResponse(f"/organizations/{organization_id}", status_code=303)

@app.post("/additional-agreements/{agreement_id}/status")
def set_agreement_status(request: Request, agreement_id: int, status: str = Form(...), comment: str = Form("")):
    s = db(); agreement = s.get(AdditionalAgreement, agreement_id)
    if not agreement: raise HTTPException(404)
    try:
        change_agreement_status(s, agreement, status, request.state.user.role, comment, audit_actor=audit_actor(request))
    except StatusTransitionError as error:
        s.close()
        return JSONResponse({"detail": str(error)}, status_code=400)
    result = {
        "status": agreement.status,
        "status_class": status_class(agreement.status),
        "transitions": allowed_status_transitions("additional_agreement", agreement.status, request.state.user.role),
    }
    s.close()
    return result

@app.get("/additional-agreements/{agreement_id}/comparison", response_class=HTMLResponse)
def agreement_comparison(request: Request, agreement_id: int, file_error: str = ""):
    s = db(); agreement = s.get(AdditionalAgreement, agreement_id)
    if not agreement: raise HTTPException(404)
    rows, years = compare_agreement_order(s, agreement)
    load_attachment_relations(agreement)
    response = views.TemplateResponse(request, "agreement_comparison.html", {"agreement": agreement, "rows": rows, "years": years, "file_error": file_error})
    s.close()
    return response


def render_order_history(
    request: Request,
    document_type: str,
    document_id: int,
    revision_id: int | None,
    compare_to: int | None,
):
    session = db()
    history = get_order_history(session, document_type, document_id)
    if not history:
        session.close()
        raise HTTPException(404)
    selected = get_revision(history, revision_id)
    if revision_id is not None and not selected:
        session.close()
        raise HTTPException(404, "Редакция не относится к этому документу")
    rows, years = order_table(selected.order) if selected else ([], [])
    comparison = compare_revisions(session, history, selected.order.id, compare_to) if selected and compare_to else None
    if compare_to is not None and comparison is None:
        session.close()
        raise HTTPException(404, "Редакция для сравнения не относится к этому документу")
    response = views.TemplateResponse(request, "order_history.html", {
        "history": history,
        "selected_revision": selected,
        "order_rows": rows,
        "years": years,
        "comparison": comparison,
    })
    session.close()
    return response


@app.get("/contracts/{contract_id}/order-history", response_class=HTMLResponse)
def contract_order_history(request: Request, contract_id: int, revision_id: int | None = None, compare_to: int | None = None):
    return render_order_history(request, "contract", contract_id, revision_id, compare_to)


@app.get("/additional-agreements/{agreement_id}/order-history", response_class=HTMLResponse)
def agreement_order_history(request: Request, agreement_id: int, revision_id: int | None = None, compare_to: int | None = None):
    return render_order_history(request, "additional_agreement", agreement_id, revision_id, compare_to)


@app.post("/contracts/{contract_id}/items")
async def add_item(contract_id: int, request: Request, specialty: str = Form(""), qualification: str = Form("")):
    form = await request.form()
    validate_order_form(form, specialty)
    s = db(); c = s.get(Contract, contract_id)
    if not c:
        s.close()
        raise HTTPException(404)
    organization_id = c.organization_id
    try:
        save_item(s, contract_id, specialty, qualification, form, user_id=request.state.user.id, catalog_only=True, audit_actor=audit_actor(request))
    except InactiveOrderRevisionError as error:
        s.close()
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        s.close()
        raise FormValidationError({"order_item": str(error)}) from error
    s.close()
    return RedirectResponse(f"/organizations/{organization_id}", status_code=303)

@app.post("/items/{item_id}")
async def edit_item(item_id: int, request: Request, specialty: str = Form(""), qualification: str = Form("")):
    form = await request.form()
    validate_order_form(form, specialty)
    s = db(); item = s.get(OrderItem, item_id)
    if not item:
        s.close()
        raise HTTPException(404)
    organization_id = item.contract.organization_id
    try:
        save_item(s, item.contract_id, specialty, qualification, form, item, request.state.user.id, catalog_only=True, audit_actor=audit_actor(request))
    except InactiveOrderRevisionError as error:
        s.close()
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        s.close()
        raise FormValidationError({"order_item": str(error)}) from error
    s.close()
    return RedirectResponse(f"/organizations/{organization_id}", status_code=303)

@app.post("/items/{item_id}/delete")
def remove_item(request: Request, item_id: int):
    s = db(); item = s.get(OrderItem, item_id)
    if not item: raise HTTPException(404)
    try:
        destination = delete_item(s, item, audit_actor=audit_actor(request))
    except InactiveOrderRevisionError as error:
        s.close()
        raise HTTPException(409, str(error)) from error
    if destination["application_id"]:
        return RedirectResponse(f"/applications/{destination['application_id']}", status_code=303)
    return RedirectResponse(f"/organizations/{destination['organization_id']}", status_code=303)

@app.post("/contracts/{contract_id}/files")
async def add_contract_file(request: Request, contract_id: int, file_kind: str = Form(...), file: UploadFile = File(...)):
    s = db(); c = s.get(Contract, contract_id)
    if not c: raise HTTPException(404)
    destination = f"/organizations/{c.organization_id}"
    try:
        if file_kind not in MANUAL_FILE_KINDS:
            raise AttachmentError("Этот тип файла нельзя загружать вручную.")
        create_attachment(
            s, contract=c, filename=file.filename or "", mime_type=file.content_type or "",
            content=await read_uploaded_file(file), file_kind=file_kind, uploaded_by=request.state.user.id,
            audit_actor=audit_actor(request),
        )
    except AttachmentError as error:
        s.close()
        return attachment_error_redirect(destination, error)
    s.close()
    return RedirectResponse(destination, status_code=303)


@app.post("/additional-agreements/{agreement_id}/files")
async def add_agreement_file(request: Request, agreement_id: int, file_kind: str = Form(...), file: UploadFile = File(...)):
    s = db(); agreement = s.get(AdditionalAgreement, agreement_id)
    if not agreement: raise HTTPException(404)
    destination = f"/additional-agreements/{agreement_id}/comparison"
    try:
        if file_kind not in MANUAL_FILE_KINDS:
            raise AttachmentError("Этот тип файла нельзя загружать вручную.")
        create_attachment(
            s, agreement=agreement, filename=file.filename or "", mime_type=file.content_type or "",
            content=await read_uploaded_file(file), file_kind=file_kind, uploaded_by=request.state.user.id,
            audit_actor=audit_actor(request),
        )
    except AttachmentError as error:
        s.close()
        return attachment_error_redirect(destination, error)
    s.close()
    return RedirectResponse(destination, status_code=303)


@app.get("/attachments/{attachment_id}/download")
def download_attachment(attachment_id: int):
    session = db()
    attachment = get_attachment_metadata(session, attachment_id)
    if not attachment:
        session.close()
        raise HTTPException(404)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(attachment.original_name)}",
        "Content-Length": str(attachment.size_bytes),
        "X-Content-Type-Options": "nosniff",
    }
    mime_type = attachment.mime_type
    session.close()
    return StreamingResponse(stream_attachment(attachment_id), media_type=mime_type, headers=headers)


@app.post("/attachments/{attachment_id}/delete")
def remove_attachment(request: Request, attachment_id: int):
    session = db(); attachment = get_attachment_metadata(session, attachment_id, include_deleted=True)
    if not attachment:
        session.close()
        raise HTTPException(404)
    destination = attachment_destination(attachment)
    try:
        delete_attachment(
            session, attachment, request.state.user.id, request.state.user.role,
            audit_actor=audit_actor(request),
        )
    except AttachmentError as error:
        session.close()
        return attachment_error_redirect(destination, error)
    session.close()
    return RedirectResponse(destination, status_code=303)


@app.post("/attachments/{attachment_id}/restore")
def restore_deleted_attachment(request: Request, attachment_id: int):
    session = db(); attachment = get_attachment_metadata(session, attachment_id, include_deleted=True)
    if not attachment:
        session.close()
        raise HTTPException(404)
    destination = attachment_destination(attachment)
    try:
        restore_attachment(
            session, attachment, request.state.user.id, request.state.user.role,
            audit_actor=audit_actor(request),
        )
    except AttachmentError as error:
        session.close()
        return attachment_error_redirect(destination, error)
    session.close()
    return RedirectResponse(destination, status_code=303)

# The legacy Jinja interface remains fully operational, but HTML/form routes
# are intentionally absent from the React API documentation. New JSON routes
# are added under /api and document their request/response models explicitly.
for route in app.routes:
    if isinstance(route, APIRoute) and not route.path.startswith("/api/"):
        route.include_in_schema = False
