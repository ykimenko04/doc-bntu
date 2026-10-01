import base64
import hashlib
import hmac
import os
from datetime import datetime, timezone

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import AppUser
from .audit_service import AuditAction, AuditActor, AuditBatch, audit_login_enabled, audited, current_audit_batch


PBKDF2_ITERATIONS = 600_000
USER_ROLES = {"ADMIN", "HEAD"}
EMAIL_ADAPTER = TypeAdapter(EmailStr)


class AccountDisabledError(ValueError):
    pass


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            base64.b64decode(salt),
            int(iterations),
        )
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def has_users(session: Session) -> bool:
    return session.query(AppUser.id).first() is not None


def _normalized_user_values(full_name: str, username: str, role: str) -> tuple[str, str, str]:
    full_name = full_name.strip()
    username = username.strip()
    role = role.strip().upper()
    if not full_name:
        raise ValueError("Укажите ФИО пользователя.")
    if not username:
        raise ValueError("Укажите логин пользователя.")
    if role not in USER_ROLES:
        raise ValueError("Выбрана неизвестная роль.")
    return full_name[:255], username[:100], role


def normalize_email(email: str) -> str:
    try:
        return str(EMAIL_ADAPTER.validate_python(str(email or "").strip())).lower()
    except ValidationError as error:
        raise ValueError("Укажите корректный адрес электронной почты.") from error


@audited
def create_user(
    session: Session,
    full_name: str,
    username: str,
    email: str,
    role: str,
    initial_password: str,
    *,
    force_password_change: bool = True,
) -> AppUser:
    full_name, username, role = _normalized_user_values(full_name, username, role)
    email = normalize_email(email)
    if len(initial_password) < 8:
        raise ValueError("Пароль должен содержать не менее 8 символов.")
    duplicate = session.query(AppUser.id).filter(func.lower(AppUser.username) == username.lower()).first()
    if duplicate:
        raise ValueError("Пользователь с таким логином уже существует.")
    duplicate_email = session.query(AppUser.id).filter(func.lower(AppUser.email) == email).first()
    if duplicate_email:
        raise ValueError("Пользователь с такой электронной почтой уже существует.")
    user = AppUser(
        full_name=full_name,
        username=username,
        email=email,
        role=role,
        password_hash=hash_password(initial_password),
        must_change_password=force_password_change,
        is_active=True,
    )
    session.add(user)
    return user


def create_initial_admin(
    session: Session,
    full_name: str,
    username: str,
    email: str,
    password: str,
    *,
    audit_actor: AuditActor | None = None,
) -> AppUser:
    if has_users(session):
        raise ValueError("Первичная настройка уже выполнена.")
    return create_user(
        session,
        full_name,
        username,
        email,
        "ADMIN",
        password,
        force_password_change=False,
        audit_actor=audit_actor,
    )


@audited
def update_user(session: Session, user: AppUser, full_name: str, role: str) -> AppUser:
    full_name, _username, role = _normalized_user_values(full_name, user.username, role)
    if user.role == "ADMIN" and role != "ADMIN":
        admins = session.query(AppUser.id).filter(AppUser.role == "ADMIN").count()
        if admins <= 1:
            raise ValueError("Нельзя изменить роль единственного администратора.")
    user.full_name = full_name
    user.role = role
    return user


@audited
def update_email(session: Session, user: AppUser, email: str) -> AppUser:
    normalized = normalize_email(email)
    duplicate = session.query(AppUser.id).filter(
        func.lower(AppUser.email) == normalized,
        AppUser.id != user.id,
    ).first()
    if duplicate:
        raise ValueError("Пользователь с такой электронной почтой уже существует.")
    user.email = normalized
    return user


@audited
def set_user_active(
    session: Session,
    user: AppUser,
    is_active: bool,
    *,
    actor_user_id: int | None = None,
) -> AppUser:
    if user.is_active == is_active:
        return user
    if not is_active:
        if actor_user_id == user.id:
            raise ValueError("Нельзя деактивировать собственную учётную запись.")
        if user.role == "ADMIN":
            active_admins = session.query(AppUser.id).filter(
                AppUser.role == "ADMIN",
                AppUser.is_active.is_(True),
            ).count()
            if active_admins <= 1:
                raise ValueError("Нельзя деактивировать последнего активного администратора.")
    user.is_active = is_active
    return user


def authenticate(session: Session, username: str, password: str, ip_address: str | None = None) -> AppUser | None:
    user = session.query(AppUser).filter(func.lower(AppUser.username) == username.strip().lower()).one_or_none()
    if not user or not verify_password(password, user.password_hash):
        return None
    if not user.is_active:
        raise AccountDisabledError("Учётная запись отключена. Обратитесь к администратору")
    login_at = datetime.now(timezone.utc)
    with AuditBatch(session, AuditActor(user.id, ip_address)) as audit:
        audit.suppress(user)
        user.last_login_at = login_at
        if audit_login_enabled(session):
            audit.record(
                user,
                AuditAction.LOGIN,
                old={},
                new={"last_login_at": login_at.isoformat()},
                label=f"Пользователь {user.full_name or user.username}",
            )
    return user


@audited
def change_password(session: Session, user: AppUser, new_password: str) -> AppUser:
    if len(new_password) < 8:
        raise ValueError("Пароль должен содержать не менее 8 символов.")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    batch = current_audit_batch(session)
    batch.record(
        user,
        AuditAction.UPDATE,
        old={},
        new={},
        comment="Пароль изменён",
    )
    return user


@audited
def reset_password(session: Session, user: AppUser, new_password: str) -> AppUser:
    """Set a temporary password without exposing its value to the audit log."""
    if len(new_password) < 8:
        raise ValueError("Пароль должен содержать не менее 8 символов.")
    user.password_hash = hash_password(new_password)
    user.must_change_password = True
    current_audit_batch(session).record(
        user,
        AuditAction.UPDATE,
        old={},
        new={"must_change_password": True},
        comment="Администратор сбросил пароль",
    )
    return user
