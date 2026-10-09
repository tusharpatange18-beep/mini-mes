"""Mini MES: work order tracking from release to dispatch, with batch traceability.

Each endpoint maps to a requirement (R1-R8) in the project document.
Role is passed in the X-Role header (PLANNER, STORE_KEEPER, OPERATOR,
QC_INSPECTOR, DISPATCH_CLERK). This is a prototype: no passwords.
"""
import os
import sqlite3
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

app = FastAPI(title="Mini MES")
HERE = os.path.dirname(os.path.abspath(__file__))

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    due_date TEXT NOT NULL,
    status TEXT NOT NULL,
    batch_no TEXT UNIQUE,
    finished_goods_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS material_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id INTEGER NOT NULL REFERENCES work_orders(id),
    lot_no TEXT NOT NULL,
    qty REAL NOT NULL,
    issued_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS production_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id INTEGER NOT NULL REFERENCES work_orders(id),
    good_qty INTEGER NOT NULL,
    rejected_qty INTEGER NOT NULL,
    reason_code TEXT,
    reported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qc_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id INTEGER NOT NULL REFERENCES work_orders(id),
    result TEXT NOT NULL,
    inspector_role TEXT NOT NULL,
    defect_type TEXT,
    disposition TEXT,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id INTEGER NOT NULL REFERENCES work_orders(id),
    customer_order TEXT NOT NULL,
    qty INTEGER NOT NULL,
    dispatched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS status_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id INTEGER NOT NULL REFERENCES work_orders(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    role TEXT NOT NULL,
    changed_at TEXT NOT NULL
);
"""


def db():
    conn = sqlite3.connect(os.environ.get("MES_DB", os.path.join(HERE, "mes.db")))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = db()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def now():
    return datetime.now().isoformat(timespec="seconds")


def need_role(role: Optional[str], *allowed: str):
    if role not in allowed:
        raise HTTPException(403, f"Role {role!r} is not allowed. Allowed: {', '.join(allowed)}")


def get_wo(conn, wo_id: int):
    wo = conn.execute("SELECT * FROM work_orders WHERE id = ?", (wo_id,)).fetchone()
    if wo is None:
        raise HTTPException(404, "Work order not found")
    return wo


def need_status(wo, *allowed: str, msg: str):
    if wo["status"] not in allowed:
        raise HTTPException(409, f"{msg} (current status: {wo['status']})")


def set_status(conn, wo, new_status: str, role: str):
    conn.execute("UPDATE work_orders SET status = ? WHERE id = ?", (new_status, wo["id"]))
    conn.execute(
        "INSERT INTO status_log (wo_id, from_status, to_status, role, changed_at) VALUES (?,?,?,?,?)",
        (wo["id"], wo["status"], new_status, role, now()),
    )


class NewWorkOrder(BaseModel):
    product: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    due_date: str = Field(min_length=1)


class MaterialIssue(BaseModel):
    lot_no: str = Field(min_length=1)
    qty: float = Field(gt=0)


class ProductionReport(BaseModel):
    good_qty: int = Field(ge=0)
    rejected_qty: int = Field(ge=0)
    reason_code: Optional[str] = None


class QcResult(BaseModel):
    result: str = Field(pattern="^(PASS|FAIL)$")


class Disposition(BaseModel):
    decision: str = Field(pattern="^(REWORK|SCRAP)$")
    defect_type: str = Field(min_length=1)


class Dispatch(BaseModel):
    customer_order: str = Field(min_length=1)
    qty: int = Field(gt=0)


@app.post("/work-orders", status_code=201)  # R1
def create_work_order(body: NewWorkOrder, x_role: Optional[str] = Header(None)):
    need_role(x_role, "PLANNER")
    conn = db()
    cur = conn.execute(
        "INSERT INTO work_orders (product, quantity, due_date, status, created_at) VALUES (?,?,?,?,?)",
        (body.product, body.quantity, body.due_date, "CREATED", now()),
    )
    conn.execute(
        "INSERT INTO status_log (wo_id, from_status, to_status, role, changed_at) VALUES (?,?,?,?,?)",
        (cur.lastrowid, None, "CREATED", x_role, now()),
    )
    conn.commit()
    row = get_wo(conn, cur.lastrowid)
    return dict(row)


@app.post("/work-orders/{wo_id}/release")  # R1
def release(wo_id: int, x_role: Optional[str] = Header(None)):
    need_role(x_role, "PLANNER")
    conn = db()
    wo = get_wo(conn, wo_id)
    need_status(wo, "CREATED", msg="Only a Created work order can be released")
    batch_no = f"B-{wo_id:04d}"
    conn.execute("UPDATE work_orders SET batch_no = ? WHERE id = ?", (batch_no, wo_id))
    set_status(conn, wo, "RELEASED", x_role)
    conn.commit()
    return dict(get_wo(conn, wo_id))


@app.post("/work-orders/{wo_id}/issue-material")  # R2, R3
def issue_material(wo_id: int, body: MaterialIssue, x_role: Optional[str] = Header(None)):
    need_role(x_role, "STORE_KEEPER")
    conn = db()
    wo = get_wo(conn, wo_id)
    need_status(wo, "RELEASED", msg="Work order must be Released before material can be issued")
    conn.execute(
        "INSERT INTO material_issues (wo_id, lot_no, qty, issued_at) VALUES (?,?,?,?)",
        (wo_id, body.lot_no, body.qty, now()),
    )
    conn.commit()
    return dict(get_wo(conn, wo_id))


@app.post("/work-orders/{wo_id}/start")  # R4
def start_production(wo_id: int, x_role: Optional[str] = Header(None)):
    need_role(x_role, "OPERATOR")
    conn = db()
    wo = get_wo(conn, wo_id)
    need_status(wo, "RELEASED", msg="Work order must be Released to start production")
    issued = conn.execute("SELECT COUNT(*) FROM material_issues WHERE wo_id = ?", (wo_id,)).fetchone()[0]
    if issued == 0:
        raise HTTPException(409, "Issue material before starting production")
    set_status(conn, wo, "IN_PRODUCTION", x_role)
    conn.commit()
    return dict(get_wo(conn, wo_id))


@app.post("/work-orders/{wo_id}/production")  # R4
def report_production(wo_id: int, body: ProductionReport, x_role: Optional[str] = Header(None)):
    need_role(x_role, "OPERATOR")
    conn = db()
    wo = get_wo(conn, wo_id)
    need_status(wo, "IN_PRODUCTION", msg="Production can only be reported while In production")
    if body.rejected_qty > 0 and not (body.reason_code and body.reason_code.strip()):
        raise HTTPException(422, "A reason code is required when rejected quantity is above 0")
    if body.good_qty + body.rejected_qty > wo["quantity"]:
        raise HTTPException(422, "Good plus rejected quantity cannot exceed the work order quantity")
    conn.execute(
        "INSERT INTO production_reports (wo_id, good_qty, rejected_qty, reason_code, reported_at) VALUES (?,?,?,?,?)",
        (wo_id, body.good_qty, body.rejected_qty, body.reason_code, now()),
    )
    set_status(conn, wo, "QC_PENDING", x_role)
    conn.commit()
    return dict(get_wo(conn, wo_id))


@app.post("/work-orders/{wo_id}/qc")  # R5
def record_qc(wo_id: int, body: QcResult, x_role: Optional[str] = Header(None)):
    need_role(x_role, "QC_INSPECTOR")
    conn = db()
    wo = get_wo(conn, wo_id)
    need_status(wo, "QC_PENDING", msg="QC result can only be recorded for a batch awaiting QC")
    conn.execute(
        "INSERT INTO qc_results (wo_id, result, inspector_role, recorded_at) VALUES (?,?,?,?)",
        (wo_id, body.result, x_role, now()),
    )
    set_status(conn, wo, "QC_PASSED" if body.result == "PASS" else "QC_FAILED", x_role)
    conn.commit()
    return dict(get_wo(conn, wo_id))


@app.post("/work-orders/{wo_id}/disposition")  # R6
def disposition(wo_id: int, body: Disposition, x_role: Optional[str] = Header(None)):
    need_role(x_role, "QC_INSPECTOR")
    conn = db()
    wo = get_wo(conn, wo_id)
    need_status(wo, "QC_FAILED", msg="Only a QC failed batch can be sent to rework or scrap")
    conn.execute(
        "UPDATE qc_results SET defect_type = ?, disposition = ? "
        "WHERE id = (SELECT MAX(id) FROM qc_results WHERE wo_id = ?)",
        (body.defect_type, body.decision, wo_id),
    )
    set_status(conn, wo, "IN_PRODUCTION" if body.decision == "REWORK" else "SCRAPPED", x_role)
    conn.commit()
    return dict(get_wo(conn, wo_id))


@app.post("/work-orders/{wo_id}/finished-goods")  # R5
def move_to_finished_goods(wo_id: int, x_role: Optional[str] = Header(None)):
    need_role(x_role, "STORE_KEEPER")
    conn = db()
    wo = get_wo(conn, wo_id)
    need_status(wo, "QC_PASSED", msg="Only a batch that passed QC can move to finished goods")
    conn.execute("UPDATE work_orders SET finished_goods_at = ? WHERE id = ?", (now(), wo_id))
    conn.commit()
    return dict(get_wo(conn, wo_id))


@app.post("/work-orders/{wo_id}/dispatch")  # R7
def dispatch(wo_id: int, body: Dispatch, x_role: Optional[str] = Header(None)):
    need_role(x_role, "DISPATCH_CLERK")
    conn = db()
    wo = get_wo(conn, wo_id)
    need_status(wo, "QC_PASSED", msg="Only a batch that passed QC can be dispatched")
    conn.execute(
        "INSERT INTO dispatches (wo_id, customer_order, qty, dispatched_at) VALUES (?,?,?,?)",
        (wo_id, body.customer_order, body.qty, now()),
    )
    set_status(conn, wo, "DISPATCHED", x_role)
    conn.commit()
    return dict(get_wo(conn, wo_id))


@app.get("/work-orders")
def list_work_orders():
    conn = db()
    return [dict(r) for r in conn.execute("SELECT * FROM work_orders ORDER BY id DESC")]


@app.get("/trace/{batch_no}")  # R8
def trace(batch_no: str):
    conn = db()
    wo = conn.execute("SELECT * FROM work_orders WHERE batch_no = ?", (batch_no,)).fetchone()
    if wo is None:
        raise HTTPException(404, "Batch not found")

    def rows(table, order="id"):
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} WHERE wo_id = ? ORDER BY {order}", (wo["id"],))]

    return {
        "work_order": dict(wo),
        "material_lots": rows("material_issues"),
        "production": rows("production_reports"),
        "qc": rows("qc_results"),
        "dispatch": rows("dispatches"),
        "status_history": rows("status_log"),
    }


init_db()
app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")
