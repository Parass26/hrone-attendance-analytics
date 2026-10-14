"""
Employee Attendance & Analytics API - STARTER
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
import logging
import re
from decimal import Decimal, ROUND_HALF_UP
from bson.json_util import dumps
import json
load_dotenv()


client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000,
)


db = client[os.getenv("MONGO_DB", "attendance_db")]


IST = timezone(timedelta(hours=5, minutes=30))


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes():
    try:
        db.employees.create_index("emp_code", unique=True)
        db.attendance_logs.create_index(
            [("emp_code", 1), ("date", 1)],
            unique=True,
        )
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        # The unique compound index above already covers (emp_code, date);
        # do not recreate the same key pattern as a non-unique index.
        db.attendance_logs.create_index([("date", 1), ("late_minutes", 1), ("emp_code", 1)])
        db.employees.create_index([("department", 1), ("joined_on", 1), ("emp_code", 1)])
        # Department summary without a department filter matches on joined_on only.
        db.employees.create_index([("joined_on", 1)])
    except Exception:
        # Keep the app up so /health can report 503 instead of the process dying.
        logging.getLogger("uvicorn.error").exception("Index creation failed")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def as_utc(value: datetime) -> datetime:
    # PyMongo may return naive datetimes representing UTC.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    # Integer arithmetic: int(timestamp() * 1000) can be off by 1 ms (float error).
    return (as_utc(value) - _EPOCH) // timedelta(milliseconds=1)


def attendance_date_for(ts: datetime, shift_start: str, shift_end: str) -> date:
    """R1: IST date of the punch-in; early punch-ins of an overnight shift
    belong to the shift that started the previous day."""
    local = as_utc(ts).astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    day = local.date()
    if (end_h, end_m) <= (start_h, start_m) and (local.hour, local.minute) < (end_h, end_m):
        day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    local_in = as_utc(punch_in).astimezone(IST)
    h, m = map(int, shift_start.split(":"))

    # Only an overnight shift's early-morning punch-in belongs to the shift that
    # started the previous day. (A day shift punch-in before shift_start is
    # simply early, not "late by almost a day".)
    shift_day = attendance_date_for(punch_in, shift_start, shift_end)
    shift_start_dt = datetime(
        shift_day.year, shift_day.month, shift_day.day, h, m, tzinfo=IST
    )

    elapsed_seconds = (as_utc(local_in) - as_utc(shift_start_dt)).total_seconds()
    if elapsed_seconds <= 10 * 60:
        return 0
    return int(elapsed_seconds // 60)


def calculate_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    seconds = (as_utc(punch_out) - as_utc(punch_in)).total_seconds()
    hours = Decimal(str(seconds)) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def calculate_overtime(
    punch_in: datetime,
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
) -> int:
    local_in = as_utc(punch_in).astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    shift_date = local_in.date()
    # An after-midnight punch-in for an overnight shift belongs
    # to the shift that started on the previous day.
    if (
        (end_h, end_m) <= (start_h, start_m)
        and (local_in.hour, local_in.minute) < (end_h, end_m)
    ):
        shift_date -= timedelta(days=1)
    shift_end_dt = datetime(
        shift_date.year,
        shift_date.month,
        shift_date.day,
        end_h,
        end_m,
        tzinfo=IST,
    )
    if (end_h, end_m) <= (start_h, start_m):
        shift_end_dt += timedelta(days=1)
    elapsed_minutes = int(
        (as_utc(punch_out) - as_utc(shift_end_dt)).total_seconds() // 60
    )
    return elapsed_minutes if elapsed_minutes >= 30 else 0


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str
    @field_validator("emp_code", "name", "email", "department", mode="before")
    @classmethod
    def validate_non_empty(cls, value):
        if not isinstance(value, str):
            # Let pydantic's own type check return a 422 (was: AttributeError -> 500).
            return value
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be empty")
        return value
    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("Time must use HH:MM format")
        return value
    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value):
        try:
            # Round-trip so "2024-1-5" (accepted by strptime) is rejected; dates are
            # compared as strings everywhere, so they must be zero-padded.
            if datetime.strptime(value, "%Y-%m-%d").date().isoformat() != value:
                raise ValueError
        except ValueError:
            raise ValueError("joined_on must use YYYY-MM-DD format")
        return value


class PunchInIn(BaseModel):
    emp_code: str
    # Strict epoch milliseconds: floats, strings and second-based values are 422.
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)


class AttendanceCorrectionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str
    regularized_by: str
    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value):
        value = value.strip()
        if not 5 <= len(value) <= 200:
            raise ValueError("reason must be between 5 and 200 characters")
        return value
    @field_validator("regularized_by")
    @classmethod
    def validate_regularized_by(cls, value):
        value = value.strip()
        if not 1 <= len(value) <= 50:
            raise ValueError("regularized_by must be between 1 and 50 characters")
        return value
    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        allowed = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
        if value is not None and value not in allowed:
            raise ValueError("Invalid attendance status")
        return value


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
def _ms_values(value):
    """Recursively turn datetimes into epoch ms (used for history entries)."""
    if isinstance(value, datetime):
        return epoch_ms(value)
    if isinstance(value, dict):
        return {k: _ms_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_ms_values(v) for v in value]
    return value


def serialize_attendance(doc: dict) -> dict:
    """Stored attendance document -> API shape (no _id, instants in epoch ms)."""
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["punch_in"] = epoch_ms(out.get("punch_in"))
    out["punch_out"] = epoch_ms(out.get("punch_out"))
    out["half_day"] = bool(out.get("half_day", False))  # seeded docs may omit it
    out["history"] = _ms_values(out.get("history") or [])  # ... or history
    return out


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )
    doc.pop("_id", None)
    doc["created_at"] = epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page must be >= 1 and page_size must be between 1 and 100",
        )
    query = {}
    if department is not None:
        query["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(query)
    items = list(
        db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    )
    for item in items:
        if item.get("created_at") is not None:
            item["created_at"] = epoch_ms(item["created_at"])
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if body.punched_at is not None:
        try:
            ts = datetime.fromtimestamp(
                body.punched_at / 1000,
                tz=timezone.utc,
            ).astimezone(IST)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(
                status_code=422,
                detail="Invalid punched_at timestamp",
            )
    else:
        ts = datetime.now(IST)
    ts = ts.replace(microsecond=0)
    local_ts = ts.astimezone(IST)
    date_str = attendance_date_for(local_ts, emp["shift_start"], emp["shift_end"]).isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts,
            emp["shift_start"],
            emp["shift_end"],
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Attendance already exists for this employee on this date",
        )
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    if body.punched_at is None:
        ts = datetime.now(IST)
    else:
        try:
            ts = datetime.fromtimestamp(
                body.punched_at / 1000,
                tz=timezone.utc,
            ).astimezone(IST)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(
                status_code=422,
                detail="Invalid punched_at timestamp",
            )
    # Store and calculate using whole-second precision.
    ts = ts.replace(microsecond=0)
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": ts},
        },
        sort=[("punch_in", -1)],
    )
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No matching punch-in record found",
        )
    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record is already punched out",
        )
    punch_in_time = as_utc(record["punch_in"])
    punch_out_time = as_utc(ts)
    elapsed = (punch_out_time - punch_in_time).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in and within 24 hours",
        )
    work_hours = calculate_work_hours(record["punch_in"], ts)
    overtime_minutes = calculate_overtime(
        record["punch_in"],
        ts,
        emp["shift_start"],
        emp["shift_end"],
    )
    half_day = work_hours < 4.50
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )
    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record was already punched out",
        )
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated)


@app.patch("/attendance/{emp_code}/{date}")
def correct_attendance(
    emp_code: str,
    date: str,
    body: AttendanceCorrectionIn,
):
    # A concurrent correction makes the conditional update below miss; re-read the
    # fresh record and recompute instead of failing, so every correction lands and
    # appends its own history entry.
    for _ in range(10):
        result = _apply_correction(emp_code, date, body)
        if result is not None:
            return result
    raise HTTPException(
        status_code=409,
        detail="Attendance record changed concurrently; retry the correction",
    )


def _apply_correction(emp_code: str, date: str, body: AttendanceCorrectionIn):
    """One read-compute-write attempt. Returns None if the record changed meanwhile."""
    # Validate the requested attendance date.
    try:
        attendance_date = datetime.strptime(date, "%Y-%m-%d").date()
        if attendance_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must use YYYY-MM-DD format",
        )
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="Attendance record not found")
    supplied = body.model_fields_set
    allowed_statuses = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
    new_status = body.status if "status" in supplied else record["status"]
    if new_status not in allowed_statuses:
        raise HTTPException(status_code=422, detail="Invalid attendance status")
    new_in = record.get("punch_in")
    new_out = record.get("punch_out")
    # Convert supplied epoch-millisecond timestamps to datetimes.
    for field in ("punch_in", "punch_out"):
        if field not in supplied:
            continue
        value = getattr(body, field)
        if value is None:
            raise HTTPException(status_code=422, detail=f"{field} cannot be null when supplied")
        if value < 100000000000 or value > 4102444800000:
            raise HTTPException(
                status_code=422,
                detail=f"{field} must be an epoch-millisecond timestamp",
            )
        try:
            parsed = datetime.fromtimestamp(
                value / 1000, tz=timezone.utc
            ).astimezone(IST).replace(microsecond=0)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(status_code=422, detail=f"Invalid {field} timestamp")
        if field == "punch_in":
            new_in = parsed
        else:
            new_out = parsed
    # ABSENT and LEAVE cannot have punch times.
    if new_status in {"ABSENT", "LEAVE"}:
        if (
            ("punch_in" in supplied and body.punch_in is not None)
            or ("punch_out" in supplied and body.punch_out is not None)
        ):
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE cannot have punch times",
            )
        new_in = None
        new_out = None
    elif new_in is None:
        raise HTTPException(status_code=422, detail="Presence statuses require punch_in")
    # Punch-in must belong to the requested attendance date (R1, overnight-aware).
    if (
        new_in is not None
        and attendance_date_for(new_in, emp["shift_start"], emp["shift_end"]) != attendance_date
    ):
        raise HTTPException(
            status_code=422,
            detail="punch_in must belong to the attendance date",
        )
    if new_out is not None:
        if new_in is None:
            raise HTTPException(status_code=422, detail="punch_out requires punch_in")
        elapsed = (as_utc(new_out) - as_utc(new_in)).total_seconds()
        if elapsed <= 0 or elapsed > 24 * 60 * 60:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in and within 24 hours",
            )
    # Record only fields that actually changed.
    changes = {}
    if new_status != record["status"]:
        changes["status"] = {"from": record["status"], "to": new_status}
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")
    old_in_utc = as_utc(old_in) if old_in is not None else None
    new_in_utc = as_utc(new_in) if new_in is not None else None
    old_out_utc = as_utc(old_out) if old_out is not None else None
    new_out_utc = as_utc(new_out) if new_out is not None else None
    if old_in_utc != new_in_utc:
        changes["punch_in"] = {"from": old_in_utc, "to": new_in_utc}
    if old_out_utc != new_out_utc:
        changes["punch_out"] = {"from": old_out_utc, "to": new_out_utc}
    # Recalculate derived values.
    late_minutes = (
        compute_late_minutes(new_in, emp["shift_start"], emp["shift_end"]) if new_in else 0
    )
    if new_in is not None and new_out is not None:
        work_hours = calculate_work_hours(new_in, new_out)
        overtime_minutes = calculate_overtime(
            new_in, new_out, emp["shift_start"], emp["shift_end"]
        )
        half_day = work_hours < 4.50
    else:
        work_hours = None
        overtime_minutes = 0
        half_day = False
    old_derived = {
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    new_derived = {
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }
    for field_name, new_value in new_derived.items():
        old_value = old_derived[field_name]
        if old_value != new_value:
            changes[field_name] = {"from": old_value, "to": new_value}
    if not changes:
        raise HTTPException(status_code=422, detail="Correction does not change any fields")
    correction = {
        "at": datetime.now(timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # Conditional update prevents a concurrent correction from being lost.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": record["status"],
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                "work_hours": work_hours,
                "late_minutes": late_minutes,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            },
            "$push": {"history": correction},
        },
    )
    if result.modified_count != 1:
        return None
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_attendance(updated)


ATTENDANCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}


def check_paging(page: int, page_size: int):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page must be >= 1 and page_size must be between 1 and 100",
        )


def build_attendance_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict:
    """Validated filter for GET /attendance (shared with the explain endpoint)."""
    for field_name, value in (("date_from", date_from), ("date_to", date_to)):
        if value is not None:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError
            except ValueError:
                raise HTTPException(status_code=422, detail=f"{field_name} must use YYYY-MM-DD format")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
    if status is not None and status not in ATTENDANCE_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid status")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    check_paging(page, page_size)
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    docs = [serialize_attendance(doc) for doc in docs]
    return {
        "items": docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Analytics: pipeline builders are shared by the endpoints and /admin/explain so
# the explain output is always for exactly the query the endpoint runs.
# --------------------------------------------------------------------------- #
def parse_month(month: str):
    """'YYYY-MM' -> (first day of the month, first day of the next month)."""
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
            raise ValueError
        year, month_number = int(month[:4]), int(month[5:])
        if not 1 <= month_number <= 12:
            raise ValueError
        start = date(year, month_number, 1)
        next_month = (
            date(year + 1, 1, 1) if month_number == 12 else date(year, month_number + 1, 1)
        )
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )
    return start, next_month


def parse_trend_range(from_date: str, to_date: str) -> int:
    """Validate the trend range and return the number of calendar days in it."""
    try:
        start_date = date.fromisoformat(from_date)
        end_date = date.fromisoformat(to_date)
        if start_date.isoformat() != from_date or end_date.isoformat() != to_date:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="from and to must use YYYY-MM-DD format")
    if end_date < start_date:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    day_count = (end_date - start_date).days + 1
    if day_count > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")
    return day_count


def build_monthly_pipeline(emp_code: str, month_start: date, next_month: date) -> list:
    attendance_pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat(),
                },
            }
        },
        {
            "$addFields": {
                "weekday": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$gte": ["$weekday", 2]},
                                    {"$lte": ["$weekday", 6]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]
    return attendance_pipeline


def build_summary_pipeline(month_start: date, next_month: date, department: Optional[str]) -> list:
    month_end = next_month - timedelta(days=1)
    # Include employees who joined on or before the month's last day.
    employee_match = {"joined_on": {"$lte": month_end.isoformat()}}
    if department:
        employee_match["department"] = department
    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$eq": ["$emp_code", "$$employee_code"]
                            },
                            "date": {
                                "$gte": month_start.isoformat(),
                                "$lt": next_month.isoformat(),
                            },
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {
                                                    "$gte": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$date"
                                                                }
                                                            }
                                                        },
                                                        2,
                                                    ]
                                                },
                                                {
                                                    "$lte": [
                                                        {
                                                            "$dayOfWeek": {
                                                                "$dateFromString": {
                                                                    "dateString": "$date"
                                                                }
                                                            }
                                                        },
                                                        6,
                                                    ]
                                                },
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "LEAVE"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "ON_DUTY"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "monthly",
            }
        },
        {
            "$unwind": {
                "path": "$monthly",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {
                    "$sum": {"$ifNull": ["$monthly.present_days", 0]}
                },
                "work_hours_sum": {
                    "$sum": {"$ifNull": ["$monthly.work_hours_sum", 0]}
                },
                "work_hours_count": {
                    "$sum": {"$ifNull": ["$monthly.work_hours_count", 0]}
                },
                "late_count": {
                    "$sum": {"$ifNull": ["$monthly.late_count", 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$monthly.total_late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$ifNull": ["$monthly.leave_count", 0]}
                },
                "on_duty_count": {
                    "$sum": {"$ifNull": ["$monthly.on_duty_count", 0]}
                },
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {
                            "$divide": [
                                {"$floor": {"$add": [{"$multiply": [{"$divide": ["$work_hours_sum", "$work_hours_count"]}, 100]}, 0.5]}},
                                100,
                            ]
                        },
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]
    return pipeline


def build_leaderboard_pipeline(
    month_start: date, next_month: date, limit: int, department: Optional[str]
) -> list:
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lt": next_month.isoformat(),
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": 1},
            }
        },
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department:
        pipeline.append(
            {"$match": {"employee.department": department}}
        )
    # Competition ranking: 1, 2, 2, 4.
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}}
                    },
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                    "rank": 1,
                }
            },
        ]
    )
    return pipeline


def build_trend_pipeline(department: str, from_date: str, day_count: int) -> list:
    # Build one row per requested day inside MongoDB, including days without logs.
    pipeline = [
        {"$match": {"department": department}},
        {"$group": {"_id": "$department", "employees": {"$push": {"emp_code": "$emp_code", "joined_on": "$joined_on"}}}},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "employees": 1,
            "days": {"$map": {
                "input": {"$range": [0, day_count]},
                "as": "offset",
                "in": {"$dateToString": {
                    "date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": from_date}}, "unit": "day", "amount": "$$offset"}},
                    "format": "%Y-%m-%d", "timezone": "UTC"
                }}
            }}
        }},
        {"$unwind": "$days"},
        {"$set": {
            "date": "$days",
            "eligible_codes": {"$map": {
                "input": {"$filter": {"input": "$employees", "as": "e", "cond": {"$lte": ["$$e.joined_on", "$days"]}}},
                "as": "e", "in": "$$e.emp_code"
            }},
            "headcount": {"$size": {"$filter": {"input": "$employees", "as": "e", "cond": {"$lte": ["$$e.joined_on", "$days"]}}}},
            "weekday": {"$dayOfWeek": {"$dateFromString": {"dateString": "$days"}}}
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"day": "$date", "codes": "$eligible_codes"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day"]}, {"$in": ["$emp_code", "$$codes"]}]}}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}
                }}
            ],
            "as": "daily"
        }},
        {"$set": {
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]},
            "is_working_day": {"$and": [{"$gte": ["$weekday", 2]}, {"$lte": ["$weekday", 6]}]}
        }},
        {"$set": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": [{"$floor": [{"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}]}, 10000]},
                None
            ]}
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"moving_avg_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}
        }},
        {"$project": {
            "_id": 0, "department": 0, "employees": 0, "days": 0, "eligible_codes": 0, "weekday": 0, "daily": 0
        }},
        {"$set": {"moving_avg_7d": {"$cond": [
            {"$eq": ["$moving_avg_raw", None]}, None,
            {"$divide": [{"$floor": [{"$add": [{"$multiply": ["$moving_avg_raw", 10000]}, 0.5]}]}, 10000]}
        ]}}},
        {"$project": {"date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}}
    ]
    return pipeline


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(emp_code: str, month: str):
    month_start, next_month = parse_month(month)
    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    )
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    # R7: Mon-Fri only, counted from the later of the joining date and month start.
    joined_on = date.fromisoformat(employee["joined_on"])
    working_days = 0
    current_day = max(month_start, joined_on)
    while current_day < next_month:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)
    totals_result = list(
        db.attendance_logs.aggregate(build_monthly_pipeline(emp_code, month_start, next_month))
    )
    totals = totals_result[0] if totals_result else {}
    present_days = float(totals.get("present_days", 0))
    attendance_pct = None
    if working_days > 0:
        # Decimal arithmetic so the R8 half-up rounding is not hit by float error.
        attendance_pct = float(
            (Decimal(str(present_days)) * 100 / working_days).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": totals.get("leave_days", 0),
        "late_count": totals.get("late_count", 0),
        "total_late_minutes": totals.get("total_late_minutes", 0),
        "total_overtime_minutes": totals.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    month_start, next_month = parse_month(month)
    pipeline = build_summary_pipeline(month_start, next_month, department)
    return {"month": month, "items": list(db.employees.aggregate(pipeline))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
):
    month_start, next_month = parse_month(month)
    if not 1 <= limit <= 50:
        raise HTTPException(
            status_code=422,
            detail="limit must be between 1 and 50",
        )
    pipeline = build_leaderboard_pipeline(month_start, next_month, limit, department)
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipeline))}


@app.get("/analytics/departments/{department}/trend")
def department_attendance_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    day_count = parse_trend_range(from_date, to_date)
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")
    pipeline = build_trend_pipeline(department, from_date, day_count)
    return {"department": department, "items": list(db.employees.aggregate(pipeline))}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    # Each branch builds the command exactly like the real endpoint does.
    if endpoint == "attendance_list":
        check_paging(page, page_size)
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": build_attendance_query(emp_code, date_from, date_to, status),
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        month_start, next_month = parse_month(month)
        if db.employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
            raise HTTPException(status_code=404, detail="Employee not found")
        collection = "attendance_logs"
        command = {
            "aggregate": collection,
            "pipeline": build_monthly_pipeline(emp_code, month_start, next_month),
            "cursor": {},
        }
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        month_start, next_month = parse_month(month)
        collection = "employees"
        command = {
            "aggregate": collection,
            "pipeline": build_summary_pipeline(month_start, next_month, department),
            "cursor": {},
        }
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        month_start, next_month = parse_month(month)
        if not 1 <= limit <= 50:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 50")
        collection = "attendance_logs"
        command = {
            "aggregate": collection,
            "pipeline": build_leaderboard_pipeline(month_start, next_month, limit, department),
            "cursor": {},
        }
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from and to are required",
            )
        day_count = parse_trend_range(from_date, to_date)
        if db.employees.count_documents({"department": department}) == 0:
            raise HTTPException(status_code=404, detail="Department not found")
        collection = "employees"
        command = {
            "aggregate": collection,
            "pipeline": build_trend_pipeline(department, from_date, day_count),
            "cursor": {},
        }
    else:
        raise HTTPException(
            status_code=422,
            detail=(
                "endpoint must be one of: attendance_list, employee_monthly, "
                "department_summary, late_leaderboard, department_trend"
            ),
        )
    explain = db.command("explain", command, verbosity="executionStats")
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(dumps(explain)),
    }
