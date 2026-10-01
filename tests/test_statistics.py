from datetime import date, datetime, timedelta, timezone

from app.models import (
    AdditionalAgreement,
    Application,
    ApplicationFaculty,
    Contract,
    ContractFaculty,
    Faculty,
    Order,
    OrderItem,
    Organization,
    Specialty,
)
from app.services.document_registry_service import application_registry
from app.services.organization_service import registry
from app.services.statistics_service import registry_statistics


def _organization(number, created_at):
    return Organization(
        unp=f"20000000{number}",
        short_name=f"Организация {number}",
        full_name=f"Организация {number}",
        created_at=created_at,
    )


def _contract(session, organization, faculty, specialty, number, status, end_date):
    contract = Contract(
        organization=organization,
        number=number,
        start_date=date(2026, 1, 1),
        end_date=end_date,
        status=status,
    )
    contract.faculty_links.append(ContractFaculty(faculty=faculty))
    order = Order(organization_id=organization.id, contract=contract, is_current=True, revision=1)
    order.items.append(OrderItem(faculty=faculty, specialty_ref=specialty))
    session.add(contract)
    return contract


def _application(session, organization, faculty, specialty, number, status, date_end, created_at):
    application = Application(
        organization=organization,
        number=number,
        signed_date=date(2026, 1, 1),
        date_end=date_end,
        status=status,
        created_at=created_at,
    )
    application.faculty_links.append(ApplicationFaculty(faculty=faculty))
    order = Order(organization_id=organization.id, application=application, is_current=True, revision=1)
    order.items.append(OrderItem(faculty=faculty, specialty_ref=specialty))
    session.add(application)
    return application


def test_statistics_counts_unique_documents_and_respects_faculty_context(session, client):
    today = date(2026, 9, 29)
    now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
    recent = now - timedelta(days=5)
    old = now - timedelta(days=90)
    faculty_a = Faculty(name="Факультет А")
    faculty_b = Faculty(name="Факультет Б")
    specialty = Specialty(code="STAT-01", name="STAT-01")
    organizations = [_organization(index, recent if index in {1, 4} else old) for index in range(1, 6)]
    session.add_all([faculty_a, faculty_b, specialty, *organizations])
    session.flush()

    _contract(session, organizations[0], faculty_a, specialty, "Д-А-1", "Активен", today + timedelta(days=10))
    _application(session, organizations[1], faculty_a, specialty, "З-А-1", "Заявка", today - timedelta(days=1), recent)
    _contract(session, organizations[2], faculty_a, specialty, "Д-А-2", "Закрыт", today + timedelta(days=100))
    _application(session, organizations[2], faculty_a, specialty, "З-А-2", "Закрыт", today + timedelta(days=100), old)
    _contract(session, organizations[4], faculty_b, specialty, "Д-Б-1", "Активен", today + timedelta(days=60))
    session.commit()

    overall = registry_statistics(session, today=today, now=now)
    assert (overall.total_organizations, overall.organizations_month) == (5, 2)
    assert (overall.active_contracts, overall.total_contracts) == (2, 3)
    assert overall.organizations_with_contracts == 3
    assert (overall.organizations_with_applications, overall.organizations_with_applications_month) == (2, 1)
    assert (overall.active_applications, overall.total_applications) == (1, 2)
    assert overall.total_active_documents == 3
    assert (overall.attention_total, overall.attention_contracts, overall.attention_applications) == (2, 1, 1)

    filtered = registry_statistics(session, faculty_a.id, today=today, now=now)
    contract_rows, *_ = registry(session, faculty=faculty_a.name)
    application_page, *_ = application_registry(session, faculty=faculty_a.name)
    assert filtered.total_organizations == 3
    assert filtered.total_contracts == len(contract_rows) == 2
    assert filtered.total_applications == application_page.total == 2

    page = client.get("/", params={"faculty": faculty_a.name})
    assert page.status_code == 200
    assert "Всего организаций" in page.text
    assert "Требуют внимания" in page.text
    assert "status=Активен&urgency=due_30" in page.text


def test_application_attention_filter_includes_overdue_and_due_dates_only(session):
    today = date.today()
    faculty = Faculty(name="Факультет срочности")
    specialty = Specialty(code="DUE-01", name="DUE-01")
    organization = _organization(6, datetime.now(timezone.utc))
    session.add_all([faculty, specialty, organization])
    session.flush()
    _application(session, organization, faculty, specialty, "OVERDUE", "Заявка", today - timedelta(days=2), datetime.now(timezone.utc))
    _application(session, organization, faculty, specialty, "DUE", "Заявка", today + timedelta(days=30), datetime.now(timezone.utc))
    _application(session, organization, faculty, specialty, "LATER", "Заявка", today + timedelta(days=31), datetime.now(timezone.utc))
    _application(session, organization, faculty, specialty, "NO-DATE", "Заявка", None, datetime.now(timezone.utc))
    session.commit()

    page, *_ = application_registry(session, urgency="due_30")
    assert {row.number for row in page.rows} == {"OVERDUE", "DUE"}


def test_active_contract_statistics_include_active_agreements(session, client):
    today = date.today()
    faculty = Faculty(name="Факультет действующих д.с.")
    specialty = Specialty(code="AGREEMENT-STAT", name="AGREEMENT-STAT")
    organizations = [_organization(index, datetime.now(timezone.utc)) for index in range(7, 10)]
    session.add_all([faculty, specialty, *organizations])
    session.flush()

    mtz = _contract(session, organizations[0], faculty, specialty, "221-АТФ/280", "Закрыт", today - timedelta(days=20))
    maz = _contract(session, organizations[1], faculty, specialty, "535/6596", "Активен", today + timedelta(days=20))
    closed = _contract(session, organizations[2], faculty, specialty, "CLOSED", "Закрыт", today - timedelta(days=20))
    mtz.agreements.append(AdditionalAgreement(number="1", date=today, status="Активен"))
    maz.agreements.append(AdditionalAgreement(number="1", date=today, status="Активен"))
    session.commit()

    statistics = registry_statistics(session, today=today)
    assert (statistics.active_contracts, statistics.total_contracts) == (2, 3)
    assert statistics.organizations_with_contracts == 3
    assert statistics.total_active_documents == 2
    assert statistics.attention_contracts == 0

    registry_page = client.get("/contracts")
    organization_card = client.get(f"/organizations/{mtz.organization_id}")
    note = f"действует д.с. №1 от {today.strftime('%d.%m.%Y')}"
    assert note in registry_page.text
    assert note in organization_card.text
