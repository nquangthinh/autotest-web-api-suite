"""10 endpoint regression cases; real reruns also checked by scripts/verify_qa.py."""

import csv
import io
import json
import sqlite3
import subprocess
from time import perf_counter
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from main_api import create_app
import qa_runner

pytestmark = pytest.mark.api


def test_health(client):
    """Health trả HTTP 200, JSON trạng thái và đơn vị VND."""
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "currency": "VND"}


def test_split_contract_and_validation(client, bill):
    """API chia tiền đúng trọng số và trả 422 cho payload sai hoặc không bảo toàn tiền."""
    response = client.post("/api/split", json=bill)
    assert response.status_code == 200
    data = response.json()
    assert data["grand_total"] == sum(s["amount"] for s in data["member_shares"]) == 600000
    assert [s["amount"] for s in data["member_shares"]] == [100000, 200000, 300000]
    for changes in ({"discount_amount": 600001}, {"members": ["An", "An"]},
                    {"tax_amount": -1}, {"tax_amount": 0.5}, {"tax_amount": True},
                    {"assignments": {"0": {"Nobody": 1}}}, {"unexpected": 1}):
        assert client.post("/api/split", json={**bill, **changes}).status_code == 422
    assert client.post("/api/split", json={}).status_code == 422
    too_large = {"members": ["An"], "items": [{"name": "Món", "price": 10**12, "quantity": 10000}] * 1000}
    assert client.post("/api/bills", json={"merchant_name": "Overflow", "bill": too_large}).status_code == 422


def test_vietqr_normalizes_and_encodes(client, qr_payload):
    """VietQR xóa whitespace STK, bỏ dấu tên Việt và encode memo có ký tự đặc biệt."""
    response = client.post("/api/vietqr", json=qr_payload)
    assert response.status_code == 200
    data = response.json()
    assert data["account_no"] == "00123456789"
    assert data["account_name"] == "NGUYEN QUANG THINH"
    url = urlsplit(data["url"])
    assert url.scheme == "https" and url.netloc == "img.vietqr.io"
    assert url.path.endswith("/970422-00123456789-compact2.png")
    assert parse_qs(url.query) == {"amount": ["100001"], "accountName": ["NGUYEN QUANG THINH"],
                                   "addInfo": [qr_payload["memo"]]}
    assert not url.fragment and " " not in data["url"]
    assert client.post("/api/vietqr", json={**qr_payload, "account_name": "Đặng Đình Đông"}).json()["account_name"] == "DANG DINH DONG"
    for changes in ({"account_no": "12/../../"}, {"bank_bin": "abc"}, {"amount": -1}, {"account_name": "   "}):
        assert client.post("/api/vietqr", json={**qr_payload, **changes}).status_code == 422


def test_csv_has_utf8_bom_and_unicode(client, bill):
    """CSV có chính xác BOM EF BB BF ở ba byte đầu, giữ tiếng Việt và giá trị tiền."""
    response = client.post("/api/export-csv", json=bill)
    assert response.status_code == 200
    assert response.content[:3] == b"\xef\xbb\xbf"
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0] == ["Thành viên", "Tiền món", "Giảm giá", "Thuế VAT", "Phụ phí", "Tổng tiền"]
    assert rows[2][0] == "Bình"
    assert sum(int(row[-1]) for row in rows[1:]) == 600000
    formula = {"members": ["=1+1"], "items": [{"name": "Món", "price": 1}]}
    text = client.post("/api/export-csv", json=formula).content.decode("utf-8-sig")
    assert "'=1+1" in text


def test_sqlite_save_read_and_reopen(client, application, bill, tmp_path):
    """SQLite lưu thật, dùng câu lệnh tham số và đọc lại sau khi tạo ứng dụng mới."""
    merchant = "Quán Việt '); DROP TABLE bills; --"
    response = client.post("/api/bills", json={"merchant_name": merchant, "bill": bill})
    assert response.status_code == 201
    saved = response.json()
    assert client.get(f"/api/bills/{saved['id']}").json() == saved
    with sqlite3.connect(application.state.store.path) as connection:
        assert connection.execute("SELECT merchant_name, grand_total FROM bills").fetchone() == (merchant, 600000)
    reopened = create_app(db_path=application.state.store.path, reports_dir=tmp_path / "reopened-reports",
                          template_dir=tmp_path / "missing-template")
    with TestClient(reopened) as other:
        assert other.get(f"/api/bills/{saved['id']}").json() == saved


def test_analytics_and_missing_bill(client, bill):
    """Thống kê SQLite cộng đúng số hóa đơn và tổng; ID thiếu trả 404."""
    assert client.get("/api/analytics").json() == {"bill_count": 0, "total_amount": 0}
    for name in ("Quán A", "Quán B"):
        assert client.post("/api/bills", json={"merchant_name": name, "bill": bill}).status_code == 201
    assert client.get("/api/analytics").json() == {"bill_count": 2, "total_amount": 1200000}
    assert client.get("/api/bills/99999").status_code == 404
    large_bill = {"members": ["An"], "items": [{"name": "Món", "price": 10**12, "quantity": 10000}] * 500}
    for _ in range(2):
        assert client.post("/api/bills", json={"merchant_name": "Large", "bill": large_bill}).status_code == 201
    assert client.get("/api/analytics").json() == {"bill_count": 4, "total_amount": 10**19 + 1200000}


def test_trip_debt_endpoint(client):
    """Endpoint cấn trừ trả giao dịch cụ thể và từ chối số dư không cân bằng."""
    response = client.post("/api/trip-debts", json={"balances": {"A": -100, "B": -200, "C": 300}})
    assert response.status_code == 200
    assert response.json() == {"currency": "VND", "transaction_count": 2,
                               "transactions": [{"from": "A", "to": "C", "amount": 100},
                                                {"from": "B", "to": "C", "amount": 200}]}
    assert client.post("/api/trip-debts", json={"balances": {"A": 1}}).status_code == 422
    assert client.post("/api/trip-debts", json={"balances": {"A": True}}).status_code == 422


def test_qa_summary_snapshot_and_unavailable(client, application, report_snapshot):
    """QA summary trả nguyên snapshot; báo 503 khi report thiếu, hỏng hoặc sai schema."""
    response = client.get("/api/qa-summary")
    assert response.status_code == 200 and response.json() == report_snapshot
    report = application.state.reports_dir / "test_results.json"
    for invalid in ("{broken", "[]", '{"schema_version":"unknown"}'):
        report.write_text(invalid, encoding="utf-8")
        assert client.get("/api/qa-summary").status_code == 503
    report.unlink()
    assert client.get("/api/qa-summary").status_code == 503
    assert client.get("/api/qa-report").status_code == 404


def test_qa_run_orchestration_and_lock(client, application, report_snapshot):
    """QA-run gọi runner, trả snapshot mới và ánh xạ lỗi 409/504/500; khóa chặn chạy trùng."""
    calls = []
    fresh = {**report_snapshot, "run_id": "fresh-fixture"}
    def fake_runner(path):
        calls.append(path)
        (path / "test_results.json").write_text(json.dumps(fresh), encoding="utf-8")
        return fresh
    application.state.qa_runner = fake_runner
    response = client.post("/api/qa-run")
    assert response.status_code == 200 and response.json() == fresh
    assert calls == [application.state.reports_dir]
    assert client.get("/api/qa-summary").json() == fresh
    for error, status in ((qa_runner.RunBusyError("busy"), 409),
                          (subprocess.TimeoutExpired("pytest", 120), 504),
                          (qa_runner.RunnerError("worker failed"), 500)):
        def failing_runner(path):
            raise error
        application.state.qa_runner = failing_runner
        assert client.post("/api/qa-run").status_code == status
        assert client.get("/api/qa-summary").json() == fresh
    with qa_runner.run_lock(application.state.reports_dir):
        with pytest.raises(qa_runner.RunBusyError):
            with qa_runner.run_lock(application.state.reports_dir):
                pytest.fail("Concurrent lock was accepted")
    with qa_runner.run_lock(application.state.reports_dir):
        pass


def test_api_sla_under_500ms(client, bill, qr_payload, report_snapshot, record_property):
    """140 HTTP requests trên 7 endpoint: mỗi phản hồi <500ms; lưu toàn bộ mẫu đo thực tế."""
    saved = client.post("/api/bills", json={"merchant_name": "SLA", "bill": bill}).json()
    requests = [("GET", "/api/health", None), ("POST", "/api/split", bill),
                ("POST", "/api/vietqr", qr_payload), ("POST", "/api/export-csv", bill),
                ("GET", "/api/analytics", None), ("GET", f"/api/bills/{saved['id']}", None),
                ("GET", "/api/qa-summary", None)]
    samples = []
    try:
        for _ in range(20):
            for method, endpoint, payload in requests:
                started = perf_counter()
                response = client.request(method, endpoint, **({"json": payload} if payload else {}))
                elapsed = (perf_counter() - started) * 1000
                samples.append({"endpoint": f"{method} {endpoint}", "duration_ms": elapsed})
                assert response.status_code == 200
                assert elapsed < 500, f"{method} {endpoint}: {elapsed:.3f}ms"
    finally:
        record_property("sla_samples", samples)
