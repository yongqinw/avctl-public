#!/usr/bin/env python3
"""Dry-run real Ask models against local prompt cases without executing tools.

The evaluator sends the production system prompt, synthetic library/context,
and production tool schemas directly to the selected provider. It inspects
the proposed calls but never invokes an avctl handler, device, Music.app, or
library mutation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import agent_providers, agentlink  # noqa: E402
from tests.agent_prompt_cases import PROMPT_CASES  # noqa: E402


SYNTHETIC_LIBRARY = """
<avctl_library_snapshot>
{"timezone":"America/Los_Angeles","albums":[{"added":"2026-08-20","artist":"周杰伦","album":"我很忙","tracks":[{"name":"青花瓷"}]},{"added":"2026-08-18","artist":"Miles Davis","album":"Kind of Blue","tracks":[{"name":"So What"}]}],"playlists":["Recently Added — This Week","Favorites"]}
</avctl_library_snapshot>""".strip()

SYNTHETIC_CONTEXT = {
    "scene": "music",
    "devices": {
        "music": {"state": "playing", "track": "Test Track", "queued": 4},
        "amp": {"power": True, "input": "dac", "volume": 56},
        "mini": {"volume": 56, "muted": False},
        "tv": {"power": True, "input": "hdmi2"},
    },
}


def _arguments(call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function") or {}
    raw = function.get("arguments") or "{}"
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def _calls(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        return calls
    content = message.get("content")
    if isinstance(content, str):
        return agentlink._dsml_tool_calls(content)  # noqa: SLF001
    return []


def _check(case: dict[str, Any], message: dict[str, Any]) -> list[str]:
    actual_calls = _calls(message)
    actual = [(str((call.get("function") or {}).get("name")), _arguments(call))
              for call in actual_calls]
    expected = case["calls"]
    errors = []
    if [name for name, _args in actual] != [row["tool"] for row in expected]:
        errors.append(
            f"tools expected {[row['tool'] for row in expected]}, "
            f"got {[name for name, _args in actual]}")
    for index, spec in enumerate(expected[:len(actual)]):
        arguments = actual[index][1]
        for key, value in spec.get("arguments", {}).items():
            actual_value = arguments.get(key)
            if key == "query" and isinstance(actual_value, str):
                if str(value).casefold() not in actual_value.casefold():
                    errors.append(f"call {index + 1} query lacks {value!r}")
            elif actual_value != value:
                errors.append(
                    f"call {index + 1} {key} expected {value!r}, "
                    f"got {actual_value!r}")
        if "candidate_count" in spec:
            low, high = spec["candidate_count"]
            count = len(arguments.get("candidates") or [])
            if not low <= count <= high:
                errors.append(
                    f"call {index + 1} candidates expected {low}-{high}, got {count}")
    forbidden = set(case.get("forbidden_tools") or [])
    used_forbidden = forbidden.intersection(name for name, _args in actual)
    if used_forbidden:
        errors.append(f"forbidden tools proposed: {sorted(used_forbidden)}")
    if case.get("requires_text"):
        content = message.get("content")
        if actual or not isinstance(content, str) or not content.strip():
            errors.append("expected a text clarification with no tool calls")
    return errors


def _selected_cases(names: list[str]) -> list[dict[str, Any]]:
    if not names:
        return PROMPT_CASES
    selected = [case for case in PROMPT_CASES if case["name"] in names]
    missing = sorted(set(names) - {case["name"] for case in selected})
    if missing:
        raise SystemExit("unknown cases: " + ", ".join(missing))
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate Ask tool planning without executing any action")
    parser.add_argument("--profile", help="configured agent profile name")
    parser.add_argument("--case", action="append", default=[],
                        help="case name; repeat to select multiple")
    parser.add_argument("--list", action="store_true", help="list case names")
    args = parser.parse_args()
    cases = _selected_cases(args.case)
    if args.list:
        for case in cases:
            print(f"{case['name']}: {case['prompt']}")
        return

    profile = (agent_providers.profiles()[args.profile] if args.profile
               else agent_providers.active_profile())
    credential = (os.environ.get(profile.api_key_env, "").strip()
                  if profile.api_key_env else "")
    if profile.driver == "fireworks" and not credential:
        raise SystemExit(
            f"set {profile.api_key_env or 'the configured API key environment'} "
            "before running this opt-in evaluator")

    prompt = agentlink.STATIC_SYSTEM_PROMPT + "\n\n" + SYNTHETIC_LIBRARY
    failures = 0
    with requests.Session() as session:
        model = agent_providers.provider(
            session=session, api_key=credential, profile_name=profile.name)
        for case in cases:
            user = (
                "<live_avctl_context>"
                + json.dumps(SYNTHETIC_CONTEXT, ensure_ascii=False,
                             separators=(",", ":"))
                + "</live_avctl_context>\n\n君父 says: " + case["prompt"]
            )
            reply = model.complete(
                [{"role": "system", "content": prompt},
                 {"role": "user", "content": user}],
                agentlink.TOOLS, "local-prompt-eval")
            try:
                message = reply.body["choices"][0]["message"]
            except (KeyError, IndexError, TypeError):
                errors = ["provider returned no assistant message"]
                message = {}
            else:
                errors = _check(case, message)
            status = "PASS" if not errors else "FAIL"
            print(f"{status} {case['name']} ({reply.elapsed_ms} ms)")
            if errors:
                failures += 1
                for error in errors:
                    print("  -", error)
                proposed = [(str((call.get("function") or {}).get("name")),
                             _arguments(call)) for call in _calls(message)]
                print("  proposed:", json.dumps(proposed, ensure_ascii=False))
    print(f"\n{len(cases) - failures}/{len(cases)} prompt cases passed")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
