"""Acceptance Test Plan TC01-TC10, run as automated tests.

Run with:  pytest -v
Each test name matches a test case ID in the project document.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MES_DB", str(tmp_path / "test.db"))
    import app as mes
    mes.init_db()
    return TestClient(mes.app)


def as_role(role):
    return {"X-Role": role}


def new_order(client, qty=500):
    r = client.post("/work-orders", json={"product": "Juice 1L", "quantity": qty, "due_date": "2026-10-20"},
                    headers=as_role("PLANNER"))
    assert r.status_code == 201
    return r.json()["id"]


def release(client, wo):
    return client.post(f"/work-orders/{wo}/release", headers=as_role("PLANNER"))


def issue(client, wo, lot="L-101", qty=100):
    return client.post(f"/work-orders/{wo}/issue-material", json={"lot_no": lot, "qty": qty},
                       headers=as_role("STORE_KEEPER"))


def to_qc_pending(client):
    wo = new_order(client)
    release(client, wo)
    issue(client, wo)
    client.post(f"/work-orders/{wo}/start", headers=as_role("OPERATOR"))
    client.post(f"/work-orders/{wo}/production", json={"good_qty": 480, "rejected_qty": 20, "reason_code": "SPILL"},
                headers=as_role("OPERATOR"))
    return wo


def qc(client, wo, result):
    return client.post(f"/work-orders/{wo}/qc", json={"result": result}, headers=as_role("QC_INSPECTOR"))


def status(client, wo):
    return next(w["status"] for w in client.get("/work-orders").json() if w["id"] == wo)


def test_tc01_create_and_release(client):  # R1
    wo = new_order(client)
    r = release(client, wo)
    assert r.status_code == 200
    assert r.json()["status"] == "RELEASED"
    assert r.json()["batch_no"] == f"B-{wo:04d}"


def test_tc02_issue_material_to_released_order(client):  # R2
    wo = new_order(client)
    release(client, wo)
    r = issue(client, wo)
    assert r.status_code == 200
    trace = client.get(f"/trace/B-{wo:04d}").json()
    assert trace["material_lots"][0]["lot_no"] == "L-101"


def test_tc03_issue_material_to_unreleased_order_is_blocked(client):  # R3 (negative)
    wo = new_order(client)
    r = issue(client, wo)
    assert r.status_code == 409
    assert "Released" in r.json()["detail"]


def test_tc04_report_production_output(client):  # R4
    wo = to_qc_pending(client)
    assert status(client, wo) == "QC_PENDING"
    trace = client.get(f"/trace/B-{wo:04d}").json()
    assert trace["production"][0]["good_qty"] == 480
    assert trace["production"][0]["rejected_qty"] == 20


def test_tc05_rejected_quantity_needs_reason_code(client):  # R4 (negative)
    wo = new_order(client)
    release(client, wo)
    issue(client, wo)
    client.post(f"/work-orders/{wo}/start", headers=as_role("OPERATOR"))
    r = client.post(f"/work-orders/{wo}/production", json={"good_qty": 480, "rejected_qty": 20},
                    headers=as_role("OPERATOR"))
    assert r.status_code == 422
    assert status(client, wo) == "IN_PRODUCTION"


def test_tc06_qc_pass(client):  # R5
    wo = to_qc_pending(client)
    r = qc(client, wo, "PASS")
    assert r.status_code == 200
    assert r.json()["status"] == "QC_PASSED"
    assert client.post(f"/work-orders/{wo}/finished-goods", headers=as_role("STORE_KEEPER")).status_code == 200


def test_tc07_qc_fail_blocks_finished_goods(client):  # R5 (negative)
    wo = to_qc_pending(client)
    qc(client, wo, "FAIL")
    r = client.post(f"/work-orders/{wo}/finished-goods", headers=as_role("STORE_KEEPER"))
    assert r.status_code == 409


def test_tc08_scrap_decision_is_recorded(client):  # R6
    wo = to_qc_pending(client)
    qc(client, wo, "FAIL")
    r = client.post(f"/work-orders/{wo}/disposition", json={"decision": "SCRAP", "defect_type": "Underfill"},
                    headers=as_role("QC_INSPECTOR"))
    assert r.status_code == 200
    assert r.json()["status"] == "SCRAPPED"
    row = client.get(f"/trace/B-{wo:04d}").json()["qc"][0]
    assert row["disposition"] == "SCRAP" and row["defect_type"] == "Underfill"
    assert row["inspector_role"] == "QC_INSPECTOR"


def test_tc09_dispatch_of_failed_batch_is_blocked(client):  # R7 (negative)
    wo = to_qc_pending(client)
    qc(client, wo, "FAIL")
    r = client.post(f"/work-orders/{wo}/dispatch", json={"customer_order": "CO-77", "qty": 480},
                    headers=as_role("DISPATCH_CLERK"))
    assert r.status_code == 409


def test_tc10_dispatch_and_trace(client):  # R7, R8
    wo = to_qc_pending(client)
    qc(client, wo, "PASS")
    r = client.post(f"/work-orders/{wo}/dispatch", json={"customer_order": "CO-77", "qty": 480},
                    headers=as_role("DISPATCH_CLERK"))
    assert r.status_code == 200
    trace = client.get(f"/trace/B-{wo:04d}").json()
    assert trace["work_order"]["status"] == "DISPATCHED"
    assert trace["material_lots"][0]["lot_no"] == "L-101"
    assert trace["qc"][0]["result"] == "PASS"
    assert trace["dispatch"][0]["customer_order"] == "CO-77"
    assert [s["to_status"] for s in trace["status_history"]][-1] == "DISPATCHED"


def test_wrong_role_is_rejected(client):  # role-based access
    r = client.post("/work-orders", json={"product": "X", "quantity": 1, "due_date": "2026-10-20"},
                    headers=as_role("OPERATOR"))
    assert r.status_code == 403
