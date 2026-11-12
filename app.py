"""Flask API for the Exam Allocation Manager.

Run locally with ``pip install -r requirements.txt && python app.py``.
Uploaded CSV tables are persisted in SQLite. This is a local prototype and
intentionally binds only to 127.0.0.1; it has no authentication layer.
"""
from __future__ import annotations

from collections import defaultdict
from io import BytesIO, StringIO
import csv
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from allocation_engine import allocate as optimize_allocation

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
DB_PATH = Path(os.environ.get("EXAM_MANAGER_DB", str(INSTANCE_DIR / "exam_manager.sqlite3"))).resolve()
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
KINDS = ("students", "rooms", "proctors", "equipment")
REQUIRED = {
    "students": [
        "candidate_id", "candidate_name", "subject", "paper", "date",
        "start_time", "duration_min", "extra_time_min", "equipment",
    ],
    "rooms": [
        "room_id", "capacity", "unusable_seats", "available_from",
        "available_to", "room_type", "status", "notes",
    ],
    "proctors": ["proctor_id", "proctor_name", "available_from", "available_to", "subjects_taught"],
    "equipment": ["equipment_type", "quantity"],
}
OPTIONAL_STUDENT_COLUMNS = ["separate_room", "required_room_type", "reader", "scribe", "rest_break_min"]
TABLES: dict[str, pd.DataFrame] = {}

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _init_db() -> None:
    with _connect() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS uploaded_tables ("
            "kind TEXT PRIMARY KEY, columns_json TEXT NOT NULL, rows_json TEXT NOT NULL, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )


def _load_tables() -> None:
    TABLES.clear()
    _init_db()
    with _connect() as connection:
        records = connection.execute("SELECT kind, columns_json, rows_json FROM uploaded_tables").fetchall()
    for record in records:
        kind = record["kind"]
        if kind in KINDS:
            columns = json.loads(record["columns_json"])
            rows = json.loads(record["rows_json"])
            TABLES[kind] = pd.DataFrame(rows, columns=columns).fillna("").astype(str)


def _save_table(kind: str, frame: pd.DataFrame) -> None:
    columns = list(frame.columns)
    rows = frame.fillna("").astype(str).to_dict(orient="records")
    # A single SQLite statement ensures readers see either the prior table or
    # the complete replacement, never a partially written upload.
    with _connect() as connection:
        connection.execute(
            "INSERT INTO uploaded_tables(kind, columns_json, rows_json, updated_at) "
            "VALUES(?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(kind) DO UPDATE SET columns_json=excluded.columns_json, "
            "rows_json=excluded.rows_json, updated_at=CURRENT_TIMESTAMP",
            (kind, json.dumps(columns), json.dumps(rows)),
        )


def _delete_tables() -> None:
    with _connect() as connection:
        connection.execute("DELETE FROM uploaded_tables")
    TABLES.clear()


def _normal(value: Any) -> str:
    return "" if value is None else str(value).strip().casefold()


def _minute(value: Any) -> int:
    hour, minute = map(int, str(value).strip().split(":"))
    return hour * 60 + minute


def _valid_time(value: Any) -> bool:
    raw = str(value).strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", raw):
        return False
    hour, minute = map(int, raw.split(":"))
    return 0 <= hour < 24 and 0 <= minute < 60


def _whole_number(value: Any, *, allow_zero: bool = False) -> bool:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return False
    return number.is_integer() and (number >= 0 if allow_zero else number > 0)


def _true(value: Any) -> bool:
    return _normal(value) in {"1", "true", "yes", "y", "required"}


def _normalise_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize CSV header/value whitespace consistently before validation."""
    result = frame.copy()
    result.columns = [str(column).strip().casefold() for column in result.columns]
    for column in result.columns:
        result[column] = result[column].map(lambda value: "" if pd.isna(value) else str(value).strip())
    return result


def validate(kind: str, frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Validate one uploaded table. Report CSV line numbers (header is row 1)."""
    if kind not in REQUIRED:
        return [{"row": 1, "message": "Unknown table type."}]
    if not frame.columns.is_unique:
        return [{"row": 1, "message": "Column names must be unique after trimming and case normalization."}]

    missing = [column for column in REQUIRED[kind] if column not in frame.columns]
    if missing:
        return [{"row": 1, "message": f"Missing required column: {column}"} for column in missing]
    if frame.empty:
        return [{"row": 1, "message": "The CSV contains a header but no data rows."}]

    non_empty = {
        "students": ["candidate_id", "candidate_name", "subject", "paper", "date", "start_time", "duration_min"],
        "rooms": ["room_id", "capacity", "available_from", "available_to", "room_type", "status"],
        "proctors": REQUIRED["proctors"],
        "equipment": REQUIRED["equipment"],
    }[kind]
    key_columns = {
        "students": ["candidate_id", "subject", "paper", "date"],
        "rooms": ["room_id"],
        "proctors": ["proctor_id"],
        "equipment": ["equipment_type"],
    }[kind]
    errors: list[dict[str, Any]] = []

    for index, row in frame.iterrows():
        line = int(index) + 2
        for column in non_empty:
            if not str(row.get(column, "")).strip():
                errors.append({"row": line, "message": f"{column} is required."})
        if any(not str(row.get(column, "")).strip() for column in key_columns):
            errors.append({"row": line, "message": "An identifier field is empty."})

        if kind in {"rooms", "proctors"}:
            valid_times = True
            for column in ("available_from", "available_to"):
                if not _valid_time(row.get(column, "")):
                    valid_times = False
                    errors.append({"row": line, "message": f"{column} must be a valid 24-hour HH:MM time."})
            if valid_times and _minute(row["available_from"]) >= _minute(row["available_to"]):
                errors.append({"row": line, "message": "Availability end must be after start; overnight availability is not supported."})

        if kind == "students":
            date_value = str(row.get("date", "")).strip()
            valid_date = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_value))
            if valid_date:
                valid_date = not pd.isna(pd.to_datetime(date_value, format="%Y-%m-%d", errors="coerce"))
            if not valid_date:
                errors.append({"row": line, "message": "date must be a real date in YYYY-MM-DD format."})

            start_valid = _valid_time(row.get("start_time", ""))
            if not start_valid:
                errors.append({"row": line, "message": "start_time must be a valid 24-hour HH:MM time."})
            duration_valid = _whole_number(row.get("duration_min", ""))
            if not duration_valid:
                errors.append({"row": line, "message": "duration_min must be a positive whole number of minutes."})

            extra_value = str(row.get("extra_time_min", "")).strip() or "0"
            if not _whole_number(extra_value, allow_zero=True):
                errors.append({"row": line, "message": "extra_time_min must be a whole number of minutes that is zero or greater."})
            rest_value = str(row.get("rest_break_min", "")).strip() or "0"
            if not _whole_number(rest_value, allow_zero=True):
                errors.append({"row": line, "message": "rest_break_min must be a whole number of minutes that is zero or greater."})
            if start_valid and duration_valid and _whole_number(extra_value, allow_zero=True) and _whole_number(rest_value, allow_zero=True):
                finish = _minute(row["start_time"]) + int(float(row["duration_min"])) + int(float(extra_value)) + int(float(rest_value))
                if finish > 24 * 60:
                    errors.append({"row": line, "message": "The sitting, extra time and rest-break allowance must finish by midnight."})

            for column in ("separate_room", "reader", "scribe"):
                value = _normal(row.get(column, ""))
                if column in frame.columns and value and value not in {
                    "1", "0", "true", "false", "yes", "no", "y", "n", "required", "none"
                }:
                    errors.append({"row": line, "message": f"{column} must be blank, yes/no, true/false, or 1/0."})

        if kind == "rooms":
            capacity = row.get("capacity", "")
            if not _whole_number(capacity):
                errors.append({"row": line, "message": "capacity must be a positive whole number of seats."})
            unusable = str(row.get("unusable_seats", "")).strip() or "0"
            if not _whole_number(unusable, allow_zero=True):
                errors.append({"row": line, "message": "unusable_seats must be a whole number from zero to capacity."})
            elif _whole_number(capacity) and int(float(unusable)) > int(float(capacity)):
                errors.append({"row": line, "message": "unusable_seats cannot exceed capacity."})
            if _normal(row.get("status", "")) not in {"available", "maintenance", "unusable"}:
                errors.append({"row": line, "message": "status must be available, maintenance, or unusable."})

        if kind == "equipment" and not _whole_number(row.get("quantity", ""), allow_zero=True):
            errors.append({"row": line, "message": "quantity must be a whole number that is zero or greater."})

    # Detect duplicate identifiers without being fooled by whitespace or letter case.
    seen: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for index, row in frame.iterrows():
        normalized_key = tuple(_normal(row.get(column, "")) for column in key_columns)
        if all(normalized_key):
            seen[normalized_key].append(int(index) + 2)
    for lines in seen.values():
        if len(lines) > 1:
            for line in lines:
                errors.append({"row": line, "message": "Duplicate identifier/key (identifiers are case-insensitive)."})

    if kind == "students":
        # Candidates sitting the same paper together must have the same standard
        # duration; individual extra time belongs in extra_time_min instead.
        sessions: dict[tuple[str, str, str, int], list[tuple[int, int]]] = defaultdict(list)
        for index, row in frame.iterrows():
            if not (_valid_time(row.get("start_time", "")) and _whole_number(row.get("duration_min", ""))):
                continue
            session_key = (
                str(row.get("date", "")).strip(),
                _normal(row.get("subject", "")),
                _normal(row.get("paper", "")),
                _minute(row["start_time"]),
            )
            sessions[session_key].append((int(index) + 2, int(float(row["duration_min"]))))
        for members in sessions.values():
            if len({duration for _line, duration in members}) > 1:
                for line, _duration in members:
                    errors.append({"row": line, "message": "Candidates in the same paper sitting must have the same duration_min; use extra_time_min for individual extra time."})
    return errors


def _cross_validate(kind: str, frame: pd.DataFrame) -> list[dict[str, Any]]:
    """Validate equipment dependencies when both related tables are available.

    This deliberately permits upload order to be arbitrary: a student table can
    be uploaded before the equipment table, and the full dependency is checked
    when both tables exist and again before allocation.
    """
    errors: list[dict[str, Any]] = []
    if kind == "students":
        student_frame = frame
        equipment_frame = TABLES.get("equipment")
    elif kind == "equipment":
        student_frame = TABLES.get("students")
        equipment_frame = frame
    else:
        return errors

    if student_frame is None or equipment_frame is None:
        return errors
    allowed = {_normal(value) for value in equipment_frame.get("equipment_type", [])}

    for index, row in student_frame.iterrows():
        line = int(index) + 2
        requested = _normal(row.get("equipment", "none"))
        if requested not in {"", "none", "n/a", "na", "null"} and requested not in allowed:
            errors.append({"row": line, "message": f"Unknown equipment type required by student: {row.get('equipment', '')}."})
        for column in ("reader", "scribe"):
            if column in student_frame.columns and _true(row.get(column, "")) and column not in allowed:
                errors.append({"row": line, "message": f"A student requires {column} support, but no '{column}' equipment type exists."})
    return errors


@app.errorhandler(HTTPException)
def handle_http_error(error: HTTPException):
    return jsonify({"error": error.description, "status": error.code}), error.code


@app.errorhandler(Exception)
def handle_unexpected_error(error: Exception):
    """Log unexpected failures and expose a local debugging detail.

    This app binds to 127.0.0.1. Keep EXAM_MANAGER_SHOW_ERRORS=0 if the app is
    ever moved to a shared or public environment. The full traceback is still
    written to the terminal log either way.
    """
    app.logger.exception("Unhandled request error")
    payload = {
        "error": "The server encountered an unexpected error.",
        "exception_type": type(error).__name__,
    }
    show_details = os.environ.get("EXAM_MANAGER_SHOW_ERRORS", "1").strip().casefold() not in {"0", "false", "no"}
    if show_details:
        payload["detail"] = str(error) or repr(error)
        payload["app_file"] = str(Path(__file__).resolve())
    return jsonify(payload), 500


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "Exam Allocation Manager API",
        "database": "sqlite",
        "app_file": str(Path(__file__).resolve()),
        "database_file": str(DB_PATH),
        "diagnostic_errors_enabled": os.environ.get("EXAM_MANAGER_SHOW_ERRORS", "1").strip().casefold() not in {"0", "false", "no"},
    })


@app.get("/api/schema")
def schema():
    return jsonify({
        "tables": REQUIRED,
        "optional_student_columns": OPTIONAL_STUDENT_COLUMNS,
        "student_row_definition": "One row represents one candidate sitting one paper.",
    })


@app.post("/api/upload/<kind>")
def upload(kind: str):
    if kind not in KINDS:
        return jsonify({"error": f"Unknown table. Choose one of: {', '.join(KINDS)}."}), 404
    file = request.files.get("file")
    if not file or not file.filename or not file.filename.lower().endswith(".csv"):
        return jsonify({"error": "Attach a CSV using the multipart field named 'file'."}), 400
    raw = file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return jsonify({"error": "CSV exceeds the 10 MB upload limit."}), 413

    try:
        # pandas may silently rename repeated CSV headings (for example, the
        # second "room_id" becomes "room_id.1"). Inspect the actual header first
        # so a duplicate column cannot slip through validation unnoticed.
        decoded = raw.decode("utf-8-sig")
        header = next(csv.reader(StringIO(decoded, newline="")), [])
        normalized_headers = [str(name).strip().casefold() for name in header]
        if len(normalized_headers) != len(set(normalized_headers)):
            return jsonify({
                "error": "CSV column names must be unique after trimming and case normalization.",
                "issues": [{"row": 1, "message": "Duplicate column name in CSV header."}],
                "valid": False,
            }), 422
        frame = pd.read_csv(BytesIO(raw), dtype=str, keep_default_na=False, encoding="utf-8-sig")
    except (UnicodeDecodeError, csv.Error, pd.errors.ParserError, pd.errors.EmptyDataError, ValueError) as exc:
        return jsonify({"error": f"Could not read CSV: {exc}"}), 400
    frame = _normalise_frame(frame)

    defaults: dict[str, dict[str, str]] = {
        "rooms": {"unusable_seats": "0", "status": "available", "notes": ""},
        "students": {"extra_time_min": "0", "equipment": "none", "rest_break_min": "0"},
    }
    for column, default in defaults.get(kind, {}).items():
        if column not in frame.columns:
            frame[column] = default

    issues = validate(kind, frame)
    issues.extend(_cross_validate(kind, frame))
    if not issues:
        _save_table(kind, frame)
        TABLES[kind] = frame

    return jsonify({
        "table": kind,
        "filename": Path(file.filename).name,
        "rows": len(frame),
        "columns": list(frame.columns),
        "valid": not issues,
        "issues": issues,
    }), (200 if not issues else 422)


@app.get("/api/data")
def data_summary():
    summary: dict[str, Any] = {}
    for kind in KINDS:
        if kind not in TABLES:
            summary[kind] = {"uploaded": False, "rows": 0, "issues": []}
            continue
        frame = TABLES[kind]
        summary[kind] = {
            "uploaded": True,
            "rows": len(frame),
            "issues": validate(kind, frame) + _cross_validate(kind, frame),
        }
    return jsonify(summary)


@app.post("/api/allocate")
def allocate():
    missing = [kind for kind in KINDS if kind not in TABLES]
    if missing:
        return jsonify({"error": "Upload all four tables first.", "missing": missing}), 409

    issues = {kind: validate(kind, TABLES[kind]) for kind in KINDS}
    # Validate student equipment requirements once, avoiding the same dependency
    # error appearing under both the student and equipment table headings.
    issues["students"].extend(_cross_validate("students", TABLES["students"]))
    if any(issues.values()):
        return jsonify({"error": "Fix validation issues before allocation.", "issues": issues}), 422

    raw_options = request.get_json(silent=True)
    if raw_options is None:
        raw_options = {}
    if not isinstance(raw_options, dict):
        return jsonify({"error": "Allocation options must be a JSON object."}), 400

    try:
        result = optimize_allocation(
            *(TABLES[kind].to_dict(orient="records") for kind in KINDS),
            options=raw_options,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        app.logger.exception("Allocation input could not be processed")
        return jsonify({"error": "Allocation could not be processed; check the uploaded tables.", "detail": str(exc)}), 422
    return jsonify(result)


@app.post("/api/reset")
def reset():
    """Clear uploaded tables from memory and SQLite (useful during development)."""
    _delete_tables()
    return jsonify({"status": "reset", "message": "All uploaded tables were cleared."})


_load_tables()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
