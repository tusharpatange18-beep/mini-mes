# Mini MES: Work Order to Dispatch

A small Manufacturing Execution System prototype. It tracks one work order from release to dispatch in a bottling-line scenario, enforces the process rules, and gives batch traceability. Built with Python, FastAPI and SQLite.

## Run it

```
pip install -r requirements.txt
uvicorn app:app --reload
```

Open http://localhost:8000 for the web UI, or http://localhost:8000/docs for the API.

Pick a role in the UI (Planner, Store Keeper, Line Operator, QC Inspector, Dispatch Clerk). The role is sent in the `X-Role` header. This is a prototype, so there is no login.

## Run the acceptance tests

```
pytest -v
```

`test_atp.py` holds one automated test per test case (TC01 to TC10) in the Acceptance Test Plan, plus a role check.

## Requirements and where they live

| Req | What the system does | Endpoint |
| --- | --- | --- |
| R1 | Planner creates and releases a work order | `POST /work-orders`, `POST /work-orders/{id}/release` |
| R2 | Store Keeper issues material and records the lot number | `POST /work-orders/{id}/issue-material` |
| R3 | Material issue is blocked unless the order is Released | same endpoint, returns 409 |
| R4 | Operator starts production, reports good and rejected quantity; a reason code is required for rejects | `POST /work-orders/{id}/start`, `POST /work-orders/{id}/production` |
| R5 | QC Inspector records pass or fail; a failed batch cannot move to finished goods | `POST /work-orders/{id}/qc`, `POST /work-orders/{id}/finished-goods` |
| R6 | A failed batch goes to rework or scrap, with defect type | `POST /work-orders/{id}/disposition` |
| R7 | Only a batch that passed QC can be dispatched | `POST /work-orders/{id}/dispatch` |
| R8 | Batch traceability: work order, material lots, production, QC, dispatch, status history | `GET /trace/{batch_no}` |

## Work order status flow

Created → Released → In production → QC pending → QC passed → Dispatched

A QC failed batch goes to Rework (back to In production) or Scrap (SCRAPPED). Every status change is written to the `status_log` table with the role and time. Any step that is skipped, or attempted by the wrong role, is rejected with a clear error.

## Database tables

`work_orders`, `material_issues`, `production_reports`, `qc_results`, `dispatches`, `status_log`. The batch number (`B-0001` style) is assigned at release and links every table together for traceability.

## Limitations

- No login or passwords; the role comes from a header.
- No machine (PLC) integration.
- One production report per run; rework creates a new report.
