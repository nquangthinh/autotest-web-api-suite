"""Bill split backend and QA report API. Frontend templates remain frontend-owned."""

from contextlib import asynccontextmanager
import csv
import io
import json
from pathlib import Path
import subprocess
from typing import Annotated

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from db_analytics import BillStore
import qa_runner
from split_calculator import build_vietqr, calculate_split, settle_debts

ROOT = Path(__file__).resolve().parent
VND = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000_000)]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Item(InputModel):
    name: str = Field(min_length=1, max_length=200)
    price: VND
    quantity: int = Field(default=1, strict=True, ge=1, le=10000)


class SplitRequest(InputModel):
    members: list[str] = Field(max_length=200)
    items: list[Item] = Field(max_length=1000)
    assignments: dict[str, dict[str, Annotated[int, Field(strict=True, ge=0, le=1000)]] | list[str]] = Field(default_factory=dict)
    discount_amount: VND = 0
    tax_amount: VND = 0
    fee_amount: VND = 0


class VietQRRequest(InputModel):
    bank_bin: str = Field(max_length=30)
    account_no: str = Field(max_length=100)
    account_name: str = Field(min_length=1, max_length=200)
    amount: VND
    memo: str = Field(default="Chia tien", max_length=200)


class DebtRequest(InputModel):
    balances: dict[str, StrictInt]


class SaveBillRequest(InputModel):
    merchant_name: str = Field(min_length=1, max_length=200)
    bill: SplitRequest


def calculate(payload):
    try:
        result = calculate_split(**payload.model_dump())
        if result["grand_total"] > 2**63 - 1:
            raise ValueError("Bill total exceeds SQLite's integer storage limit")
        return result
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def create_app(db_path=None, reports_dir=None, template_dir=None):
    store = BillStore(db_path or ROOT / "data" / "bills.sqlite3")
    report_path = Path(reports_dir or ROOT / "reports")
    template_path = Path(template_dir or ROOT / "templates") / "index.html"

    @asynccontextmanager
    async def lifespan(application):
        store.initialize()
        yield

    application = FastAPI(title="Bill Split Backend & QA", version="1.0.0", lifespan=lifespan)
    application.state.store = store
    application.state.reports_dir = report_path
    application.state.qa_runner = qa_runner.run_suite

    @application.get("/")
    def homepage():
        if template_path.is_file():
            return HTMLResponse(template_path.read_text(encoding="utf-8"))
        return {"service": "bill-split-backend", "status": "ok", "frontend_ready": False,
                "message": "Frontend template has not been supplied.",
                "links": {"docs": "/docs", "qa_summary": "/api/qa-summary", "qa_run": "/api/qa-run"}}

    @application.get("/api/health")
    def health():
        return {"status": "ok", "currency": "VND"}

    @application.post("/api/split")
    def split(payload: SplitRequest):
        return calculate(payload)

    @application.post("/api/vietqr")
    def vietqr(payload: VietQRRequest):
        try:
            return build_vietqr(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/api/trip-debts")
    def trip_debts(payload: DebtRequest):
        try:
            transactions = settle_debts(payload.balances)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"transactions": transactions, "transaction_count": len(transactions), "currency": "VND"}

    @application.post("/api/export-csv")
    def export_csv(payload: SplitRequest):
        result = calculate(payload)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["Thành viên", "Tiền món", "Giảm giá", "Thuế VAT", "Phụ phí", "Tổng tiền"])
        for share in result["member_shares"]:
            member = share["member"]
            # Names are text, including names that Excel could otherwise treat as formulas.
            if member.lstrip().startswith(("=", "+", "-", "@")) or member.startswith(("\t", "\r", "\n")):
                member = "'" + member
            writer.writerow([member, share["subtotal"], share["discount_amount"],
                             share["tax_amount"], share["fee_amount"], share["amount"]])
        return Response(output.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="bill-split.csv"'})

    @application.post("/api/bills", status_code=201)
    def save_bill(payload: SaveBillRequest):
        return store.save(payload.merchant_name, calculate(payload.bill))

    @application.get("/api/bills/{bill_id}")
    def get_bill(bill_id: int):
        bill = store.get(bill_id)
        if bill is None:
            raise HTTPException(status_code=404, detail="Bill not found")
        return bill

    @application.get("/api/analytics")
    def analytics():
        return store.summary()

    @application.get("/api/qa-summary")
    def qa_summary():
        try:
            result = json.loads((report_path / "test_results.json").read_text(encoding="utf-8"))
            if not isinstance(result, dict) or result.get("schema_version") != "1.0":
                raise ValueError("Invalid report schema")
            return result
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="QA report unavailable; POST /api/qa-run to generate it") from exc

    @application.post("/api/qa-run")
    def qa_run():
        # Sync handler uses FastAPI's worker pool, keeping summary/health responsive.
        try:
            return application.state.qa_runner(report_path)
        except qa_runner.RunBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=504, detail="QA run exceeded 120 seconds; previous report retained") from exc
        except (qa_runner.RunnerError, OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail="QA runner failed; previous report retained") from exc

    @application.get("/api/qa-report", response_class=FileResponse)
    def qa_report():
        path = report_path / "autotest_report.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="HTML report not generated yet")
        return FileResponse(path, media_type="text/html")

    return application


app = create_app()
