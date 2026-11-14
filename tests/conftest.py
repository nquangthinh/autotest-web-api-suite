import json
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main_api import create_app


@pytest.fixture
def bill():
    return {"members": ["An", "Bình", "Chi"],
            "items": [{"name": "Lẩu", "price": 600000, "quantity": 1}],
            "assignments": {"0": {"An": 1, "Bình": 2, "Chi": 3}},
            "discount_amount": 90000, "tax_amount": 60000, "fee_amount": 30000}


@pytest.fixture
def qr_payload():
    return {"bank_bin": "970422", "account_no": " 0012 3456\t789\n",
            "account_name": " Nguyễn   Quang Thịnh ", "amount": 100001,
            "memo": "Ăn trưa & cà phê / #1?"}


@pytest.fixture
def application(tmp_path):
    return create_app(db_path=tmp_path / "test.sqlite3", reports_dir=tmp_path / "reports",
                      template_dir=tmp_path / "frontend")


@pytest.fixture
def client(application):
    with TestClient(application) as session:
        yield session


@pytest.fixture
def report_snapshot(application):
    """Minimal distinguishable snapshot to test pass-through, not measured QA data."""
    result = {"schema_version": "1.0", "run_id": "fixture-only", "status": "PASSED",
              "total_tests": 0, "passed": 0, "pass_rate_percent": 0,
              "total_duration_ms": 0, "tests": [], "layers": {}, "defects": [], "sla": {}}
    directory = application.state.reports_dir
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "test_results.json").write_text(json.dumps(result), encoding="utf-8")
    return result


@pytest.fixture
def frontend_fixture(tmp_path):
    """A disposable transport fixture; never writes the project's templates/index.html."""
    directory = tmp_path / "frontend"
    directory.mkdir()
    content = ('<!doctype html><html lang="vi"><head><meta charset="utf-8">'
               '<title>Hợp đồng kiểm thử</title></head><body>'
               '<main id="fixture">Nguyễn Quang Thịnh &amp; bạn bè</main>'
               '<script type="application/json" id="fixture-data">'
               '{"currency":"VND","members":["Bình","Chi"]}</script></body></html>')
    (directory / "index.html").write_text(content, encoding="utf-8")
    return content
