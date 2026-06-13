import json
import time
from types import SimpleNamespace

from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request
from ouroboros.triad_review import parse_model_review_results


class FakeLLM:
    def __init__(self):
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        body = {
            "verdict": "PASS",
            "findings": [],
            "summary": f"reviewed by {kwargs['model']}",
        }
        return {"content": json.dumps(body)}, {"prompt_tokens": 10, "completion_tokens": 5}


class FencedArrayLLM:
    def chat(self, **kwargs):
        body = (
            "Here is the review:\n"
            "```json\n"
            "[{\"verdict\":\"FAIL\",\"severity\":\"critical\",\"item\":\"x\",\"evidence\":\"e\",\"recommendation\":\"r\"}]\n"
            "```"
        )
        return {"content": body}, {"prompt_tokens": 10, "completion_tokens": 5}


class FencedObjectLLM:
    def chat(self, **kwargs):
        body = (
            "Verdict below:\n"
            "```json\n"
            "{\"verdict\":\"PASS\",\"findings\":[]}\n"
            "```"
        )
        return {"content": body}, {"prompt_tokens": 10, "completion_tokens": 5}


class ConcernsLLM:
    def chat(self, **kwargs):
        # Valid JSON, transport ok, but a non-PASS/FAIL/DEGRADED verdict.
        return {"content": "{\"verdict\":\"CONCERNS\",\"findings\":[]}"}, {"prompt_tokens": 10, "completion_tokens": 5}


class ErrorLLM:
    def chat(self, **kwargs):
        raise RuntimeError("provider exploded")


class HangingLLM:
    def chat(self, **kwargs):
        time.sleep(0.2)
        return {"content": "{\"verdict\":\"PASS\",\"findings\":[],\"summary\":\"late\"}"}, {}


def test_review_substrate_treats_duplicate_models_as_independent_slots(tmp_path):
    llm = FakeLLM()
    slots = [
        ReviewSlot(slot_id="triad_a", model="same/model", effort="high"),
        ReviewSlot(slot_id="triad_b", model="same/model", effort="high"),
    ]
    result = run_review_request(
        ReviewRequest(surface="task_acceptance", goal="verify final claim", subject="done", task_id="task-1"),
        slots=slots,
        drive_root=tmp_path,
        llm=llm,
    )

    assert result.aggregate_signal == "PASS"
    assert [actor["slot_id"] for actor in result.actors] == ["triad_a", "triad_b"]
    assert [call["model"] for call in llm.calls] == ["same/model", "same/model"]
    for actor in result.actors:
        assert actor["prompt_ref"]["manifest_ref"]["path"]
        assert actor["response_ref"]["manifest_ref"]["path"]


def test_review_substrate_queues_all_slots_above_concurrency_cap(tmp_path):
    llm = FakeLLM()
    slots = [
        ReviewSlot(slot_id=f"slot_{idx}", model=f"model-{idx}", effort="high")
        for idx in range(10)
    ]
    result = run_review_request(
        ReviewRequest(surface="task_acceptance", goal="verify final claim", subject="done", task_id="task-10"),
        slots=slots,
        drive_root=tmp_path,
        llm=llm,
    )

    assert result.aggregate_signal == "PASS"
    assert [actor["slot_id"] for actor in result.actors] == [slot.slot_id for slot in slots]
    assert {call["model"] for call in llm.calls} == {slot.model for slot in slots}
    assert len(llm.calls) == 10
    assert all(actor["status"] == "ok" for actor in result.actors)

    slow_calls = []
    slow_llm = SimpleNamespace(chat=lambda **kwargs: (
        slow_calls.append(kwargs),
        time.sleep(0.2),
        ({"content": "{\"verdict\":\"PASS\",\"findings\":[],\"summary\":\"late\"}"}, {}),
    )[-1])
    slow_slots = [
        ReviewSlot(slot_id=f"slow_{idx}", model=f"slow-model-{idx}", effort="high", timeout_sec=0.05)
        for idx in range(10)
    ]
    slow_result = run_review_request(
        ReviewRequest(surface="task_acceptance", goal="verify final claim", subject="done", task_id="task-slow"),
        slots=slow_slots,
        drive_root=tmp_path,
        llm=slow_llm,
    )
    assert len(slow_calls) == 10
    assert "Not started before reviewer timeout budget expired" not in "\n".join(slow_result.degraded_reasons)


def test_review_substrate_reports_no_slots_as_degraded(tmp_path):
    result = run_review_request(
        ReviewRequest(surface="plan", goal="review plan", task_id="task-1"),
        slots=[],
        drive_root=tmp_path,
        llm=FakeLLM(),
    )

    assert result.aggregate_signal == "DEGRADED"
    assert result.degraded is True
    assert "no_review_slots" in result.degraded_reasons


def test_review_substrate_emits_usage_when_context_supplied(tmp_path):
    class Ctx:
        task_id = "task-usage"
        pending_events = []

    ctx = Ctx()
    result = run_review_request(
        ReviewRequest(surface="task_acceptance", goal="review claim", task_id="task-usage"),
        slots=[ReviewSlot(slot_id="slot_a", model="same/model")],
        drive_root=tmp_path,
        llm=FakeLLM(),
        usage_ctx=ctx,
    )

    assert result.aggregate_signal == "PASS"
    usage_events = [event for event in ctx.pending_events if event.get("type") == "llm_usage"]
    assert len(usage_events) == 1
    assert usage_events[0]["task_id"] == "task-usage"
    assert usage_events[0]["source"] == "review_substrate:task_acceptance"
    assert usage_events[0]["slot_id"] == "slot_a"


def test_review_substrate_parses_fenced_json_array_findings(tmp_path):
    result = run_review_request(
        ReviewRequest(surface="scope", goal="review diff", task_id="task-json-array"),
        slots=[ReviewSlot(slot_id="slot_a", model="same/model")],
        drive_root=tmp_path,
        llm=FencedArrayLLM(),
    )

    assert result.aggregate_signal == "FAIL"
    assert result.parsed_findings[0]["item"] == "x"
    assert result.actors[0]["parsed"][0]["verdict"] == "FAIL"


def test_review_substrate_parses_fenced_json_object_verdict(tmp_path):
    # A fenced JSON OBJECT (not array) must parse as PASS, not a false DEGRADED.
    result = run_review_request(
        ReviewRequest(surface="task_acceptance", goal="verify claim", subject="done", task_id="task-obj"),
        slots=[
            ReviewSlot(slot_id="slot_a", model="m"),
            ReviewSlot(slot_id="slot_b", model="m"),
        ],
        drive_root=tmp_path,
        llm=FencedObjectLLM(),
    )
    assert result.aggregate_signal == "PASS"
    assert result.degraded is False


def test_review_substrate_degraded_quorum_carries_reason(tmp_path):
    # No FAIL, no PASS quorum, no transport errors -> DEGRADED must still be honest:
    # degraded=True with a non-empty reason (no DEGRADED/degraded=False/empty mismatch).
    result = run_review_request(
        ReviewRequest(
            surface="task_acceptance", goal="verify claim", subject="done", task_id="task-quorum",
            policy={"min_successful_slots": 2},
        ),
        slots=[
            ReviewSlot(slot_id="slot_a", model="m"),
            ReviewSlot(slot_id="slot_b", model="m"),
        ],
        drive_root=tmp_path,
        llm=ConcernsLLM(),
    )
    assert result.aggregate_signal == "DEGRADED"
    assert result.degraded is True
    assert result.degraded_reasons
    assert any("quorum_not_met" in reason for reason in result.degraded_reasons)


def test_review_substrate_persists_error_actor_response_ref(tmp_path):
    result = run_review_request(
        ReviewRequest(surface="scope", goal="review diff", task_id="task-error"),
        slots=[ReviewSlot(slot_id="slot_a", model="same/model")],
        drive_root=tmp_path,
        llm=ErrorLLM(),
    )

    actor = result.actors[0]
    assert actor["status"] == "error"
    assert actor["prompt_ref"]["manifest_ref"]["path"]
    assert actor["response_ref"]["manifest_ref"]["path"]
    manifest = json.loads(open(actor["response_ref"]["manifest_ref"]["path"], encoding="utf-8").read())
    assert manifest["call_type"] == "scope_review_error"
    assert manifest["status"] == "error"


def test_review_substrate_persists_timeout_actor_refs(tmp_path):
    result = run_review_request(
        ReviewRequest(surface="scope", goal="review diff", task_id="task-timeout"),
        slots=[ReviewSlot(slot_id="slot_a", model="same/model", timeout_sec=0.01)],
        drive_root=tmp_path,
        llm=HangingLLM(),
    )

    actor = result.actors[0]
    assert actor["status"] == "error"
    assert "Timeout after" in actor["error"]
    assert actor["prompt_ref"]["manifest_ref"]["path"]
    assert actor["response_ref"]["manifest_ref"]["path"]


def test_triad_actor_records_preserve_review_refs():
    parsed = parse_model_review_results({
        "results": [{
            "model": "m1",
            "text": "[{\"item\":\"x\",\"verdict\":\"PASS\",\"severity\":\"advisory\",\"reason\":\"ok\"}]",
            "prompt_ref": {"manifest_ref": {"path": "prompt.json"}},
            "response_ref": {"manifest_ref": {"path": "response.json"}},
        }]
    })

    actor = parsed.actor_records[0].to_dict()
    assert actor["prompt_ref"]["manifest_ref"]["path"] == "prompt.json"
    assert actor["response_ref"]["manifest_ref"]["path"] == "response.json"


def test_scope_review_result_preserves_substrate_refs(tmp_path, monkeypatch):
    from ouroboros.tools import scope_review
    from ouroboros.tools.review_helpers import build_scope_actor_record

    class FakeScopeLLM:
        def chat(self, **kwargs):
            return {"content": "[]"}, {"prompt_tokens": 10, "completion_tokens": 5}

    ctx = SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path, task_id="scope-task", pending_events=[])
    monkeypatch.setattr(scope_review, "LLMClient", lambda: FakeScopeLLM())
    monkeypatch.setattr(scope_review, "_build_scope_prompt", lambda *a, **k: ("scope prompt", None))
    monkeypatch.setattr(scope_review, "_get_scope_model", lambda: "test-scope-model")

    result = scope_review.run_scope_review(ctx, "commit message")
    record = build_scope_actor_record(result, fallback_model_id="test-scope-model", slot_id="scope_slot_1")

    assert result.status == "responded"
    assert record["prompt_ref"]["manifest_ref"]["path"]
    assert record["response_ref"]["manifest_ref"]["path"]
