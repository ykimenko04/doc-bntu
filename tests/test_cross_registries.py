from datetime import date, timedelta

from sqlalchemy import select

from app.main import app
from app.models import (
    Application,
    Contract,
    ContractFaculty,
    Document,
    DocumentAttachment,
    Faculty,
)
from app.services.application_service import create_application, save_application_item
from app.services.document_registry_service import PAGE_SIZE, application_registry, contract_registry
from app.services.organization_service import save_item
from app.services.status_service import URGENCY_DUE_30, URGENCY_DUE_90, URGENCY_LATER


def _contract(session, organization, faculty, number, end_date, status="Активен"):
    contract = Contract(
        organization_id=organization.id,
        number=number,
        start_date=date(2026, 1, 15),
        end_date=end_date,
        status=status,
    )
    session.add(contract)
    session.flush()
    session.add(ContractFaculty(contract_id=contract.id, faculty_id=faculty.id))
    session.commit()
    return contract


def _scan(session, organization_id, user_id, *, contract_id=None, application_id=None):
    document = Document(
        organization_id=organization_id,
        contract_id=contract_id,
        application_id=application_id,
        type="contract" if contract_id else "application",
        status="SIGNED",
    )
    session.add(document)
    session.flush()
    session.add(DocumentAttachment(
        document_id=document.id,
        file_kind="signed_scan",
        original_name="scan.pdf",
        mime_type="application/pdf",
        size_bytes=4,
        content=b"scan",
        uploaded_by=user_id,
    ))
    session.commit()


def test_contract_registry_combines_search_filters_urgency_and_scan(
    session, organization, user, actor, client
):
    today = date.today()
    faculty = Faculty(name="Факультет реестра договоров")
    other = Faculty(name="Другой факультет")
    session.add_all([faculty, other])
    session.commit()
    target = _contract(session, organization, faculty, "РЕЕСТР-2026/01", today + timedelta(days=20))
    _contract(session, organization, faculty, "ЗАКРЫТ-2026/02", today + timedelta(days=20), "Закрыт")
    _contract(session, organization, other, "ДАЛЬНИЙ-2027/01", today + timedelta(days=120))
    save_item(
        session,
        target.id,
        "REG-01",
        "Инженер",
        {"faculty_id": str(faculty.id), "demand_2027": "2"},
        user_id=user.id,
        audit_actor=actor,
    )
    _scan(session, organization.id, user.id, contract_id=target.id)

    page, faculties, years, counts, urgency = contract_registry(
        session,
        query_text="РЕЕСТР-2026",
        faculty=faculty.name,
        status="Активен",
        end_year=str(target.end_date.year),
        urgency=URGENCY_DUE_30,
    )
    assert page.total == 1
    assert page.rows[0].contract.id == target.id
    assert page.rows[0].specialty_count == 1
    assert page.rows[0].contract.has_signed_scan is True
    assert counts[URGENCY_DUE_30] == 1
    assert counts[URGENCY_LATER] == 0
    assert urgency == URGENCY_DUE_30
    assert faculty.name in faculties
    assert target.end_date.year in {int(year) for year in years}

    response = client.get("/contracts", params={
        "q": "РЕЕСТР-2026",
        "faculty": faculty.name,
        "status": "Активен",
        "end_year": target.end_date.year,
        "urgency": URGENCY_DUE_30,
    })
    assert response.status_code == 200
    assert target.number in response.text
    assert "ЗАКРЫТ-2026/02" not in response.text
    assert "ДАЛЬНИЙ-2027/01" not in response.text
    assert f"/organizations/{organization.id}#contract-{target.id}" in response.text
    assert "Есть подписанный скан" in response.text
    assert "Документы" not in response.text
    assert 'data-optional="true"' in response.text
    assert "Все факультеты" in response.text

    without_faculty = client.get("/contracts", params={
        "q": target.number,
        "status": "Активен",
        "end_year": target.end_date.year,
    })
    assert without_faculty.status_code == 200
    assert target.number in without_faculty.text

    toggle = client.get("/contracts", params={
        "q": target.number,
        "status": "Активен",
        "end_year": target.end_date.year,
        "urgency_choice": URGENCY_DUE_30,
    }, follow_redirects=False)
    assert toggle.status_code == 303
    assert "urgency=due_30" in toggle.headers["location"]
    assert "faculty=" not in toggle.headers["location"]
    active = client.get(toggle.headers["location"])
    assert 'urgency-due_30 active' in active.text


def test_application_registry_combines_search_faculty_status_and_scan(
    session, organization, user, actor, client
):
    target = create_application(
        session,
        organization.id,
        ["Факультет заявок"],
        "ЗАЯВКА-01",
        "2026-09-24",
        "2027-12-31",
        user.id,
        audit_actor=actor,
    )
    closed = create_application(
        session,
        organization.id,
        ["Факультет заявок"],
        "ЗАЯВКА-02",
        "",
        "",
        user.id,
        audit_actor=actor,
    )
    closed.status = "Закрыт"
    session.commit()
    _scan(session, organization.id, user.id, application_id=target.id)

    page, faculties, _years, _counts, _urgency = application_registry(
        session,
        query_text="ЗАЯВКА-01",
        faculty="Факультет заявок",
        status="Заявка",
    )
    assert page.total == 1
    assert page.rows[0].id == target.id
    assert page.rows[0].has_signed_scan is True
    assert "Факультет заявок" in faculties

    response = client.get("/applications", params={
        "q": organization.name,
        "faculty": "Факультет заявок",
        "status": "Заявка",
    })
    assert response.status_code == 200
    assert target.number in response.text
    assert closed.number not in response.text
    target_url = f"/organizations/{organization.id}?application_id={target.id}#application-{target.id}"
    assert target_url in response.text
    assert "Есть подписанный скан" in response.text
    assert "Дата подписания" in response.text
    assert "Действует до" in response.text
    assert "31.12.2027" in response.text
    assert "Дата получения" not in response.text
    assert "24.09.2026" in response.text

    card = client.get(f"/applications/{target.id}")
    assert card.status_code == 200
    assert 'href="/applications"' in card.text
    assert "Действует до" in card.text
    assert 'value="2027-12-31"' in card.text
    assert "Получена:" not in card.text


def test_application_registry_filters_year_and_all_urgency_buckets(
    session, organization, user, actor, client
):
    today = date.today()
    values = {
        "OVERDUE": today - timedelta(days=5),
        "SOON": today + timedelta(days=20),
        "MIDDLE": today + timedelta(days=60),
        "FAR": today + timedelta(days=120),
    }
    created = {
        number: create_application(
            session, organization.id, ["Факультет сроков"], number, "",
            end_date.isoformat(), user.id, audit_actor=actor,
        )
        for number, end_date in values.items()
    }
    no_date = create_application(
        session, organization.id, ["Факультет сроков"], "NO-DATE", "", "", user.id,
        audit_actor=actor,
    )

    due, _faculties, years, counts, selected = application_registry(session, urgency=URGENCY_DUE_30)
    assert [row.id for row in due.rows[:2]] == [created["OVERDUE"].id, created["SOON"].id]
    assert no_date.id not in {row.id for row in due.rows}
    assert counts == {URGENCY_DUE_30: 2, URGENCY_DUE_90: 1, URGENCY_LATER: 1}
    assert selected == URGENCY_DUE_30
    assert {int(year) for year in years} >= {value.year for value in values.values()}

    middle, *_ = application_registry(session, urgency=URGENCY_DUE_90)
    far, *_ = application_registry(session, urgency=URGENCY_LATER)
    by_year, *_ = application_registry(session, end_year=str(values["FAR"].year), query_text="FAR")
    assert [row.id for row in middle.rows] == [created["MIDDLE"].id]
    assert [row.id for row in far.rows] == [created["FAR"].id]
    assert [row.id for row in by_year.rows] == [created["FAR"].id]

    page = client.get("/applications", params={"urgency": URGENCY_DUE_30})
    assert page.status_code == 200
    assert "Любой год окончания" in page.text
    assert "31–90 дней" in page.text and "&gt; 90 дней" in page.text
    assert "просрочен" in page.text
    assert 'data-optional="true"' in page.text
    assert 'urgency-due_30 active' in page.text

    without_faculty = client.get("/applications", params={
        "q": "FAR",
        "status": "Заявка",
        "end_year": values["FAR"].year,
        "urgency": URGENCY_LATER,
    })
    assert without_faculty.status_code == 200
    assert created["FAR"].number in without_faculty.text


def test_organization_card_shows_and_expands_selected_application(
    session, organization, user, actor, client
):
    first = create_application(
        session, organization.id, ["Факультет заявок"], "ЗАЯВКА-КАРТОЧКА-1",
        "2026-09-20", "2027-09-20", user.id, audit_actor=actor,
    )
    second = create_application(
        session, organization.id, ["Другой факультет"], "ЗАЯВКА-КАРТОЧКА-2",
        "2026-09-25", "2028-09-25", user.id, audit_actor=actor,
    )
    save_application_item(
        session, second, "APP-CARD-01", "Инженер",
        {"profile": "Профилизация заявки", "demand_2027": "4"},
        audit_actor=actor,
    )
    _scan(session, organization.id, user.id, application_id=second.id)

    page = client.get(
        f"/organizations/{organization.id}", params={"application_id": second.id}
    )

    assert page.status_code == 200
    assert "Договоры" in page.text and "Заявки" in page.text
    assert first.number in page.text and second.number in page.text
    assert "25.09.2026" in page.text and "25.09.2028" in page.text
    assert "Другой факультет" in page.text
    assert "Есть подписанный скан" in page.text
    assert f'id="application-{second.id}" class="application-card" open' in page.text
    assert f'id="application-{first.id}" class="application-card" open' not in page.text
    assert "Профилизация заявки" in page.text
    assert f'/applications/{second.id}/order-history' not in page.text
    assert f'/applications/{second.id}/files' in page.text


def test_organization_without_applications_has_explicit_empty_state(
    organization, client
):
    page = client.get(f"/organizations/{organization.id}")

    assert page.status_code == 200
    assert "Заявки" in page.text
    assert "Заявок нет" in page.text


def test_application_is_created_without_received_date(session, organization, client):
    faculty = Faculty(name="Факультет срока заявки")
    session.add(faculty)
    session.commit()

    form = client.get(f"/organizations/{organization.id}/applications/new")
    assert form.status_code == 200
    assert "Дата получения" not in form.text
    assert "Действует до" in form.text

    response = client.post(
        f"/organizations/{organization.id}/applications",
        data={
            "faculty": faculty.name,
            "number": "З-СРОК",
            "signed_date": "2026-09-29",
            "date_end": "2027-09-29",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    application = session.scalar(select(Application).where(Application.number == "З-СРОК"))
    assert application.received_date is None
    assert application.date_end == date(2027, 9, 29)


def test_applications_registry_has_one_get_route():
    routes = [
        route for route in app.routes
        if getattr(route, "path", None) == "/applications" and "GET" in getattr(route, "methods", set())
    ]
    assert len(routes) == 1


def test_contract_registry_paginates_by_fifty(session, organization):
    faculty = Faculty(name="Факультет пагинации")
    session.add(faculty)
    session.commit()
    for index in range(PAGE_SIZE + 1):
        _contract(
            session,
            organization,
            faculty,
            f"PAGE-{index:02d}",
            date(2030, 1, 1) + timedelta(days=index),
        )

    first, *_ = contract_registry(session, faculty=faculty.name, page=1)
    second, *_ = contract_registry(session, faculty=faculty.name, page=2)

    assert first.total == PAGE_SIZE + 1
    assert first.pages == 2
    assert len(first.rows) == PAGE_SIZE
    assert len(second.rows) == 1
    assert second.page == 2
