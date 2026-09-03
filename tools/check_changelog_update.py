#!/usr/bin/env python3
"""Require CHANGELOG.md in every PR that changes product behavior or configuration."""
from __future__ import annotations

import argparse
import subprocess


def changed_files(base):
    output = subprocess.check_output(
        ["git", "diff", "--name-only", f"{base}...HEAD"], text=True
    )
    return {line.strip() for line in output.splitlines() if line.strip()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    changed = changed_files(args.base)
    if changed and "CHANGELOG.md" not in changed:
        raise SystemExit(
            "CHANGELOG.md was not updated in this PR; user-visible fixes must update 最近更新."
        )
    print("CHANGELOG gate OK")


if __name__ == "__main__":
    main()
