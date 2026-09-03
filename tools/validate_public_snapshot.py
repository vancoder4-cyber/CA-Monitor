#!/usr/bin/env python3
"""Validate the Pages snapshot contract and block accidental identity disclosure."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path


FORBIDDEN_KEYS = {"by", "by_name", "open_id", "chat_id"}
OPEN_ID_PATTERN = re.compile(r"\bou_[A-Za-z0-9]+\b")
REQUIRED_LISTS = {
    "coverage", "pending", "forecasts", "calendar", "announced",
    "recent_declares", "conflicts", "gaps", "changelog",
}


def _walk(value, path="$"):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in FORBIDDEN_KEYS:
                raise ValueError(f"forbidden public key: {child_path}")
            _walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk(child, f"{path}[{index}]")
    elif isinstance(value, str) and OPEN_ID_PATTERN.search(value):
        raise ValueError(f"Lark open_id found in public value: {path}")


def validate(data, *, expected_sha="", expected_run_id=""):
    if not isinstance(data, dict):
        raise ValueError("snapshot root must be an object")
    if data.get("schema_version") != 3:
        raise ValueError(f"expected schema v3, got {data.get('schema_version')!r}")
    for key in REQUIRED_LISTS:
        if not isinstance(data.get(key), list):
            raise ValueError(f"{key} must be a list")
    if not isinstance(data.get("counts"), dict):
        raise ValueError("counts must be an object")
    for key in ("generated", "business_date", "source_sha"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"{key} is missing or invalid")
    try:
        dt.date.fromisoformat(data["business_date"])
    except ValueError as exc:
        raise ValueError("business_date must use YYYY-MM-DD") from exc
    if expected_sha and data.get("source_sha") != expected_sha:
        raise ValueError("source_sha does not match this workflow commit")
    if expected_run_id and str(data.get("run_id")) != str(expected_run_id):
        raise ValueError("run_id does not match this workflow run")
    if data.get("delivery_status") not in {"sent", "legal_skip"}:
        raise ValueError("delivery_status is missing or invalid")
    parsed_times = {}
    for key in ("generated_at_utc", "valid_until_utc"):
        try:
            parsed = dt.datetime.fromisoformat(str(data[key]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid {key}") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{key} must be timezone-aware")
        parsed_times[key] = parsed
    if parsed_times["valid_until_utc"] <= parsed_times["generated_at_utc"]:
        raise ValueError("valid_until_utc must be later than generated_at_utc")
    _walk(data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="site_data.json")
    parser.add_argument("--expected-sha", default="")
    parser.add_argument("--expected-run-id", default="")
    args = parser.parse_args()
    path = Path(args.path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        validate(data, expected_sha=args.expected_sha, expected_run_id=args.expected_run_id)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"PUBLIC SNAPSHOT VALIDATION FAILED: {exc}")
    print(f"public snapshot OK: schema v{data['schema_version']} · {data['source_sha'][:12]}")


if __name__ == "__main__":
    main()
