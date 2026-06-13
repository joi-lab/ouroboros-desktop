import gzip
import json
import os
from concurrent.futures import ThreadPoolExecutor

from ouroboros.observability import (
    persist_call,
    posix_private_modes_supported,
    redact_projection,
    write_blob,
)
from ouroboros.outcomes import (
    RESULT_FAILED,
    RESULT_INFRA_FAILED,
    RESULT_SUCCEEDED,
    artifact_bundle_from_result,
    derive_loop_outcome,
    maybe_write_verification_artifact,
)
from ouroboros.utils import sanitize_tool_args_for_log


def _read_gzip_json(path):
    with gzip.open(path, "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))


def test_redactor_records_key_and_value_rules_without_secret_leak():
    payload = {
        "OPENAI_API_KEY": "sk-testsecretvalue000000000000",
        "log": "MY_API_KEY=thisisaverylongsecretvalue123456 github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
        "prompt_tokens": 123,
        "completion_tokens": 45,
        "cached_tokens": 6,
        "token_estimate": 789,
        "reasoning_tokens": 10,
        "nested": {
            "authorization": "Bearer verylongbearertokenvalue123456",
            "access_token": "verylongaccesstokenvalue123456",
            "refreshToken": "verylongrefreshtokenvalue123456",
            "secret": "plainsecretvalue1234567890",
            "secret_key": "secretkeyvalue1234567890",
            "apiKey": "apikeyvalue1234567890",
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
            "PRIVATE_KEY_PEM": "-----BEGINPRIVATEKEY-----abc1234567890",
            "STRIPE_SECRET_KEY": "stripescretvalue1234567890",
            "bearer_token": "verylongbearertokenvalueabcdef",
            "anthropic_secret": "sk-ant-verylongsecretvalue123456",
            "url": "https://user:pass@example.com/path",
        },
    }

    redacted = redact_projection(payload)

    rendered = json.dumps(redacted.value)
    assert "sk-testsecretvalue" not in rendered
    assert "thisisaverylongsecretvalue" not in rendered
    assert "github_pat_" not in rendered
    assert "verylongbearertokenvalue" not in rendered
    assert "verylongaccesstokenvalue" not in rendered
    assert "verylongrefreshtokenvalue" not in rendered
    assert "plainsecretvalue" not in rendered
    assert "secretkeyvalue" not in rendered
    assert "apikeyvalue" not in rendered
    assert "wJalrXUtnFEMI" not in rendered
    assert "BEGINPRIVATEKEY" not in rendered
    assert "stripescretvalue" not in rendered
    assert "verylongsecretvalue" not in rendered
    assert "user:pass" not in rendered
    assert redacted.value["prompt_tokens"] == 123
    assert redacted.value["completion_tokens"] == 45
    assert redacted.value["cached_tokens"] == 6
    assert redacted.value["token_estimate"] == 789
    assert redacted.value["reasoning_tokens"] == 10
    assert redacted.manifest()["redacted"] is True
    rules = {item["rule"] for item in redacted.manifest()["rules"]}
    assert {"secret_key_name", "url_credentials"} <= rules


def test_persist_call_writes_private_full_and_redacted_refs(tmp_path):
    payload = {"tool": "run_command", "args": {"token": "ghp_abcdefghijklmnopqrstuvwxyz123456"}}

    refs = persist_call(
        tmp_path,
        task_id="task-1",
        call_id="call-1",
        call_type="tool_call",
        payload=payload,
        manifest={"model": "test/model"},
    )

    manifest_path = tmp_path / "observability" / "calls" / "task-1" / "call-1.json"
    assert manifest_path.exists()
    if posix_private_modes_supported():
        assert os.stat(tmp_path / "observability").st_mode & 0o777 == 0o700
        assert os.stat(tmp_path / "observability" / "blobs").st_mode & 0o777 == 0o700
        assert os.stat(manifest_path.parent).st_mode & 0o777 == 0o700
        assert os.stat(manifest_path).st_mode & 0o777 == 0o600

    redacted_path = refs["redacted_projection_ref"]["path"]
    if posix_private_modes_supported():
        assert os.stat(redacted_path).st_mode & 0o777 == 0o600
    assert "full_payload_ref" not in refs
    assert "redacted_projection" not in refs
    assert _read_gzip_json(redacted_path)["args"]["token"] == "***REDACTED***"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    full_path = manifest["full_payload_ref"]["path"]
    if posix_private_modes_supported():
        assert os.stat(full_path).st_mode & 0o777 == 0o600
    assert _read_gzip_json(full_path)["args"]["token"].startswith("ghp_")
    assert manifest["call_type"] == "tool_call"
    assert manifest["redaction"]["redacted"] is True
    assert manifest["full_payload_ref"]["sha256"]
    assert refs["manifest_ref"]["sha256"] == __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest()


def test_write_blob_accepts_concurrent_same_payload_publish(tmp_path):
    payload = {"message": "same reviewer response", "usage": {"prompt_tokens": 1}}

    with ThreadPoolExecutor(max_workers=8) as pool:
        refs = list(pool.map(lambda _: write_blob(tmp_path, payload), range(16)))

    assert len({ref["sha256"] for ref in refs}) == 1
    assert all(os.path.exists(ref["path"]) for ref in refs)


def test_loop_outcome_distinguishes_success_empty_and_provider_failure():
    ok = derive_loop_outcome("done", {"rounds": 1}, {"tool_calls": []})
    assert ok["result_status"] == RESULT_SUCCEEDED
    assert ok["failure"] is None

    empty = derive_loop_outcome("", {"rounds": 1}, {"tool_calls": []})
    assert empty["result_status"] == RESULT_FAILED
    assert empty["reason_code"] == "empty_final_text"

    infra = derive_loop_outcome(
        "ignored",
        {"result_status": RESULT_INFRA_FAILED, "reason_code": "llm_api_error"},
        {"tool_calls": []},
    )
    assert infra["result_status"] == RESULT_INFRA_FAILED
    assert infra["failure"]["kind"] == "provider"

    runtime_error = derive_loop_outcome(
        "⚠️ Error during processing: RuntimeError: boom",
        {"rounds": 1},
        {"tool_calls": []},
    )
    assert runtime_error["result_status"] == RESULT_INFRA_FAILED
    assert runtime_error["reason_code"] == "task_exception"

    deep_unavailable = derive_loop_outcome(
        "❌ Deep self-review unavailable: no key",
        {},
        {"tool_calls": []},
    )
    assert deep_unavailable["result_status"] == RESULT_INFRA_FAILED
    assert deep_unavailable["reason_code"] == "deep_self_review_unavailable"

    tool_failure = derive_loop_outcome(
        "Created the file.",
        {"rounds": 2},
        {"tool_calls": [{
            "tool": "run_command",
            "is_error": True,
            "status": "artifact_output_error",
            "result": "⚠️ ARTIFACT_OUTPUT_ERROR: undeclared output",
        }]},
    )
    assert tool_failure["result_status"] == RESULT_FAILED
    assert tool_failure["reason_code"] == "tool_failure"
    assert tool_failure["failure"]["kind"] == "tool"
    assert tool_failure["failure"]["tool_errors"][0]["status"] == "artifact_output_error"

    recovered = derive_loop_outcome(
        "Created the file.",
        {"rounds": 3},
        {"tool_calls": [
            {
                "tool": "edit_text",
                "args": {"root": "user_files", "path": "Desktop/report.html"},
                "is_error": True,
                "status": "edit_text_blocked",
                "result": "⚠️ EDIT_TEXT_ERROR: old_str matched 0 times",
            },
            {
                "tool": "write_file",
                "args": {"root": "user_files", "path": "Desktop/report.html"},
                "is_error": False,
                "status": "ok",
                "result": "OK: wrote user_files:Desktop/report.html\nARTIFACT_OUTPUTS: registered user file -> artifact_store:report.html",
            },
        ]},
    )
    assert recovered["result_status"] == RESULT_SUCCEEDED
    assert recovered["failure"] is None

    unrelated_recovery = derive_loop_outcome(
        "Created another file.",
        {"rounds": 3},
        {"tool_calls": [
            {
                "tool": "run_command",
                "args": {"cmd": "python3 build_report.py", "outputs": ["report.html"]},
                "is_error": True,
                "status": "non_zero_exit",
                "result": "⚠️ SHELL_EXIT_ERROR: command exited with exit_code=1.",
            },
            {
                "tool": "write_file",
                "args": {"root": "user_files", "path": "Desktop/other.html"},
                "is_error": False,
                "status": "ok",
                "result": "OK: wrote user_files:Desktop/other.html\nARTIFACT_OUTPUTS: registered user file -> artifact_store:other.html",
            },
        ]},
    )
    assert unrelated_recovery["result_status"] == RESULT_FAILED
    assert unrelated_recovery["reason_code"] == "tool_failure"

    cleanup_failure = derive_loop_outcome(
        "Done",
        {"rounds": 2},
        {
            "tool_calls": [],
            "verification_events": [{
                "kind": "services_stopped",
                "services": [{
                    "name": "devserver",
                    "artifact_output_failed": True,
                    "artifact_outputs": "⚠️ ARTIFACT_OUTPUT_ERROR:\n- missing output: report.html",
                }],
            }],
        },
    )
    assert cleanup_failure["result_status"] == RESULT_FAILED
    assert cleanup_failure["failure"]["kind"] == "verification"


def test_tool_arg_sanitizer_uses_value_pattern_redactor():
    args = {
        "cmd": "curl -H 'Authorization: Bearer verylongbearertokenvalue1234567890' https://x",
        "script": "OPENROUTER_API_KEY=sk-or-thisisaverylongsecretvalue1234567890",
    }

    rendered = json.dumps(sanitize_tool_args_for_log("run_command", args))

    assert "verylongbearertokenvalue" not in rendered
    assert "sk-or-thisisaverylongsecret" not in rendered
    assert "***REDACTED***" in rendered


def test_loop_outcome_trace_refs_include_llm_and_tool_refs():
    outcome = derive_loop_outcome(
        "done",
        {
            "execution_id": "exec_1",
            "rounds": 1,
            "llm_call_refs": [{
                "llm_call_id": "llm_1",
                "round_id": "exec_1:round:1",
                "request_ref": {"path": "req"},
                "response_ref": {"path": "resp"},
            }],
        },
        {
            "tool_calls": [{
                "trace_ref": {
                    "call_id": "tool_1",
                    "manifest_ref": {"path": "tool"},
                    "redacted_projection_ref": {"path": "redacted"},
                }
            }]
        },
    )

    refs = outcome["trace_refs"]
    assert refs["execution_id"] == "exec_1"
    assert refs["llm_call_refs"][0]["llm_call_id"] == "llm_1"
    assert refs["tool_call_refs"][0]["call_id"] == "tool_1"


def test_artifact_bundle_and_large_verification_ledger_artifact(tmp_path):
    bundle = artifact_bundle_from_result({
        "artifact_status": "ready",
        "artifacts": [{"kind": "patch", "name": "fix.patch", "path": "/tmp/fix.patch", "size": 4, "sha256": "abcd"}],
    })
    assert bundle["status"] == "ready"
    assert bundle["artifacts"][0]["kind"] == "patch"

    mixed = artifact_bundle_from_result({
        "artifact_status": "failed",
        "artifact_error": "patch failed",
        "artifacts": [{"kind": "verification_ledger", "name": "verification_ledger.json", "path": "/tmp/ledger"}],
    })
    assert mixed["status"] == "failed"
    assert mixed["artifacts"][0]["status"] == "ready"

    ledger = {"schema_version": 1, "created_at": "now", "task_id": "task-1", "entries": [{"x": "y" * 200}]}
    refs = maybe_write_verification_artifact(tmp_path, "task-1", ledger, threshold_chars=20)
    assert refs["inline"]["omitted_to_artifact"] is True
    artifact = refs["artifact"]
    assert artifact["status"] == "ready"
    assert artifact["path"].endswith("verification_ledger.json")
    assert os.path.exists(artifact["path"])
