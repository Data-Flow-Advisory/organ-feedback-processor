# Feedback Processor Organ

A **pure decider** that turns the facts of a feedback interview into a structured,
actionable issue — extracted from discovery-engine's
`app/services/feedback_processor.py`.

It is the bridge logic between the discovery API (which collects and structures
feedback) and the self-healing loop (which fixes issues automatically): classify,
triage, title, and timeline a piece of user feedback — without ever touching a
database, network, or clock.

## What it does

Given the already-fetched facts of a feedback interview, the organ:

1. **Gates** — decides whether the feedback is even processable:
   - status must be `completed` or `in_progress`,
   - an `in_progress` interview must have at least one answer,
   - there must be real user input (a transcript or a summary), not just browser context.
2. **Classifies type** — `bug` / `feature_request` / `improvement` from fact themes.
3. **Assesses severity** — `critical` / `major` / `minor` from a severity-themed fact.
4. **Generates a title** — bug-description fact → summary → first transcript answer.
5. **Builds a description** — summary, or a joined transcript when no summary exists.
6. **Computes action status** — `open` / `actioned` / `resolved` from `action_taken` facts.
7. **Extracts action history** — a time-sorted timeline of who actioned what, with PR links.
8. **Derives browser context** — console/network errors, page URL, viewport.
9. **Scrubs internal emails** — synthetic `*.internal` addresses are never surfaced.

The DB fetch (loading the `Interview`, its `Question`/`Answer` rows, and the
submitting `User`) is the **spine's** job. The organ is *handed* those resolved
facts in `state`.

## Input contract

```json
{
  "state": {
    "status": "completed",
    "answer_count": 3,
    "summary": "The Save button crashes the page on >10 rows.",
    "transcript": [
      {"question": "What happened?", "answer": "The page crashed"}
    ],
    "source_interview_id": 1201,
    "user": {"email": "ruth@example.com", "display_name": "Ruth"},
    "facts": [
      {"theme": "bug_description", "fact": "Save crashes the page"},
      {"theme": "severity", "fact": "Total data loss, I can't use it"},
      {"theme": "context", "evidence": "{\"url\": \"...\", \"consoleErrors\": [\"...\"]}"}
    ]
  }
}
```

### Fields

- **status** (str): interview status. Processable when `completed` or `in_progress`.
- **answer_count** (int): pre-counted answers — gates `in_progress` interviews.
- **facts** (list[dict]): `interview.key_facts`. Each has a `theme` and `fact`;
  `context` facts carry an `evidence` JSON string; `action_taken` facts carry
  `status`, `actioned_at`, `actioned_by`, and `evidence` (PR URL).
- **summary** (str): `interview.summary`.
- **transcript** (list[dict]): pre-fetched `{question, answer}` pairs.
- **source_interview_id** (int|null): provenance.
- **user** (dict|null): `{email, display_name}` of the submitter.

`context` (optional) is accepted for orchestrator compatibility and is unused.

## Output contract

```json
{
  "output": {
    "process": true,
    "skip_reason": null,
    "issue": {
      "type": "bug",
      "title": "Save crashes the page",
      "description": "...",
      "severity": "critical",
      "action_status": "open",
      "action_history": [],
      "user_email": "ruth@example.com",
      "page_url": "...",
      "console_errors": ["..."],
      "...": "..."
    }
  },
  "rationale": "Processed feedback into a critical bug: ...",
  "self_metric": {
    "confidence": 1.0,
    "decision_path": "processed",
    "facts_evaluated": 3,
    "transcript_turns": 1,
    "feedback_type": "bug",
    "severity": "critical",
    "action_status": "open"
  }
}
```

When the feedback is not processable, `process` is `false`, `issue` is `null`, and
`skip_reason` is one of `empty_state`, `status_not_processable`,
`in_progress_no_answers`, `no_user_input`, `decision_error`.

## Run it

```bash
# from stdin
echo '{"state": {"status": "completed", "summary": "a bug"}}' | python3 organ.py

# from a sample file
ORGAN_INPUT=samples/bug_critical.json python3 organ.py
```

## Test

```bash
python -m pytest -q
```

## Purity guarantees

- No DB / network / filesystem / clock access in `decide()`.
- Deterministic given the same input.
- Fails safe to a conservative **skip** (never a confident-wrong "process this") on
  malformed or empty `state`.
- Stdlib-only.

See [`CONTRACT.md`](https://github.com/Data-Flow-Advisory) in the orchestrator for
the full organ interface. Conformance (shadow-run on the committed samples + the
test suite) runs in CI via `.github/workflows/conformance.yml`.
