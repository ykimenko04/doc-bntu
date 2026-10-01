from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AppUser, AuditLog, OrderItem, Specialty
from app.services.auth_service import hash_password


def test_admin_can_search_and_edit_specialty_directory(client, session):
    specialty = Specialty(
        code="SPEC-01",
        name="SPEC-01",
        profile=None,
        qualification="Инженер",
    )
    session.add(specialty)
    session.commit()

    page = client.get("/specialties", params={"q": "SPEC-01"})
    assert page.status_code == 200
    assert "Справочник специальностей" in page.text
    assert "SPEC-01" in page.text

    response = client.post(
        f"/specialties/{specialty.id}",
        data={
            "name": "Проектирование машин",
            "profile": "Автомобилестроение",
            "qualification": "Инженер-механик",
        },
    )
    assert response.status_code == 303
    session.expire_all()
    saved = session.get(Specialty, specialty.id)
    assert saved.code == "SPEC-01"
    assert saved.name == "Проектирование машин"
    assert saved.profile == "Автомобилестроение"
    assert saved.qualification == "Инженер-механик"
    assert session.scalar(select(AuditLog).where(
        AuditLog.entity_type == "specialty",
        AuditLog.entity_id == specialty.id,
        AuditLog.action == "UPDATE",
    )) is not None


def test_head_can_view_but_cannot_edit_specialty_directory(session):
    from app.main import app

    specialty = Specialty(code="HEAD-01", name="Название")
    head = AppUser(
        username="specialty-head",
        email="specialty-head@example.com",
        password_hash=hash_password("Head-password-123"),
        full_name="Руководитель",
        role="HEAD",
        must_change_password=False,
    )
    session.add_all([specialty, head])
    session.commit()

    with TestClient(app, follow_redirects=False) as browser:
        browser.post("/login", data={"username": head.username, "password": "Head-password-123"})
        page = browser.get("/specialties")
        assert page.status_code == 200
        assert "HEAD-01" in page.text
        assert "Редактирование доступно администратору" in page.text
        assert 'action="/specialties/' not in page.text
        assert browser.post(
            f"/specialties/{specialty.id}",
            data={"name": "Подмена", "profile": "", "qualification": ""},
        ).status_code == 403

    session.expire_all()
    assert session.get(Specialty, specialty.id).name == "Название"


def test_specialty_picker_autofills_profile_and_qualification_but_allows_override(
    client, session, contract
):
    specialty = Specialty(
        code="AUTO-01",
        name="Автомобильная техника",
        profile="Проектирование автомобилей",
        qualification="Инженер-механик",
    )
    session.add(specialty)
    session.commit()

    page = client.get(f"/organizations/{contract.organization_id}")
    assert page.status_code == 200
    assert 'data-profile="Проектирование автомобилей"' in page.text
    assert 'data-qualification="Инженер-механик"' in page.text

    faculty_id = contract.faculty_links[0].faculty_id
    response = client.post(
        f"/contracts/{contract.id}/items",
        data={
            "faculty_id": faculty_id,
            "specialty": specialty.code,
            "profile": "Профиль из документа",
            "qualification": "Квалификация из документа",
        },
    )
    assert response.status_code == 303
    item = session.scalar(select(OrderItem).where(OrderItem.specialty_id == specialty.id))
    assert item.profile == "Профиль из документа"
    assert item.qualification_value == "Квалификация из документа"
