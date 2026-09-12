from __future__ import annotations

from api import agentlink
from tests.agent_prompt_cases import PROMPT_CASES


def test_prompt_case_corpus_is_unique_and_substantial():
    names = [case["name"] for case in PROMPT_CASES]
    prompts = [case["prompt"] for case in PROMPT_CASES]

    assert len(PROMPT_CASES) >= 20
    assert len(names) == len(set(names))
    assert len(prompts) == len(set(prompts))
    assert any(len(case["calls"]) >= 5 for case in PROMPT_CASES)
    assert any(any("candidate_count" in call for call in case["calls"])
               for case in PROMPT_CASES)


def test_prompt_case_expected_calls_use_real_valid_tool_schemas():
    for case in PROMPT_CASES:
        for call in case["calls"]:
            assert call["tool"] in agentlink.TOOL_HANDLERS, case["name"]
            # Partial argument expectations deliberately omit model-authored
            # values such as playlist names. Validate the full expectations.
            schema = agentlink._TOOL_SCHEMAS[call["tool"]]  # noqa: SLF001
            required = set(schema.get("required") or [])
            arguments = call.get("arguments") or {}
            if required.issubset(arguments):
                agentlink._validate_tool_arguments(  # noqa: SLF001
                    call["tool"], arguments)
        assert not set(case.get("forbidden_tools") or []).difference(
            agentlink.TOOL_HANDLERS)


def test_thirty_song_case_matches_production_curate_limit():
    case = next(case for case in PROMPT_CASES
                if case["name"] == "thirty_song_mixed_queue")
    expected = next(call["candidate_count"] for call in case["calls"]
                    if call["tool"] == "curate_music")
    tool = next(row["function"] for row in agentlink.TOOLS
                if row["function"]["name"] == "curate_music")

    assert expected[1] == tool["parameters"]["properties"]["candidates"][
        "maxItems"]
