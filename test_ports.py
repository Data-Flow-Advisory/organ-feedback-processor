"""Tests for the connection-standard ports manifest (ports.json).

Mirrors ports_validate.py under pytest so the port checks run in the suite as
well as the standalone CI step. Also asserts the typed-port tolerance added to
decide(): a KeyFacts / InterviewTranscript producer's payload snaps into the
`facts` / `transcript` ports unchanged (a real stud, not a decorative one).
"""
import importlib.util
import json
from pathlib import Path

import pytest

import ports_validate as pv

ROOT = Path(__file__).parent

ORGAN = ROOT / "organ.py"
_spec = importlib.util.spec_from_file_location("feedback_organ_pt", ORGAN)
organ = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(organ)


# --------------------------------------------------------------------------- #
# Manifest well-formedness + vocabulary membership                            #
# --------------------------------------------------------------------------- #

def test_ports_json_parses_and_is_wellformed():
    ports = pv.load_ports()
    assert ports["inputs"] and ports["outputs"]
    for p in ports["inputs"] + ports["outputs"]:
        assert isinstance(p["name"], str) and p["name"]
        assert isinstance(p["type"], str) and p["type"]


def test_every_declared_type_exists_in_vocabulary():
    ports = pv.load_ports()
    vocab = pv.load_vocabulary()
    pv.check_types_exist(ports, vocab)  # raises on any unknown type


def test_decide_reads_each_input_and_writes_each_output():
    ports = pv.load_ports()
    pv.check_reads_and_writes(ports, organ)  # raises if a name is never touched


def test_validator_main_exits_zero():
    assert pv.main() == 0


def test_declared_ports_are_the_expected_names():
    ports = pv.load_ports()
    assert [(p["name"], p["type"]) for p in ports["inputs"]] == [
        ("transcript", "InterviewTranscript"),
        ("facts", "KeyFacts"),
    ]
    assert [(p["name"], p["type"]) for p in ports["outputs"]] == [
        ("issue", "FeedbackIssue"),
    ]


# --------------------------------------------------------------------------- #
# Typed-port tolerance — the ports are TRUE connections, not name-coincidence  #
# --------------------------------------------------------------------------- #

def _base_state():
    return {
        "status": "completed",
        "answer_count": 1,
        "summary": "Save button crashes the page",
        "source_interview_id": 7,
        "user": {"email": "a@b.com", "display_name": "Ann"},
    }


def test_facts_port_accepts_keyfacts_wrapper():
    """A KeyFacts payload ({"facts": [...]}) wires into the `facts` port and
    classifies identically to the bare-array form."""
    bare = dict(_base_state(), facts=[{"theme": "bug_description", "fact": "Save crashes"}],
                transcript=[{"question": "q", "answer": "a"}])
    wrapped = dict(_base_state(), facts={"facts": [{"theme": "bug_description", "fact": "Save crashes"}]},
                   transcript=[{"question": "q", "answer": "a"}])
    rb, rw = organ.decide(bare), organ.decide(wrapped)
    assert rw["output"]["process"] is True
    assert rw["output"]["issue"]["type"] == "bug"
    assert rw["output"]["issue"]["type"] == rb["output"]["issue"]["type"]
    assert rw["self_metric"]["facts_evaluated"] == 1


def test_transcript_port_accepts_interviewtranscript_wrapper():
    """An InterviewTranscript payload ({"interview_id", "qa":[...]}) wires into
    the `transcript` port; its qa pairs drive title/description as before."""
    qa = [{"question": "what happened", "answer": "The map tiles never load"}]
    bare = dict(_base_state(), summary="", facts=[], transcript=qa)
    wrapped = dict(_base_state(), summary="", facts=[],
                   source_interview_id=None,
                   transcript={"interview_id": 99, "qa": qa})
    rb, rw = organ.decide(bare), organ.decide(wrapped)
    assert rw["output"]["process"] is True
    assert rw["output"]["issue"]["title"] == "The map tiles never load"
    assert rw["output"]["issue"]["title"] == rb["output"]["issue"]["title"]
    assert rw["self_metric"]["transcript_turns"] == 1
    # interview_id is recovered from the typed wrapper when not given separately.
    assert rw["output"]["issue"]["source_interview_id"] == 99


def test_bare_array_inputs_still_work_unchanged():
    """Backward compatibility: the historical bare-array form is untouched."""
    s = dict(_base_state(), facts=[{"theme": "feature_request", "fact": "dark mode"}],
             transcript=[{"answer": "please add dark mode"}])
    r = organ.decide(s)
    assert r["output"]["issue"]["type"] == "feature_request"


# --------------------------------------------------------------------------- #
# Output type is genuinely new (proposed upstream)                            #
# --------------------------------------------------------------------------- #

def test_feedback_issue_type_is_marked_proposed():
    """FeedbackIssue is minted in the vendored vocab as PROPOSED — it has no
    upstream equivalent and is proposed in this PR."""
    vocab = json.loads((ROOT / "types.json").read_text())
    fi = vocab["types"]["FeedbackIssue"]
    assert fi.get("_status") == "PROPOSED"
    assert "organ-feedback-processor" in fi["produced_by_eg"]
