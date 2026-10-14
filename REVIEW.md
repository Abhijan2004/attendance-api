# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees`: `skip = page * page_size` | Pages start at 1, so page 1 skipped a whole page of results. | `GET /employees?page=1&page_size=20` returned `items: []` while `total` was 6. | `skip = (page - 1) * page_size` |
| 2 | `list_employees`: `count_documents({})` | `total` counted every employee even when a department filter was applied. The contract says `total` counts the filtered set. | Compared with the contract: the filter `q` was used by `find` but not by the count. | `count_documents(q)` |
| 3 | `list_employees`: `find(...)` had no sort | Order was whatever MongoDB returned, so pages are not stable. The contract says `emp_code` ascending. | Read against the contract; with 100,000 records the page contents would drift. | `.sort("emp_code", 1)` before `.skip()` and `.limit()` |
| 4 | `list_employees`: `page: int = 1, page_size: int = 20` | No limits. `page=0` gives a negative skip, and `page_size` was unbounded. | Read against the contract (page >= 1, page_size 1 to 100, otherwise 422). | `Query(1, ge=1)` and `Query(20, ge=1, le=100)` |
| 5 | `list_employees` / `POST /employees`: `created_at` | Returned as text (or a naive local time). The contract says epoch milliseconds. | Compared the response with `openapi.yaml`. | Stored in UTC, returned with `to_ms(...)` |
| 6 | `punch_in`: `emp["shift_start"]` after `find_one` | An unknown employee made `emp` `None`, so reading from it crashed with a 500. The contract says 404. | `POST /attendance/punch-in` with an unknown `emp_code` returned a server error. | `if emp is None: raise HTTPException(404, ...)` |
| 7 | `punch_in`: `doc["id"] = str(res.inserted_id)` | Leaked MongoDB's internal id under another name. The contract says ids never appear in responses. | Compared the response with the `AttendanceRecord` schema. | Deleted the line, kept `doc.pop("_id", None)` |
| 8 | `punch_in`: `datetime.fromtimestamp(...)` and `datetime.now()` | No time zone, so the attendance date depends on the server's clock. R1 says the IST date. It only works on my laptop because it is already on IST. | Read against R1; could not reproduce on an IST machine. | Both calls use `tz=IST` |
| 9 | `punch_in`: returned `doc` | `punch_in` came back as text with microseconds. The contract wants epoch milliseconds and whole seconds. | The response showed `"2026-10-11T00:17:53.469823+05:30"`. | `ts.replace(microsecond=0)` and `to_ms(ts)` in the response |
| 10 | `punch_in`: `find_one` check, then `insert_one` | Race condition. Two simultaneous requests can both pass the check and both insert. | Read against the contract's concurrency rule. | Unique index on `(emp_code, date)`; `DuplicateKeyError` becomes 409 |
| 11 | `punch_in`: `d = ts.date().isoformat()` | For a night shift, a punch-in after midnight and before `shift_end` belongs to the previous day (R1). | Read against R1. | New helper `attendance_date` |
| 12 | `PunchInIn`: `status` and `punched_at` | `status` accepted any text. `punched_at` accepted floats, strings and seconds-looking numbers, which the contract rejects with 422. | Read the schema in `openapi.yaml`. | `status` is a `Literal` of PRESENT, WFH, ON_DUTY. `punched_at` is a `StrictInt` with a minimum and maximum. |
| 13 | `compute_late_minutes` | The grace check used whole minutes, so 09:40:30 counted as on time, but R2 says more than 10:00. It also used the punch-in's own date, which is wrong after midnight on a night shift. | Worked examples against R2. | Compare in seconds (`> 600`) and measure from the attendance date's shift start |
| 14 | `compute_work_hours` | `round()` is banker's rounding, so 8.125 gives 8.12. R4 says half-up. | Checked `round(8.125, 2)` by hand. | `Decimal` with `ROUND_HALF_UP` (inside `derive`) |
| 15 | `compute_overtime` | No 30-minute threshold and no night-shift handling (R3). | Read against R3. | Replaced by `derive` |
| 16 | `/health` | Always returned ok, even when MongoDB is down. The contract says 503. | Read the code against the contract. | Ping MongoDB, return 503 on failure |
| 17 | `POST /employees`: duplicate check | Check-then-insert race, and no unique index on `emp_code`. | Read the code. | Unique index; `DuplicateKeyError` becomes 409 |
| 18 | `POST /employees`: `EmployeeIn` | No validation of `emp_code` format, email, field lengths or shift times, and `shift_start` could equal `shift_end`. | Read the schema in `openapi.yaml`. | `Field` patterns and lengths, 422 for equal shifts, date check for `joined_on` |
| 19 | `GET /attendance` | Loaded every record into Python and sorted and sliced there. It also leaked `id`, returned times as datetimes, and had no page or date validation. | Read the code against the contract. | Filter, sort, skip and limit in MongoDB; `format_record`; `Query` limits; date checks |
| 20 | Startup | No indexes existed at all. | Searched the file for `create_index`. | Unique `emp_code` on employees; unique `(emp_code, date)` and `date` on attendance_logs |



Also note anything you looked at and decided was **not** a defect, and why.

- `load_dotenv()`: by default it does not override environment variables that are already set, so real variables win over `.env`, as the contract requires.
- The starter's fixed `+05:30` offset for `IST`: correct, because India has no daylight saving. The bug was that `punch_in` did not use it.
