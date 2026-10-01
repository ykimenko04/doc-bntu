from sqlalchemy import select
from fastapi.testclient import TestClient

from app.models import AppUser, AuditLog
from app.services.audit_registry_service import get_audit_registry
from app.services.auth_service import create_user, hash_password, set_user_active, verify_password


def test_empty_database_allows_one_time_admin_setup(session):
    from app.main import app

    with TestClient(app, follow_redirects=False) as browser:
        assert browser.get("/").headers["location"] == "/setup"
        response = browser.post("/setup", data={
            "full_name": "Первый администратор",
            "username": "first-admin",
            "email": "first-admin@example.com",
            "password": "First-password-123",
            "password_repeat": "First-password-123",
        })

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    user = session.scalar(select(AppUser).where(AppUser.username == "first-admin"))
    assert user.role == "ADMIN"
    assert user.email == "first-admin@example.com"
    assert user.must_change_password is False
    assert verify_password("First-password-123", user.password_hash)
    audit = session.scalar(select(AuditLog).where(AuditLog.entity_type == "app_user"))
    assert audit.action == "CREATE"
    assert "First-password-123" not in str(audit.diff)
    assert user.password_hash not in str(audit.diff)


def test_admin_creates_user_and_first_login_requires_password_change(client, session):
    response = client.post("/users/new", data={
        "full_name": "Руководитель отдела",
        "username": "department-head",
        "email": "department-head@example.com",
        "role": "HEAD",
        "initial_password": "Initial-password-123",
        "password_repeat": "Initial-password-123",
    })
    assert response.status_code == 303
    user = session.scalar(select(AppUser).where(AppUser.username == "department-head"))
    assert user.must_change_password is True
    assert user.email == "department-head@example.com"
    assert session.scalar(select(AuditLog).where(
        AuditLog.entity_type == "app_user", AuditLog.entity_id == user.id, AuditLog.action == "CREATE"
    )) is not None

    client.get("/logout")
    login = client.post("/login", data={"username": "department-head", "password": "Initial-password-123"})
    assert login.status_code == 303
    assert login.headers["location"] == "/change-password"
    assert client.get("/").headers["location"] == "/change-password"

    changed = client.post("/change-password", data={
        "current_password": "Initial-password-123",
        "new_password": "Permanent-password-456",
        "password_repeat": "Permanent-password-456",
    })
    assert changed.status_code == 303
    session.expire_all()
    user = session.get(AppUser, user.id)
    assert user.must_change_password is False
    assert not verify_password("Initial-password-123", user.password_hash)
    assert verify_password("Permanent-password-456", user.password_hash)
    event = session.scalars(select(AuditLog).where(
        AuditLog.entity_type == "app_user", AuditLog.entity_id == user.id,
        AuditLog.action == "UPDATE", AuditLog.comment == "Пароль изменён",
    )).one()
    assert event.diff == {"old": {}, "new": {}}


def test_user_email_is_required_valid_and_unique(client):
    missing = client.post("/users/new", data={
        "full_name": "Без адреса", "username": "no-email", "role": "HEAD",
        "initial_password": "Initial-password-123", "password_repeat": "Initial-password-123",
    })
    assert missing.status_code == 422
    assert "корректный адрес электронной почты" in missing.text

    invalid = client.post("/users/new", data={
        "full_name": "Плохой адрес", "username": "bad-email", "email": "wrong", "role": "HEAD",
        "initial_password": "Initial-password-123", "password_repeat": "Initial-password-123",
    })
    assert invalid.status_code == 422

    duplicate = client.post("/users/new", data={
        "full_name": "Дубликат", "username": "duplicate-email", "email": "test-admin@example.com", "role": "HEAD",
        "initial_password": "Initial-password-123", "password_repeat": "Initial-password-123",
    })
    assert duplicate.status_code == 422
    assert "уже существует" in duplicate.text


def test_head_cannot_manage_users(session):
    from app.main import app

    head = AppUser(
        username="head-user",
        email="head-user@example.com",
        password_hash=hash_password("Head-password-123"),
        full_name="Руководитель",
        role="HEAD",
        must_change_password=False,
    )
    session.add(head)
    session.commit()
    with TestClient(app, follow_redirects=False) as browser:
        assert browser.post("/login", data={
            "username": "head-user", "password": "Head-password-123",
        }).status_code == 303
        assert browser.get("/users").status_code == 403


def test_admin_can_edit_name_and_role_but_not_demote_last_admin(client, session):
    admin = session.scalar(select(AppUser).where(AppUser.username == "test-admin"))
    blocked = client.post(f"/users/{admin.id}/edit", data={
        "full_name": "Администратор тестов", "role": "HEAD",
    })
    assert blocked.status_code == 400
    session.expire_all()
    assert session.get(AppUser, admin.id).role == "ADMIN"

    second = create_user(
        session, "Второй администратор", "second-admin", "second-admin@example.com", "ADMIN", "Second-password-123",
    )
    changed = client.post(f"/users/{second.id}/edit", data={
        "full_name": "Обновлённое имя", "role": "HEAD",
    })
    assert changed.status_code == 303
    session.expire_all()
    second = session.get(AppUser, second.id)
    assert second.full_name == "Обновлённое имя"
    assert second.role == "HEAD"


def test_admin_deactivates_and_reactivates_user_with_audit_and_session_revocation(client, session):
    from app.main import app

    head = create_user(
        session, "Отключаемый сотрудник", "disabled-head", "disabled-head@example.com",
        "HEAD", "Head-password-123", force_password_change=False,
    )
    session.add(AuditLog(
        user_id=head.id,
        action="UPDATE",
        entity_type="app_user",
        entity_id=head.id,
        entity_label="Пользователь Отключаемый сотрудник",
        diff={"old": {}, "new": {"full_name": "Отключаемый сотрудник"}},
    ))
    session.commit()
    with TestClient(app, follow_redirects=False) as head_browser:
        assert head_browser.post("/login", data={
            "username": head.username, "password": "Head-password-123",
        }).status_code == 303

        disabled = client.post(f"/users/{head.id}/status", data={"is_active": "false"})
        assert disabled.status_code == 303
        session.expire_all()
        assert session.get(AppUser, head.id).is_active is False

        revoked = head_browser.get("/")
        assert revoked.status_code == 303
        assert revoked.headers["location"] == "/login?disabled=1"
        login_page = head_browser.get(revoked.headers["location"])
        assert "Учётная запись отключена. Обратитесь к администратору" in login_page.text
        refused = head_browser.post("/login", data={
            "username": head.username, "password": "Head-password-123",
        })
        assert refused.status_code == 401
        assert "Учётная запись отключена. Обратитесь к администратору" in refused.text

    registry = client.get("/users")
    assert "отключён" in registry.text
    assert "user-disabled" in registry.text
    event = session.scalars(select(AuditLog).where(
        AuditLog.entity_type == "app_user",
        AuditLog.entity_id == head.id,
        AuditLog.action == "UPDATE",
    ).order_by(AuditLog.id.desc())).first()
    assert event.diff == {"old": {"is_active": True}, "new": {"is_active": False}}
    assert event.user_id == session.scalar(select(AppUser.id).where(AppUser.username == "test-admin"))
    old_rows = get_audit_registry(session, user=str(head.id))
    assert old_rows.rows[0].employee == "Отключаемый сотрудник"

    enabled = client.patch(f"/api/users/{head.id}/status", json={"is_active": True})
    assert enabled.status_code == 200
    assert enabled.json()["is_active"] is True
    session.expire_all()
    assert session.get(AppUser, head.id).is_active is True
    with TestClient(app, follow_redirects=False) as browser:
        assert browser.post("/login", data={
            "username": head.username, "password": "Head-password-123",
        }).status_code == 303


def test_user_deactivation_protects_self_and_last_active_admin(client, session):
    admin = session.scalar(select(AppUser).where(AppUser.username == "test-admin"))
    self_blocked = client.post(f"/users/{admin.id}/status", data={"is_active": "false"})
    assert self_blocked.status_code == 400
    assert "собственную учётную запись" in self_blocked.text

    try:
        set_user_active(session, admin, False, actor_user_id=999)
    except ValueError as error:
        assert "последнего активного администратора" in str(error)
    else:
        raise AssertionError("Деактивация последнего администратора должна быть запрещена")
