"""Deterministic provider/replay primitives for Ask reliability scenarios."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


class ProviderResponse:
    def __init__(self, body: dict[str, Any], status: int = 200):
        self.body = body
        self.status_code = status
        self.ok = 200 <= status < 300

    def json(self) -> dict[str, Any]:
        return self.body


@dataclass
class ScriptedProvider:
    """Replay model outputs while preserving every input for assertions."""

    responses: list[ProviderResponse]
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def post(self, url: str, **kwargs: Any) -> ProviderResponse:
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("reliability scenario exhausted provider replies")
        return self.responses.pop(0)


@dataclass
class ReviewingProvider:
    """Return an initial plan, then select real numbered curation options.

    Tests may name the expected provider title for translated recordings.
    Every other request selects its first server-offered safe option, matching
    the constrained follow-up contract used by Fireworks in production.
    """

    initial: ProviderResponse
    option_names: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    review: dict[str, Any] | None = None

    def post(self, url: str, **kwargs: Any) -> ProviderResponse:
        self.calls.append((url, kwargs))
        if len(self.calls) == 1:
            return self.initial
        messages = kwargs["json"]["messages"]
        tool_message = next(
            row for row in reversed(messages)
            if row.get("name") == "curate_music"
        )
        result = json.loads(tool_message["content"])
        self.review = result["semantic_review"]
        options = {
            int(row["option_index"]): row
            for row in self.review["options"]
        }
        decisions = []
        for request in self.review["requests"]:
            offered = list(request["option_indexes"])
            wanted = self.option_names.get(str(request.get("title") or ""))
            if wanted is None:
                chosen = offered[0]
            else:
                chosen = next(
                    index for index in offered
                    if options[index].get("name") == wanted
                )
            decisions.append({
                "request_index": request["request_index"],
                "option_index": chosen,
            })
        return tool_reply("resolve_music_review", {
            "review_id": self.review["review_id"],
            "decisions": decisions,
        }, "semantic-review")


def tool_reply(name: str, arguments: dict[str, Any],
               call_id: str = "scenario-call") -> ProviderResponse:
    return ProviderResponse({"choices": [{"message": {"tool_calls": [{
        "id": call_id,
        "type": "function",
        "function": {"name": name,
                     "arguments": json.dumps(arguments)},
    }]}}]})


def text_reply(message: str) -> ProviderResponse:
    return ProviderResponse({"choices": [{"message": {"content": message}}]})
