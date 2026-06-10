#!/usr/bin/env python3
"""
Feedback Processor Organ — extracted decision logic from discovery-engine.

A pure decider that turns the FACTS of a completed (or in-progress) feedback
interview into a structured, actionable issue: classify the type
(bug / feature_request / improvement), assess severity, generate a title, build a
description, derive browser/console/network context, and compute the action status
(open / actioned / resolved) plus the action history timeline.

This is the pure core of discovery-engine's ``app/services/feedback_processor.py``.
The DB fetch (loading the Interview, its Questions/Answers and the submitting User)
is the spine's job — the organ is *handed* those already-resolved facts in ``state``
and never touches a database, network, or clock.

Contract (see CONTRACT.md):
  INPUT state: {
    "status": "completed" | "in_progress" | str,   # interview status
    "answer_count": int,                            # pre-counted answers (in_progress gate)
    "facts": [ {theme, fact, evidence, status, ...}, ... ],  # interview.key_facts
    "summary": str,                                 # interview.summary
    "transcript": [ {"question": str, "answer": str}, ... ],  # pre-fetched Q&A
    "source_interview_id": int | null,
    "user": { "email": str | null, "display_name": str | null } | null
  }

  OUTPUT: {
    "output": {
      "process": bool,            # should this become a tracked issue?
      "skip_reason": str | null,  # why not, when process=False
      "issue": { ... } | null     # the structured issue (None when skipped)
    },
    "rationale": str,
    "self_metric": {
      "confidence": float,
      "decision_path": str,
      "facts_evaluated": int,
      "transcript_turns": int,
      "feedback_type": str | null,
      "severity": str | null,
      "action_status": str | null
    }
  }

The organ is pure: all inputs via JSON, no DB/network/clock calls, deterministic,
fail-safe to the conservative verdict (process=False, low confidence) on bad input.
"""
from __future__ import annotations

import json
import os
import sys

_PROCESSABLE_STATUSES = ("completed", "in_progress")

# Severity keyword ladders (mirrors discovery-engine _assess_severity).
_CRITICAL_WORDS = ("crash", "data loss", "can't use", "blocker")
_MAJOR_WORDS = ("broken", "wrong", "error", "fail")


def decide(state: dict, context: dict | None = None) -> dict:
    """Decide whether/how to turn a feedback interview into a structured issue."""
    context = context or {}

    try:
        if not isinstance(state, dict) or not state:
            return _skip("empty_state", "No state supplied — nothing to process.",
                         confidence=0.3, decision_path="empty_state")

        status = state.get("status")
        answer_count = state.get("answer_count", 0)
        facts = state.get("facts") or []
        summary = state.get("summary") or ""
        transcript = state.get("transcript") or []
        source_interview_id = state.get("source_interview_id")
        user = state.get("user") or None

        # --- Gate 1: status must be processable -----------------------------
        if status not in _PROCESSABLE_STATUSES:
            return _skip(
                "status_not_processable",
                f"Interview status {status!r} is not processable "
                f"(must be one of {_PROCESSABLE_STATUSES}).",
                confidence=1.0, decision_path="status_gate",
                facts=facts, transcript=transcript,
            )

        # --- Gate 2: in-progress interviews need at least one answer --------
        if status == "in_progress" and not answer_count:
            return _skip(
                "in_progress_no_answers",
                "In-progress interview has zero answers — too early to action.",
                confidence=1.0, decision_path="answer_gate",
                facts=facts, transcript=transcript,
            )

        # --- Gate 3: must contain real user input ---------------------------
        if not transcript and not summary:
            return _skip(
                "no_user_input",
                "No transcript and no summary — only browser context, nothing to action.",
                confidence=1.0, decision_path="input_gate",
                facts=facts, transcript=transcript,
            )

        # --- Build the structured issue -------------------------------------
        browser_context = _extract_browser_context(facts)

        description = summary
        if not description and transcript:
            description = "; ".join(t.get("answer", "") for t in transcript)[:500]

        feedback_type = _classify_type(facts, summary)
        severity = _assess_severity(facts)
        title = _generate_title(facts, summary)
        if title == "User feedback" and transcript:
            title = (transcript[0].get("answer") or "")[:100]

        action_status = classify_feedback_status(facts)
        action_history = _extract_action_history(facts)

        user_email = user.get("email") if isinstance(user, dict) else None
        user_name = user.get("display_name") if isinstance(user, dict) else None
        # Never expose internal synthetic emails.
        if user_email and user_email.endswith(".internal"):
            user_email = None
            user_name = None

        issue = {
            "type": feedback_type,
            "title": title,
            "description": description,
            "severity": severity,
            "facts": facts,
            "transcript": transcript,
            "source_interview_id": source_interview_id,
            "status": status,
            "action_status": action_status,
            "action_history": action_history,
            "user_email": user_email,
            "user_name": user_name,
            "browser_context": browser_context,
            "console_errors": browser_context.get("consoleErrors", []) if browser_context else [],
            "network_errors": browser_context.get("networkErrors", []) if browser_context else [],
            "page_url": browser_context.get("url") if browser_context else None,
            "viewport": browser_context.get("viewport") if browser_context else None,
        }

        # Confidence: full when we built from real transcript/summary; a touch
        # lower when the title had to fall back to a generic label.
        confidence = 1.0 if (summary or len(transcript) > 0) else 0.7
        if feedback_type == "bug" and severity == "minor" and not _has_severity_fact(facts):
            # Severity defaulted to "minor" with no explicit severity fact — slightly
            # less certain about the triage, but the issue is still valid.
            confidence = min(confidence, 0.85)

        return {
            "output": {
                "process": True,
                "skip_reason": None,
                "issue": issue,
            },
            "rationale": (
                f"Processed feedback into a {severity} {feedback_type}: "
                f"{title!r}. action_status={action_status}, "
                f"{len(transcript)} transcript turn(s), {len(facts)} fact(s)."
            ),
            "self_metric": {
                "confidence": confidence,
                "decision_path": "processed",
                "facts_evaluated": len(facts),
                "transcript_turns": len(transcript),
                "feedback_type": feedback_type,
                "severity": severity,
                "action_status": action_status,
            },
        }

    except Exception as e:  # malformed-but-non-empty state: conservative skip
        return _skip(
            "decision_error",
            f"Decision logic error (fail-safe skip): {e}",
            confidence=0.0, decision_path="error_fallback",
        )


# --------------------------------------------------------------------------- #
# Pure helpers (mirror discovery-engine feedback_processor.py)                  #
# --------------------------------------------------------------------------- #

def _classify_type(facts, summary) -> str:
    """Classify feedback as bug / feature_request / improvement from themes."""
    themes = [f.get("theme", "") for f in facts if isinstance(f, dict)]
    if "bug_description" in themes or "reproduction_steps" in themes:
        return "bug"
    if "feature_request" in themes:
        return "feature_request"
    return "improvement"


def _assess_severity(facts) -> str:
    """Assess severity from a severity-themed fact's content."""
    for f in facts:
        if isinstance(f, dict) and f.get("theme") == "severity":
            text = (f.get("fact") or "").lower()
            if any(w in text for w in _CRITICAL_WORDS):
                return "critical"
            if any(w in text for w in _MAJOR_WORDS):
                return "major"
    return "minor"


def _has_severity_fact(facts) -> bool:
    return any(isinstance(f, dict) and f.get("theme") == "severity" for f in facts)


def _generate_title(facts, summary) -> str:
    """Generate a short title from the bug-description fact or the summary."""
    for f in facts:
        if isinstance(f, dict) and f.get("theme") == "bug_description":
            return (f.get("fact") or "User feedback")[:100]
    return summary[:100] if summary else "User feedback"


def classify_feedback_status(facts: list) -> str:
    """Three-state classifier for a feedback item's action status.

    open     — no action_taken fact on the interview
    actioned — >=1 action_taken fact, none with status='resolved'
    resolved — any action_taken fact carries status='resolved'
    """
    action_facts = [
        f for f in (facts or [])
        if isinstance(f, dict) and f.get("theme") == "action_taken"
    ]
    if not action_facts:
        return "open"
    for f in action_facts:
        if f.get("status") == "resolved":
            return "resolved"
    return "actioned"


def _extract_action_history(facts: list) -> list:
    """Return time-sorted list of action_taken fact dicts for the dashboard."""
    history = []
    for f in (facts or []):
        if not isinstance(f, dict) or f.get("theme") != "action_taken":
            continue
        action_taken = f.get("fact", "")
        prefix = "Feedback actioned: "
        if action_taken.startswith(prefix):
            action_taken = action_taken[len(prefix):]
        history.append({
            "at": f.get("actioned_at", ""),
            "by": f.get("actioned_by", "—"),
            "action_taken": action_taken,
            "pr_url": f.get("evidence") or None,
        })
    history.sort(key=lambda h: h["at"])
    return history


def _extract_browser_context(facts) -> dict | None:
    """Pull the captured browser-context blob from the context-themed fact."""
    for f in facts:
        if isinstance(f, dict) and f.get("theme") == "context":
            try:
                return json.loads(f.get("evidence", "{}"))
            except (json.JSONDecodeError, TypeError):
                return None
    return None


# --------------------------------------------------------------------------- #
# Skip-verdict helper + entrypoint                                             #
# --------------------------------------------------------------------------- #

def _skip(reason: str, rationale: str, *, confidence: float, decision_path: str,
          facts=None, transcript=None) -> dict:
    return {
        "output": {
            "process": False,
            "skip_reason": reason,
            "issue": None,
        },
        "rationale": rationale,
        "self_metric": {
            "confidence": confidence,
            "decision_path": decision_path,
            "facts_evaluated": len(facts or []),
            "transcript_turns": len(transcript or []),
            "feedback_type": None,
            "severity": None,
            "action_status": None,
        },
    }


def main() -> int:
    path = os.environ.get("ORGAN_INPUT")
    raw = open(path).read() if path else sys.stdin.read()
    try:
        payload = json.loads(raw)
        state = payload["state"]
    except Exception as e:
        print(json.dumps({"error": f"invalid input: {e}"}), file=sys.stderr)
        return 1
    print(json.dumps(decide(state, payload.get("context")), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
