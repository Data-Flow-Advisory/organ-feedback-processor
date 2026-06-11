"""Tests for the feedback-processor organ. Vanilla pytest, stdlib only."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ORGAN = Path(__file__).parent / "organ.py"
_spec = importlib.util.spec_from_file_location("feedback_organ", ORGAN)
organ = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(organ)


# --------------------------------------------------------------------------- #
# Skip gates                                                                   #
# --------------------------------------------------------------------------- #

def test_empty_state_is_conservative_skip():
    r = organ.decide({})
    assert r["output"]["process"] is False
    assert r["output"]["skip_reason"] == "empty_state"
    assert r["output"]["issue"] is None
    assert r["self_metric"]["confidence"] <= 0.5


def test_status_not_processable_skips():
    r = organ.decide({"status": "draft", "summary": "x", "transcript": [{"answer": "x"}]})
    assert r["output"]["process"] is False
    assert r["output"]["skip_reason"] == "status_not_processable"
    assert r["self_metric"]["decision_path"] == "status_gate"


def test_in_progress_no_answers_skips():
    r = organ.decide({
        "status": "in_progress",
        "answer_count": 0,
        "summary": "anything",
        "transcript": [{"question": "q", "answer": "a"}],
    })
    assert r["output"]["process"] is False
    assert r["output"]["skip_reason"] == "in_progress_no_answers"


def test_in_progress_with_answers_processes():
    r = organ.decide({
        "status": "in_progress",
        "answer_count": 2,
        "summary": "Button does nothing",
        "transcript": [{"question": "what happened", "answer": "Button does nothing"}],
    })
    assert r["output"]["process"] is True
    assert r["output"]["issue"]["status"] == "in_progress"


def test_no_user_input_skips():
    r = organ.decide({"status": "completed", "summary": "", "transcript": []})
    assert r["output"]["process"] is False
    assert r["output"]["skip_reason"] == "no_user_input"
    assert r["self_metric"]["decision_path"] == "input_gate"


# --------------------------------------------------------------------------- #
# Type classification                                                          #
# --------------------------------------------------------------------------- #

def _completed(facts=None, summary="A summary", transcript=None, user=None):
    return {
        "status": "completed",
        "answer_count": 1,
        "facts": facts or [],
        "summary": summary,
        "transcript": transcript if transcript is not None else [],
        "source_interview_id": 42,
        "user": user,
    }


def test_classify_bug_from_bug_description():
    r = organ.decide(_completed(facts=[{"theme": "bug_description", "fact": "Save crashes"}]))
    assert r["output"]["issue"]["type"] == "bug"


def test_classify_bug_from_reproduction_steps():
    r = organ.decide(_completed(facts=[{"theme": "reproduction_steps", "fact": "1. click"}]))
    assert r["output"]["issue"]["type"] == "bug"


def test_classify_feature_request():
    r = organ.decide(_completed(facts=[{"theme": "feature_request", "fact": "Add dark mode"}]))
    assert r["output"]["issue"]["type"] == "feature_request"


def test_classify_defaults_to_improvement():
    r = organ.decide(_completed(facts=[{"theme": "general", "fact": "It's slow"}]))
    assert r["output"]["issue"]["type"] == "improvement"


# --------------------------------------------------------------------------- #
# Severity                                                                     #
# --------------------------------------------------------------------------- #

def test_severity_critical():
    r = organ.decide(_completed(facts=[{"theme": "severity", "fact": "App crash on every save"}]))
    assert r["output"]["issue"]["severity"] == "critical"


def test_severity_major():
    r = organ.decide(_completed(facts=[{"theme": "severity", "fact": "The total is wrong"}]))
    assert r["output"]["issue"]["severity"] == "major"


def test_severity_defaults_minor():
    r = organ.decide(_completed(facts=[{"theme": "severity", "fact": "slightly off colour"}]))
    assert r["output"]["issue"]["severity"] == "minor"


def test_severity_minor_when_no_severity_fact():
    r = organ.decide(_completed(facts=[]))
    assert r["output"]["issue"]["severity"] == "minor"


# --------------------------------------------------------------------------- #
# Title + description                                                          #
# --------------------------------------------------------------------------- #

def test_title_from_bug_description_fact():
    r = organ.decide(_completed(facts=[{"theme": "bug_description", "fact": "Export button is dead"}]))
    assert r["output"]["issue"]["title"] == "Export button is dead"


def test_title_falls_back_to_summary():
    r = organ.decide(_completed(summary="Pricing page typo"))
    assert r["output"]["issue"]["title"] == "Pricing page typo"


def test_title_falls_back_to_first_transcript_answer():
    r = organ.decide(_completed(
        summary="",
        transcript=[{"question": "what", "answer": "The map tiles never load"}],
    ))
    assert r["output"]["issue"]["title"] == "The map tiles never load"


def test_description_built_from_transcript_when_no_summary():
    r = organ.decide(_completed(
        summary="",
        transcript=[{"answer": "first"}, {"answer": "second"}],
    ))
    assert r["output"]["issue"]["description"] == "first; second"


# --------------------------------------------------------------------------- #
# Action status + history                                                      #
# --------------------------------------------------------------------------- #

def test_action_status_open():
    r = organ.decide(_completed(facts=[]))
    assert r["output"]["issue"]["action_status"] == "open"


def test_action_status_actioned():
    r = organ.decide(_completed(facts=[
        {"theme": "action_taken", "fact": "Investigating", "status": "in_progress"},
    ]))
    assert r["output"]["issue"]["action_status"] == "actioned"


def test_action_status_resolved():
    r = organ.decide(_completed(facts=[
        {"theme": "action_taken", "fact": "Fixed", "status": "resolved"},
    ]))
    assert r["output"]["issue"]["action_status"] == "resolved"


def test_action_history_sorted_and_prefix_stripped():
    r = organ.decide(_completed(facts=[
        {"theme": "action_taken", "fact": "Feedback actioned: second", "actioned_at": "2026-06-02", "actioned_by": "Matt", "evidence": "http://pr/2"},
        {"theme": "action_taken", "fact": "Feedback actioned: first", "actioned_at": "2026-06-01", "actioned_by": "Dev"},
    ]))
    hist = r["output"]["issue"]["action_history"]
    assert [h["action_taken"] for h in hist] == ["first", "second"]
    assert hist[0]["pr_url"] is None
    assert hist[1]["pr_url"] == "http://pr/2"


# --------------------------------------------------------------------------- #
# Browser context + user                                                      #
# --------------------------------------------------------------------------- #

def test_browser_context_extracted():
    ctx = {"url": "https://app/x", "consoleErrors": ["boom"], "networkErrors": [], "viewport": "1920x1080"}
    r = organ.decide(_completed(facts=[{"theme": "context", "evidence": json.dumps(ctx)}]))
    issue = r["output"]["issue"]
    assert issue["page_url"] == "https://app/x"
    assert issue["console_errors"] == ["boom"]
    assert issue["viewport"] == "1920x1080"


def test_browser_context_malformed_json_is_safe():
    r = organ.decide(_completed(facts=[{"theme": "context", "evidence": "{not json"}]))
    issue = r["output"]["issue"]
    assert issue["browser_context"] is None
    assert issue["console_errors"] == []
    assert issue["page_url"] is None


def test_user_email_and_name_passthrough():
    r = organ.decide(_completed(user={"email": "a@b.com", "display_name": "Ann"}))
    assert r["output"]["issue"]["user_email"] == "a@b.com"
    assert r["output"]["issue"]["user_name"] == "Ann"


def test_internal_email_is_stripped():
    r = organ.decide(_completed(user={"email": "synthetic.user.internal", "display_name": "Bot"}))
    assert r["output"]["issue"]["user_email"] is None
    assert r["output"]["issue"]["user_name"] is None


# --------------------------------------------------------------------------- #
# self_metric + determinism                                                    #
# --------------------------------------------------------------------------- #

def test_self_metric_shape_on_process():
    r = organ.decide(_completed(
        facts=[{"theme": "bug_description", "fact": "x"}],
        transcript=[{"answer": "x"}],
    ))
    sm = r["self_metric"]
    assert "confidence" in sm
    assert sm["feedback_type"] == "bug"
    assert sm["transcript_turns"] == 1
    assert sm["facts_evaluated"] == 1


def test_deterministic():
    s = _completed(facts=[{"theme": "severity", "fact": "crash"}], summary="boom")
    assert organ.decide(s) == organ.decide(s)


# --------------------------------------------------------------------------- #
# Entrypoint                                                                   #
# --------------------------------------------------------------------------- #

def test_entrypoint_stdin_roundtrip():
    payload = {"state": _completed(summary="a real bug")}
    p = subprocess.run(
        [sys.executable, str(ORGAN)],
        input=json.dumps(payload), capture_output=True, text=True,
    )
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["output"]["process"] is True
    assert "confidence" in out["self_metric"]


def test_entrypoint_rejects_malformed_input():
    p = subprocess.run(
        [sys.executable, str(ORGAN)],
        input="not json", capture_output=True, text=True,
    )
    assert p.returncode == 1


# --------------------------------------------------------------------------- #
# Sample conformance — PIN each committed sample to its verdict.               #
#                                                                              #
# The conformance Action only shadow-PRINTS sample output to the job summary;  #
# it never asserts. Without this test a sample verdict (or the organ logic     #
# that produces it) could flip and CI would stay green. These assertions are   #
# the actual gate.                                                             #
# --------------------------------------------------------------------------- #

SAMPLES_DIR = Path(__file__).parent / "samples"

# Pinned verdict per sample file. Keys must stay in lock-step with the files
# on disk (see test_sample_set_matches_pins for the drift guard).
_EXPECTED = {
    "bug_critical.json": {
        "process": True,
        "skip_reason": None,
        "type": "bug",
        "severity": "critical",
        "action_status": "open",
        "title": "Save button crashes the page on >10 rows",
        "page_url": "https://app.dataflowadvisory.com/blueprint",
        "user_email": "ruth@yourpropertyzone.com",
        "decision_path": "processed",
        "confidence": 1.0,
    },
    "feature_request_resolved.json": {
        "process": True,
        "skip_reason": None,
        "type": "feature_request",
        "severity": "minor",
        "action_status": "resolved",
        "title": "It would be great to export the blueprint as a PDF.",
        "page_url": None,
        "user_email": "neil.mcshane@av-dawson.com",
        "decision_path": "processed",
        "confidence": 1.0,
    },
    "improvement_actioned.json": {
        "process": True,
        "skip_reason": None,
        "type": "improvement",
        "severity": "minor",
        "action_status": "actioned",
        "title": "The dashboard feels sluggish when I switch between tenants.",
        "page_url": None,
        # Internal synthetic email must be scrubbed.
        "user_email": None,
        "decision_path": "processed",
        "confidence": 1.0,
    },
    "skip_in_progress_no_answers.json": {
        "process": False,
        "skip_reason": "in_progress_no_answers",
        "type": None,
        "severity": None,
        "action_status": None,
        "title": None,
        "page_url": None,
        "user_email": None,
        "decision_path": "answer_gate",
        "confidence": 1.0,
    },
}


def _load_sample(name):
    payload = json.loads((SAMPLES_DIR / name).read_text())
    return organ.decide(payload["state"], payload.get("context"))


@pytest.mark.parametrize("name", sorted(_EXPECTED))
def test_samples_conform(name):
    exp = _EXPECTED[name]
    r = _load_sample(name)
    out, sm = r["output"], r["self_metric"]

    assert out["process"] is exp["process"], name
    assert out["skip_reason"] == exp["skip_reason"], name
    assert sm["decision_path"] == exp["decision_path"], name
    assert sm["confidence"] == exp["confidence"], name

    issue = out["issue"]
    if exp["process"]:
        assert issue is not None, name
        assert issue["type"] == exp["type"], name
        assert issue["severity"] == exp["severity"], name
        assert issue["action_status"] == exp["action_status"], name
        assert issue["title"] == exp["title"], name
        assert issue["page_url"] == exp["page_url"], name
        assert issue["user_email"] == exp["user_email"], name
        # self_metric mirrors the issue triage fields.
        assert sm["feedback_type"] == exp["type"], name
        assert sm["severity"] == exp["severity"], name
        assert sm["action_status"] == exp["action_status"], name
    else:
        assert issue is None, name


def test_sample_set_matches_pins():
    """Drift guard: every sample on disk is pinned, and vice versa."""
    on_disk = {p.name for p in SAMPLES_DIR.glob("*.json")}
    pinned = set(_EXPECTED)
    assert on_disk == pinned, (
        f"sample/pin drift: on_disk-only={on_disk - pinned}, "
        f"pinned-only={pinned - on_disk}"
    )
