# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All code is in `app/main.py`.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI=mongodb://localhost:27017 MONGO_DB=hrone
python sample_seed.py
uvicorn app.main:app --port 8000
```
MongoDB **6.0 or newer** is required. Real environment variables win over a local `.env`. Indexes are created at startup (and again on the first successful `/health`).

## Implemented
- Starter fixes: see `REVIEW.md`.
- `POST /attendance/punch-out` and `PATCH /attendance/{emp_code}/{date}` (append-only `history`).
- Analytics as aggregation pipelines: employee monthly, department summary, late leaderboard, department trend.
- `GET /admin/explain/{endpoint}`.
- Design answers: see `DECISIONS.md`.

## Tested by hand (via `/docs`)
- `/health`, create employee, punch-in, punch-out, PATCH with history, `GET /attendance`.
- Employee monthly, department summary and department trend, checked against hand-counted numbers.
- Explain for `employee_monthly` (uses the `(emp_code, date)` index).
- Late leaderboard, for the "nobody was late" case.

## Not tested / known gaps
- Late leaderboard ranking and ties with real data.
- Simultaneous requests (handled by a unique index and filtered updates; no concurrent test run).
- Night shifts end to end.
- The 503 path of `/health`.
- Explain output for the leaderboard and trend endpoints.
- Error body is FastAPI's default `{"detail": ...}`; not checked against the contract's error schema.
