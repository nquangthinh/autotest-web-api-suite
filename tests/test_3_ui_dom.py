"""6 backend HTTP/data/HTML-transport cases; no production UI assertions."""

import json
from bs4 import BeautifulSoup
import pytest

pytestmark = pytest.mark.dom


def test_homepage_http_200(client):
    """Trang chủ backend trả HTTP 200 kể cả khi Frontend chưa cung cấp template."""
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


def test_homepage_fallback_data_contract(client):
    """JSON trang chủ nêu rõ frontend_ready=false và cung cấp đường dẫn tích hợp QA."""
    data = client.get("/").json()
    assert data["status"] == "ok" and data["frontend_ready"] is False
    assert data["service"] == "bill-split-backend"
    assert data["links"] == {"docs": "/docs", "qa_summary": "/api/qa-summary", "qa_run": "/api/qa-run"}


def test_frontend_template_served_unchanged(client, frontend_fixture):
    """Template tạm do test cung cấp được phục vụ nguyên văn với HTTP 200 và text/html."""
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.content == frontend_fixture.encode("utf-8")


def test_template_dom_transport(client, frontend_fixture):
    """BeautifulSoup đọc được DOM của fixture qua HTTP; backend giữ html/head/body và UTF-8."""
    soup = BeautifulSoup(client.get("/").text, "html.parser")
    assert soup.html["lang"] == "vi"
    assert soup.head.meta["charset"] == "utf-8"
    assert soup.title.string == "Hợp đồng kiểm thử"
    assert soup.body.select_one("main#fixture") is not None


def test_template_unicode_and_embedded_data(client, frontend_fixture):
    """Backend giữ tên tiếng Việt, entity HTML và JSON nhúng trong template fixture."""
    soup = BeautifulSoup(client.get("/").text, "html.parser")
    assert soup.select_one("#fixture").get_text() == "Nguyễn Quang Thịnh & bạn bè"
    embedded = soup.select_one("script#fixture-data")
    assert embedded["type"] == "application/json"
    assert json.loads(embedded.string) == {"currency": "VND", "members": ["Bình", "Chi"]}


def test_openapi_exposes_frontend_contract(client):
    """OpenAPI công bố đúng phương thức hai API QA cùng schema đầu vào chia tiền."""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert set(schema["paths"]["/api/qa-summary"]) == {"get"}
    assert set(schema["paths"]["/api/qa-run"]) == {"post"}
    assert "200" in schema["paths"]["/api/qa-summary"]["get"]["responses"]
    assert "200" in schema["paths"]["/api/qa-run"]["post"]["responses"]
    assert schema["components"]["schemas"]["SplitRequest"]["required"] == ["members", "items"]
