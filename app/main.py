"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import os
import re
import json
import calendar
from datetime import datetime, timedelta, timezone, date as Date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #
load_dotenv()  

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=2000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))  

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

_indexes_ready = False


def ensure_indexes():
    """Create the indexes once. Safe to call repeatedly."""
    global _indexes_ready
    if _indexes_ready:
        return
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index("date")
    _indexes_ready = True


try:
    ensure_indexes()
except Exception as exc:  
    print("WARNING: could not create indexes yet:", exc)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_ms(dt):
    """aware datetime -> epoch milliseconds (None stays None)"""
    return None if dt is None else int(dt.timestamp() * 1000)


def from_db(dt):
    """MongoDB returns UTC times without a label: label them, then show in IST"""
    return None if dt is None else dt.replace(tzinfo=timezone.utc).astimezone(IST)


def round_half_up(value, places=2):
    q = Decimal(1).scaleb(-places)  
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def valid_date_string(s):
    try:
        Date.fromisoformat(s)
        return len(s) == 10  
    except ValueError:
        return False


def month_bounds(month):
    """'2026-10' -> ('2026-10-01', '2026-11-01'); dates are stored as text"""
    year = int(month[:4])
    mon = int(month[5:7])
    start = month + "-01"
    next_start = f"{year + 1}-01-01" if mon == 12 else f"{year}-{mon + 1:02d}-01"
    return start, next_start


def count_working_days(month, joined_on):
    """Mon-Fri days of the month, counted from joined_on (R7)"""
    year = int(month[:4])
    mon = int(month[5:7])
    days_in_month = calendar.monthrange(year, mon)[1]
    count = 0
    for day in range(1, days_in_month + 1):
        d = datetime(year, mon, day)
        if d.weekday() >= 5:  
            continue
        if d.strftime("%Y-%m-%d") < joined_on:  
            continue
        count += 1
    return count


def attendance_date(ts, emp):
    """R1: IST date of the punch-in, except overnight shifts before shift_end"""
    d = ts.date()
    if emp["shift_end"] <= emp["shift_start"] and ts.strftime("%H:%M") < emp["shift_end"]:
        d = d - timedelta(days=1)
    return d.isoformat()


def compute_late_minutes(punch_in, shift_start, att_date=None):
    """R2: late only if MORE than 10:00 after shift start; minutes counted from shift start"""
    h, m = map(int, shift_start.split(":"))
    base = punch_in
    if att_date is not None:
        base = datetime.strptime(att_date, "%Y-%m-%d").replace(tzinfo=punch_in.tzinfo)
    start = base.replace(hour=h, minute=m, second=0, microsecond=0)
    secs = (punch_in - start).total_seconds()
    return int(secs // 60) if secs > 600 else 0


def derive(emp, att_date, p_in, p_out):
    """late_minutes, work_hours, overtime_minutes, half_day from the final times"""
    if p_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(p_in, emp["shift_start"], att_date)
    if p_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    secs = (p_out - p_in).total_seconds()
    wh = (Decimal(int(secs)) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    shift_end = datetime.strptime(att_date + " " + emp["shift_end"], "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    if emp["shift_end"] <= emp["shift_start"]:  
        shift_end += timedelta(days=1)
    ot = int((p_out - shift_end).total_seconds() // 60)
    return {
        "late_minutes": late,
        "work_hours": float(wh),
        "overtime_minutes": ot if ot >= 30 else 0,
        "half_day": wh < Decimal("4.50"),
    }


def format_record(d):
    """database record -> response (times as milliseconds, no _id)"""
    d["punch_in"] = to_ms(from_db(d["punch_in"]))
    d["punch_out"] = to_ms(from_db(d["punch_out"]))
    d["history"] = [
        {**h, "at": to_ms(from_db(h["at"])) if isinstance(h["at"], datetime) else h["at"]}
        for h in d["history"]
    ]
    d.pop("_id", None)
    return d


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
MIN_MS = 100_000_000_000  
MAX_MS = 253_402_300_799_999  


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, ge=MIN_MS, le=MAX_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, ge=MIN_MS, le=MAX_MS)


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[StrictInt] = Field(None, ge=MIN_MS, le=MAX_MS)
    punch_out: Optional[StrictInt] = Field(None, ge=MIN_MS, le=MAX_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Health and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    try:
        ensure_indexes()
    except Exception as exc:
        print("WARNING: could not create indexes:", exc)
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, "shift_start and shift_end must differ")
    if not valid_date_string(body.joined_on):
        raise HTTPException(422, "joined_on must be YYYY-MM-DD")

    now = datetime.now(timezone.utc)
    doc = body.model_dump()
    doc["created_at"] = now
    try:
        db.employees.insert_one(doc)  
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = to_ms(now)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size))
    for e in items:
        if isinstance(e.get("created_at"), datetime):
            e["created_at"] = to_ms(from_db(e["created_at"]))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=IST)
    else:
        ts = datetime.now(tz=IST)
    ts = ts.replace(microsecond=0)  
    d = attendance_date(ts, emp)

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index: only one wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    doc.pop("_id", None)
    doc["punch_in"] = to_ms(ts)
    return doc


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    
    if body.punched_at is not None:
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=IST)
    else:
        ts = datetime.now(tz=IST)
    ts = ts.replace(microsecond=0)

    
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")

    
    if rec["punch_out"] is not None:
        raise HTTPException(409, "already punched out")

    
    punched_in = rec["punch_in"].replace(tzinfo=timezone.utc)
    secs = (ts - punched_in).total_seconds()
    if secs <= 0 or secs > 24 * 3600:
        raise HTTPException(422, "invalid punch-out time")

    
    derived = derive(emp, rec["date"], punched_in.astimezone(IST), ts)

    
    result = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": derived["work_hours"],
            "overtime_minutes": derived["overtime_minutes"],
            "half_day": derived["half_day"],
        }},
    )
    if result.matched_count == 0:
        raise HTTPException(409, "already punched out")

    
    out = format_record(rec)  
    out["punch_out"] = to_ms(ts)
    out["work_hours"] = derived["work_hours"]
    out["overtime_minutes"] = derived["overtime_minutes"]
    out["half_day"] = derived["half_day"]
    return out


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    
    old_status = rec["status"]
    old_in = from_db(rec["punch_in"])
    old_out = from_db(rec["punch_out"])

    
    new_status = body.status if body.status is not None else old_status

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_in = None
        new_out = None  
    else:
        new_in = old_in
        new_out = old_out
        if body.punch_in is not None:
            new_in = datetime.fromtimestamp(body.punch_in / 1000, tz=IST).replace(microsecond=0)
        if body.punch_out is not None:
            new_out = datetime.fromtimestamp(body.punch_out / 1000, tz=IST).replace(microsecond=0)

        
        if new_in is None:
            raise HTTPException(422, "a presence status needs a punch_in")
        if attendance_date(new_in, emp) != date:
            raise HTTPException(422, "punch_in must be on the record's date")
        if new_out is not None:
            secs = (new_out - new_in).total_seconds()
            if secs <= 0 or secs > 24 * 3600:
                raise HTTPException(422, "invalid punch_out")

    
    nothing_changed = (
        new_status == old_status
        and to_ms(new_in) == to_ms(old_in)
        and to_ms(new_out) == to_ms(old_out)
    )
    if nothing_changed:
        raise HTTPException(422, "nothing to change")

    
    derived = derive(emp, date, new_in, new_out)

    
    before = {
        "status": old_status,
        "punch_in": to_ms(old_in),
        "punch_out": to_ms(old_out),
        "late_minutes": rec["late_minutes"],
        "work_hours": rec["work_hours"],
        "overtime_minutes": rec["overtime_minutes"],
        "half_day": rec["half_day"],
    }
    after = {
        "status": new_status,
        "punch_in": to_ms(new_in),
        "punch_out": to_ms(new_out),
        "late_minutes": derived["late_minutes"],
        "work_hours": derived["work_hours"],
        "overtime_minutes": derived["overtime_minutes"],
        "half_day": derived["half_day"],
    }
    changes = {}
    for field in after:
        if after[field] != before[field]:
            changes[field] = {"from": before[field], "to": after[field]}

    
    now = datetime.now(tz=IST).replace(microsecond=0)
    note = {"at": now, "by": body.regularized_by, "reason": body.reason, "changes": changes}

    result = db.attendance_logs.update_one(
        {"_id": rec["_id"], "history": {"$size": len(rec["history"])}},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                "late_minutes": derived["late_minutes"],
                "work_hours": derived["work_hours"],
                "overtime_minutes": derived["overtime_minutes"],
                "half_day": derived["half_day"],
            },
            "$push": {"history": note},
        },
    )
    if result.matched_count == 0:
        raise HTTPException(409, "record was changed by someone else, try again")

    
    history_out = []
    for h in rec["history"]:
        at = h["at"]
        if isinstance(at, datetime):
            at = to_ms(from_db(at))
        history_out.append({"at": at, "by": h["by"], "reason": h["reason"], "changes": h["changes"]})
    history_out.append({"at": to_ms(now), "by": body.regularized_by, "reason": body.reason, "changes": changes})

    rec.update(after)
    rec["history"] = history_out
    rec.pop("_id", None)
    return rec


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    for v in (date_from, date_to):
        if v is not None and not valid_date_string(v):
            raise HTTPException(422, "dates must be YYYY-MM-DD")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    docs = (db.attendance_logs.find(q)
            .sort([("date", -1), ("emp_code", 1)])
            .skip((page - 1) * page_size)
            .limit(page_size))

    return {"items": [format_record(d) for d in docs], "total": total,
            "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Analytics: pipeline builders (shared by the endpoints and the explain endpoint)
# --------------------------------------------------------------------------- #
PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]


def build_employee_monthly_pipeline(emp_code, start, next_start):
    return [
       
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lt": next_start}}},
        
        {"$addFields": {"dow": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}}},
        
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", PRESENT_STATUSES]},
                    {"$gte": ["$dow", 2]},
                    {"$lte": ["$dow", 6]},
                ]},
                {"$cond": ["$half_day", 0.5, 1]},
                0,
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]


def build_department_summary_pipeline(start, next_start, department=None):
    is_present = {"$in": ["$$l.status", PRESENT_STATUSES]}
    dow = {"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}  # 1=Sun ... 7=Sat
    has_hours = {"$and": [is_present, {"$isNumber": "$$l.work_hours"}]}

    def over_logs(value):
        
        return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": value}}}

    
    match = {"joined_on": {"$lt": next_start}}
    if department:
        match["department"] = department

    return [
        {"$match": match},
        
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start, "$lt": next_start}}}],
            "as": "logs",
        }},
        
        {"$addFields": {
            "present_days": over_logs({"$cond": [
                {"$and": [is_present, {"$gte": [dow, 2]}, {"$lte": [dow, 6]}]},
                {"$cond": ["$$l.half_day", 0.5, 1]},
                0,
            ]}),
            "hours_sum": over_logs({"$cond": [has_hours, "$$l.work_hours", 0]}),
            "hours_n": over_logs({"$cond": [has_hours, 1, 0]}),
            "late_count": over_logs({"$cond": [{"$gt": ["$$l.late_minutes", 0]}, 1, 0]}),
            "late_total": over_logs({"$ifNull": ["$$l.late_minutes", 0]}),
            "leave_count": over_logs({"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]}),
            "on_duty_count": over_logs({"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]}),
        }},
        
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "hours_sum": {"$sum": "$hours_sum"},
            "hours_n": {"$sum": "$hours_n"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$late_total"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard_pipeline(start, next_start, limit, department=None):
    pipeline = [
        # this month's records
        {"$match": {"date": {"$gte": start, "$lt": next_start}}},
        
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        
        {"$match": {"total_late_minutes": {"$gt": 0}}},
       
        {"$lookup": {"from": "employees", "localField": "_id",
                     "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ]
    return pipeline


def build_department_trend_pipeline(department: str, start_date: Date, end_date: Date):
    """Database-side daily aggregation, gap filling and rolling average."""
    start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    end_exclusive = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    start_s, end_s = start_date.isoformat(), (end_date + timedelta(days=1)).isoformat()
    return [
        {"$match": {"date": {"$gte": start_s, "$lt": end_s}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "pipeline": [{"$match": {"department": department}}], "as": "_emp"}},
        {"$unwind": "$_emp"},
        {"$group": {
            "_id": {"$dateFromString": {"dateString": "$date"}},
            "present_count": {"$sum": {"$cond": [{"$in": ["$status", PRESENT_STATUSES]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        # Add one seed row so the requested date range still exists when there are no logs.
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}}, {"$limit": 1},
            {"$project": {"_id": 0, "_day": {"$literal": start_dt}, "present_count": {"$literal": 0}, "late_count": {"$literal": 0}}},
            {"$project": {"_id": "$_day", "present_count": 1, "late_count": 1}},
        ]}},
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_exclusive]}}},
        {"$set": {"present_count": {"$ifNull": ["$present_count", 0]}, "late_count": {"$ifNull": ["$late_count", 0]}, "date": {"$dateToString": {"date": "$_id", "format": "%Y-%m-%d", "timezone": "UTC"}}, "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$_id"}, 2]}, {"$lte": [{"$dayOfWeek": "$_id"}, 6]}]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"department": department}}, {"$match": {"$expr": {"$lte": ["$joined_on", "$$day"]}}},
            {"$count": "n"},
        ], "as": "_headcount"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$_headcount.n", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"_id": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"_id": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    # 1. the employee must exist (a bad month is already a 422)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

   
    working_days = count_working_days(month, emp["joined_on"])

    
    start, next_start = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(build_employee_monthly_pipeline(emp_code, start, next_start)))

    
    if rows:
        s = rows[0]
    else:
        s = {"present_days": 0, "leave_days": 0, "late_count": 0,
             "total_late_minutes": 0, "total_overtime_minutes": 0}

    
    present_days = round_half_up(s["present_days"])
    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up(Decimal(str(present_days)) / Decimal(working_days) * 100)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": s["leave_days"],
        "late_count": s["late_count"],
        "total_late_minutes": s["total_late_minutes"],
        "total_overtime_minutes": s["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=MONTH_PATTERN),
    department: Optional[str] = None,
):
    start, next_start = month_bounds(month)
    pipeline = build_department_summary_pipeline(start, next_start, department)

    items = []
    for row in db.employees.aggregate(pipeline):
        if row["hours_n"] > 0:
            avg = round_half_up(Decimal(str(round(row["hours_sum"], 6))) / Decimal(row["hours_n"]))
        else:
            avg = None
        items.append({
            "department": row["_id"],
            "headcount": row["headcount"],
            "present_days": round_half_up(row["present_days"]),
            "avg_work_hours": avg,
            "late_count": row["late_count"],
            "total_late_minutes": row["total_late_minutes"],
            "leave_count": row["leave_count"],
            "on_duty_count": row["on_duty_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, next_start = month_bounds(month)
    pipeline = build_late_leaderboard_pipeline(start, next_start, limit, department)
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    if from_date is None or to_date is None:
        raise HTTPException(422, "from and to are required")
    if not valid_date_string(from_date) or not valid_date_string(to_date):
        raise HTTPException(422, "from and to must be YYYY-MM-DD dates")
    start_date, end_date = Date.fromisoformat(from_date), Date.fromisoformat(to_date)
    days = (end_date - start_date).days
    if days < 0:
        raise HTTPException(422, "to must be on or after from")
    if days >= 92:
        raise HTTPException(422, "date range must not exceed 92 calendar days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")

    pipeline = build_department_trend_pipeline(department, start_date, end_date)
    items = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    for item in items:
        item["present_count"] = round_half_up(item.get("present_count", 0), 2)
        rate = item.get("attendance_rate")
        avg = item.get("moving_avg_7d")
        item["attendance_rate"] = None if rate is None else round_half_up(rate, 4)
        item["moving_avg_7d"] = None if avg is None else round_half_up(avg, 4)
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def explain_command(collection_name, pipeline=None, query=None, sort=None, skip=0, limit=None):
    if pipeline is not None:
        command = {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}
    else:
        command = {"find": collection_name, "filter": query or {}}
        if sort:
            command["sort"] = sort
        if skip:
            command["skip"] = skip
        if limit is not None:
            command["limit"] = limit
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result))  # make MongoDB-specific types JSON friendly


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        explain = explain_command("attendance_logs", query=q, sort={"date": -1, "emp_code": 1},
                                  skip=(page - 1) * page_size, limit=page_size)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if not emp_code or not month or not re.fullmatch(MONTH_PATTERN, month):
            raise HTTPException(422, "emp_code and valid month are required")
        start, next_start = month_bounds(month)
        pipeline = build_employee_monthly_pipeline(emp_code, start, next_start)
        explain = explain_command("attendance_logs", pipeline=pipeline)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "department_summary":
        if not month or not re.fullmatch(MONTH_PATTERN, month):
            raise HTTPException(422, "valid month is required")
        start, next_start = month_bounds(month)
        pipeline = build_department_summary_pipeline(start, next_start, department)
        explain = explain_command("employees", pipeline=pipeline)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain}

    if endpoint == "late_leaderboard":
        if not month or not re.fullmatch(MONTH_PATTERN, month):
            raise HTTPException(422, "valid month is required")
        start, next_start = month_bounds(month)
        pipeline = build_late_leaderboard_pipeline(start, next_start, limit, department)
        explain = explain_command("attendance_logs", pipeline=pipeline)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    # department_trend
    if not department or not from_date or not to_date or not valid_date_string(from_date) or not valid_date_string(to_date):
        raise HTTPException(422, "department, from and to are required and must be valid dates")
    start_date, end_date = Date.fromisoformat(from_date), Date.fromisoformat(to_date)
    if (end_date - start_date).days < 0 or (end_date - start_date).days >= 92:
        raise HTTPException(422, "invalid date range (maximum 92 days)")
    pipeline = build_department_trend_pipeline(department, start_date, end_date)
    explain = explain_command("attendance_logs", pipeline=pipeline)
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
