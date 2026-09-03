#!/usr/bin/env python3
"""Fail closed when the production deduplication state was not restored safely."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def validate(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"missing or empty state file: {path}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid state JSON: {exc}") from exc
    if not isinstance(state, dict):
        raise ValueError("state root must be an object")
    for field in ("seen", "fired_rounds", "declared"):
        if not isinstance(state.get(field), dict):
            raise ValueError(f"state.{field} must be an object")
    if not state["seen"]:
        raise ValueError("state.seen is empty; refusing a production history replay")


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "data/state.json")
    try:
        validate(target)
    except ValueError as exc:
        print(f"STATE VALIDATION FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"state OK: {len(json.loads(target.read_text(encoding='utf-8'))['seen'])} seen signatures")
