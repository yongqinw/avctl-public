#!/usr/bin/env python3
"""Summarize private Ask friction without sending history anywhere."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import ask_history


def _similar(left: str, right: str) -> bool:
    a = "".join(left.casefold().split())
    b = "".join(right.casefold().split())
    return bool(a and b and SequenceMatcher(None, a, b).ratio() >= 0.58)


def analyze(rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(row.get("action_status") or "unknown") for row in rows)
    tools = Counter(
        str(outcome.get("tool") or "unknown")
        for row in rows for outcome in row.get("outcomes") or []
        if isinstance(outcome, dict)
    )
    failed_tools = Counter(
        str(outcome.get("tool") or "unknown")
        for row in rows for outcome in row.get("outcomes") or []
        if isinstance(outcome, dict)
        and outcome.get("status") in {"rejected", "unknown"}
    )
    provider_times = [
        float((row.get("trace") or {}).get("provider_ms") or 0)
        for row in rows
    ]
    friction = []
    for index, row in enumerate(rows):
        status = str(row.get("action_status") or "")
        repeated = index > 0 and _similar(
            str(rows[index - 1].get("user") or ""),
            str(row.get("user") or ""),
        )
        if status in {"rejected", "unknown", "error"} or repeated:
            friction.append({
                "time": datetime.fromtimestamp(float(row["created_at"])).isoformat(
                    timespec="seconds"),
                "status": status,
                "channel": row.get("channel"),
                "repeated": repeated,
                "user": row.get("user"),
                "assistant": row.get("assistant"),
                "error_kind": row.get("error_kind"),
            })
    suggestions = []
    if statuses["rejected"]:
        suggestions.append(
            "Review rejected tool arguments and selection expiry; these are "
            "grounding or capability gaps, not phrasing problems.")
    if statuses["unknown"]:
        suggestions.append(
            "Add driver readback for unknown outcomes before changing prompts.")
    if statuses["error"]:
        suggestions.append(
            "Group provider, transcription, and application errors by error_kind.")
    if any(row.get("channel") == "voice" for row in friction):
        suggestions.append(
            "Compare voice transcripts with the following correction and add "
            "music/equipment vocabulary to transcription context.")
    if any(row["repeated"] for row in friction):
        suggestions.append(
            "Repeated requests usually mean the prior reply sounded successful "
            "without producing the expected state; inspect its tool receipt.")
    return {
        "turns": len(rows),
        "channels": dict(Counter(str(row.get("channel")) for row in rows)),
        "statuses": dict(statuses),
        "tools": dict(tools.most_common()),
        "failed_tools": dict(failed_tools.most_common()),
        "provider_ms_median": (
            round(statistics.median(provider_times)) if provider_times else 0),
        "friction": friction[-50:],
        "suggestions": suggestions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze local Ask outcomes; no data leaves this Mac.")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = analyze(ask_history.analysis_rows(args.days))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(f"Ask history: {report['turns']} turns / {args.days} days")
    print("channels:", report["channels"])
    print("statuses:", report["statuses"])
    print("tools:", report["tools"])
    print("failed tools:", report["failed_tools"])
    print("median provider latency:", report["provider_ms_median"], "ms")
    for row in report["friction"]:
        marker = " repeated" if row["repeated"] else ""
        print(f"\n[{row['time']}] {row['status']}/{row['channel']}{marker}")
        print("you:", row["user"])
        if row["assistant"]:
            print("ask:", row["assistant"])
        if row["error_kind"]:
            print("error:", row["error_kind"])
    if report["suggestions"]:
        print("\nNext improvements:")
        for suggestion in report["suggestions"]:
            print("-", suggestion)


if __name__ == "__main__":
    main()
