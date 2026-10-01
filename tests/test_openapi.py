from fastapi.testclient import TestClient

from app.main import app
from app.models import AppUser
from app.services.auth_service import hash_password


EXPECTED_TAGS = {
    "auth",
    "organizations",
    "contracts",
    "applications",
    "orders",
    "audit",
    "users",
    "settings",
    "import-export",
    "specialties",
}

EXPECTED_OPERATIONS = {
    ("post", "/api/auth/login"), ("post", "/api/auth/logout"),
    ("get", "/api/auth/me"), ("put", "/api/auth/email"),
    ("post", "/api/auth/change-password"),
    ("get", "/api/faculties"), ("get", "/api/specialties"),
    ("put", "/api/specialties/{specialty_id}"),
    ("get", "/api/organizations"), ("get", "/api/organizations/{organization_id}"),
    ("get", "/api/statistics"),
    ("get", "/api/contracts"), ("post", "/api/contracts"),
    ("get", "/api/contracts/{contract_id}"), ("put", "/api/contracts/{contract_id}"),
    ("post", "/api/contracts/{contract_id}/status"),
    ("post", "/api/contracts/{contract_id}/order-items"),
    ("post", "/api/contracts/{contract_id}/additional-agreements"),
    ("get", "/api/contracts/{contract_id}/order-history"),
    ("get", "/api/contracts/{contract_id}/order-comparison"),
    ("post", "/api/additional-agreements/{agreement_id}/activate"),
    ("post", "/api/additional-agreements/{agreement_id}/status"),
    ("get", "/api/additional-agreements/{agreement_id}/order-history"),
    ("get", "/api/applications"), ("post", "/api/applications"),
    ("get", "/api/applications/{application_id}"), ("put", "/api/applications/{application_id}"),
    ("post", "/api/applications/{application_id}/status"),
    ("get", "/api/orders/{order_id}"), ("post", "/api/orders/{order_id}/items"),
    ("put", "/api/order-items/{item_id}"), ("delete", "/api/order-items/{item_id}"),
    ("get", "/api/audit"), ("get", "/api/audit/metadata"),
    ("get", "/api/users"), ("post", "/api/users"),
    ("put", "/api/users/{user_id}"), ("post", "/api/users/{user_id}/reset-password"),
    ("patch", "/api/users/{user_id}/status"),
    ("get", "/api/settings/profile"),
    ("post", "/api/import"), ("get", "/api/imports"),
    ("post", "/api/reconciliation"), ("get", "/api/reconciliation/{token}/export"),
    ("get", "/api/export/contracts"), ("get", "/api/export/applications"),
}


def test_openapi_contains_only_documented_json_routes_and_cookie_security():
    with TestClient(app) as browser:
        response = browser.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert {tag["name"] for tag in schema["tags"]} == EXPECTED_TAGS
    operations = {
        (method, path)
        for path, path_item in schema["paths"].items()
        for method in path_item
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert operations == EXPECTED_OPERATIONS
    assert schema["components"]["securitySchemes"]["cookieAuth"] == {
        "type": "apiKey",
        "description": (
            "Сессионная cookie FastAPI. В Swagger сначала выполните POST /api/auth/login: "
            "браузер сохранит HttpOnly cookie и отправит её в следующих запросах автоматически."
        ),
        "in": "cookie",
        "name": "session",
    }
    assert schema["paths"]["/api/auth/me"]["get"]["security"] == [{"cookieAuth": []}]
    assert schema["paths"]["/api/auth/me"]["get"]["tags"] == ["auth", "settings"]
    assert schema["paths"]["/api/auth/change-password"]["post"]["tags"] == ["auth", "settings"]
    assert "вкладки «Профиль»" in schema["paths"]["/api/auth/me"]["get"]["description"]
    assert "Поле повторного пароля" in schema["paths"]["/api/auth/change-password"]["post"]["description"]
    user_schema = schema["components"]["schemas"]["AuthenticatedUserResponse"]
    assert user_schema["properties"]["role"]["enum"] == ["ADMIN", "HEAD"]
    password_schema = schema["components"]["schemas"]["ChangePasswordRequest"]
    assert password_schema["properties"]["current_password"]["writeOnly"] is True
    assert password_schema["properties"]["new_password"]["minLength"] == 8
    assert "/api/health" not in schema["paths"]
    assert "/login" not in schema["paths"]


def test_every_documented_operation_has_summary_tags_security_and_response_schema():
    schema = app.openapi()
    for method, path in EXPECTED_OPERATIONS:
        operation = schema["paths"][path][method]
        assert operation.get("summary"), f"Нет summary: {method.upper()} {path}"
        assert operation.get("tags"), f"Нет tags: {method.upper()} {path}"
        if path != "/api/auth/login":
            assert operation.get("security") == [{"cookieAuth": []}], f"Нет cookieAuth: {method.upper()} {path}"
            assert "401" in operation["responses"]
        success_code = "204" if (method, path) in {
            ("post", "/api/auth/logout"),
            ("post", "/api/auth/change-password"),
            ("delete", "/api/order-items/{item_id}"),
            ("post", "/api/users/{user_id}/reset-password"),
        } else "201" if (method, path) in {
            ("post", "/api/contracts"),
            ("post", "/api/contracts/{contract_id}/additional-agreements"),
            ("post", "/api/applications"),
            ("post", "/api/orders/{order_id}/items"),
            ("post", "/api/contracts/{contract_id}/order-items"),
            ("post", "/api/users"),
        } else "200"
        assert success_code in operation["responses"]
        if success_code != "204":
            assert operation["responses"][success_code].get("content"), f"Нет схемы ответа: {method.upper()} {path}"


def test_openapi_uses_multi_faculty_email_and_string_year_contracts():
    schema = app.openapi()
    contract_parameters = schema["paths"]["/api/contracts"]["get"]["parameters"]
    faculty_ids = next(item for item in contract_parameters if item["name"] == "faculty_ids")
    assert faculty_ids["schema"]["type"] == "array"
    assert schema["components"]["schemas"]["UserCreate"]["required"] == [
        "full_name", "username", "email", "initial_password"
    ]
    current_user = schema["components"]["schemas"]["AuthenticatedUserResponse"]["properties"]
    assert "email" in current_user and "need_email" in current_user
    years = schema["components"]["schemas"]["OrderItemResponse"]["properties"]["years"]
    assert years["additionalProperties"]["type"] == "integer"
    assert "2026" in years["examples"][0]


def test_openapi_nullable_fields_filter_enums_and_deprecated_profile_alias():
    schema = app.openapi()
    components = schema["components"]["schemas"]
    qualification = components["OrderItemResponse"]["properties"]["qualification"]
    assert {item.get("type") for item in qualification["anyOf"]} == {"string", "null"}
    assert components["SpecialtyResponse"]["properties"]["name"]["type"] == "string"
    assert components["SpecialtyResponse"]["properties"]["name"]["examples"] == ["1-37 01 03"]

    def parameter(path, name):
        return next(item for item in schema["paths"][path]["get"]["parameters"] if item["name"] == name)

    assert parameter("/api/contracts", "status")["schema"]["anyOf"][0]["enum"] == ["Активен", "Закрыт"]
    assert parameter("/api/contracts", "urgency")["schema"]["anyOf"][0]["enum"] == [
        "due_30", "due_90", "due_over"
    ]
    assert parameter("/api/organizations", "status")["schema"]["anyOf"][0]["enum"] == ["Активен", "Закрыт"]
    assert parameter("/api/organizations", "urgency")["schema"]["anyOf"][0]["enum"] == [
        "due_30", "due_90", "due_over"
    ]
    assert parameter("/api/applications", "status")["schema"]["anyOf"][0]["enum"] == ["Заявка", "Закрыт"]
    assert parameter("/api/applications", "urgency")["schema"]["anyOf"][0]["enum"] == [
        "due_30", "due_90", "due_over"
    ]
    assert parameter("/api/export/contracts", "status")["schema"]["anyOf"][0]["enum"] == ["Активен", "Закрыт"]
    assert parameter("/api/export/contracts", "urgency")["schema"]["anyOf"][0]["enum"] == [
        "due_30", "due_90", "due_over"
    ]
    assert parameter("/api/export/applications", "status")["schema"]["anyOf"][0]["enum"] == ["Заявка", "Закрыт"]
    assert parameter("/api/export/applications", "urgency")["schema"]["anyOf"][0]["enum"] == [
        "due_30", "due_90", "due_over"
    ]
    assert parameter("/api/audit", "user_id")["schema"]["anyOf"][0]["type"] == "integer"
    assert parameter("/api/applications", "end_year")["schema"]["anyOf"][0]["type"] == "integer"
    assert parameter("/api/export/applications", "end_year")["schema"]["anyOf"][0]["type"] == "integer"
    assert "фильтр по году" in schema["paths"]["/api/applications"]["get"]["description"]
    assert schema["paths"]["/api/settings/profile"]["get"]["deprecated"] is True


def test_swagger_is_enabled_by_default_and_can_be_disabled(monkeypatch):
    monkeypatch.delenv("SHOW_DOCS", raising=False)
    with TestClient(app) as browser:
        assert browser.get("/docs").status_code == 200
        assert browser.get("/openapi.json").status_code == 200

        monkeypatch.setenv("SHOW_DOCS", "false")
        assert browser.get("/docs").status_code == 404
        assert browser.get("/openapi.json").status_code == 200


def test_api_returns_json_401_instead_of_legacy_redirect():
    with TestClient(app, follow_redirects=False) as browser:
        response = browser.get("/api/auth/me")

    assert response.status_code == 401
    assert response.json() == {"detail": "Требуется авторизация."}
    assert "location" not in response.headers


def test_login_cookie_authorizes_following_api_request(session):
    user = AppUser(
        username="swagger-user",
        email="swagger-user@example.com",
        password_hash=hash_password("Swagger-password-123"),
        full_name="Пользователь Swagger",
        role="HEAD",
        must_change_password=False,
    )
    session.add(user)
    session.commit()

    with TestClient(app) as browser:
        login = browser.post("/api/auth/login", json={
            "username": "swagger-user",
            "password": "Swagger-password-123",
        })
        assert login.status_code == 200
        assert login.json() == {
            "id": user.id,
            "username": "swagger-user",
            "email": "swagger-user@example.com",
            "full_name": "Пользователь Swagger",
            "role": "HEAD",
            "need_password_change": False,
            "need_email": False,
        }
        assert browser.cookies.get("session")

        current = browser.get("/api/auth/me")
        assert current.status_code == 200
        assert current.json() == login.json()

        changed_email = browser.put("/api/auth/email", json={"email": "updated@example.com"})
        assert changed_email.status_code == 200
        assert changed_email.json()["email"] == "updated@example.com"
        assert changed_email.json()["need_email"] is False

        logout = browser.post("/api/auth/logout")
        assert logout.status_code == 204
        assert browser.get("/api/auth/me").status_code == 401


def test_forced_password_change_is_available_through_api(session):
    user = AppUser(
        username="new-user",
        email="new-user@example.com",
        password_hash=hash_password("Temporary-password-123"),
        full_name="Новый пользователь",
        role="HEAD",
        must_change_password=True,
    )
    session.add(user)
    session.commit()

    with TestClient(app) as browser:
        login = browser.post("/api/auth/login", json={
            "username": "new-user",
            "password": "Temporary-password-123",
        })
        assert login.status_code == 200
        assert login.json()["need_password_change"] is True
        assert browser.get("/api/auth/me").status_code == 200

        changed = browser.post("/api/auth/change-password", json={
            "current_password": "Temporary-password-123",
            "new_password": "Permanent-password-456",
        })
        assert changed.status_code == 204
        assert browser.get("/api/auth/me").json()["need_password_change"] is False

