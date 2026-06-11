#!/usr/bin/env python3
"""Conformance check for the connection-standard ports manifest (CONNECTORS.md).

Asserts the three things the standard's conformance gate requires of an organ's
``ports.json``:

  1. ``ports.json`` parses and is well-formed (inputs/outputs, name+type each).
  2. Every declared ``type`` exists in the shared vocabulary (``types.json``,
     vendored here — the upstream registry lives in the private orchestrator repo).
  3. ``decide`` actually READS each declared input ``name`` under ``state`` and
     WRITES each declared output ``name`` under ``output`` — sampled against the
     organ's own committed samples, so a port can't be declared for a key the
     organ never touches.

Pure-stdlib, no third-party deps; exits non-zero on the first failure so it can
gate CI. ``test_ports.py`` exercises the same checks under pytest.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PORTS = ROOT / "ports.json"
TYPES = ROOT / "types.json"
ORGAN = ROOT / "organ.py"
SAMPLES_DIR = ROOT / "samples"


def _load_organ():
    spec = importlib.util.spec_from_file_location("feedback_organ", ORGAN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _TrackingDict(dict):
    """A dict that records which keys were actually read, to prove decide()
    reads each declared input name (reads via ``.get`` or ``[]``)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.reads: set = set()

    def get(self, key, default=None):
        self.reads.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.reads.add(key)
        return super().__getitem__(key)


def load_ports() -> dict:
    ports = json.loads(PORTS.read_text())
    if not isinstance(ports, dict):
        raise ValueError("ports.json must be a JSON object")
    for side in ("inputs", "outputs"):
        if side not in ports or not isinstance(ports[side], list):
            raise ValueError(f"ports.json missing list '{side}'")
        for p in ports[side]:
            if not isinstance(p, dict) or "name" not in p or "type" not in p:
                raise ValueError(f"each {side} port needs 'name' and 'type': {p!r}")
            if side == "inputs" and "required" in p and not isinstance(p["required"], bool):
                raise ValueError(f"input port 'required' must be bool: {p!r}")
    return ports


def load_vocabulary() -> set:
    vocab = json.loads(TYPES.read_text())
    types = vocab.get("types")
    if not isinstance(types, dict) or not types:
        raise ValueError("types.json has no 'types' map")
    return set(types)


def check_types_exist(ports: dict, vocab: set) -> None:
    for side in ("inputs", "outputs"):
        for p in ports[side]:
            if p["type"] not in vocab:
                raise ValueError(
                    f"{side} port {p['name']!r} declares type {p['type']!r} "
                    f"which is not in the vocabulary (types.json)."
                )


def check_reads_and_writes(ports: dict, organ) -> None:
    in_names = [p["name"] for p in ports["inputs"]]
    out_names = [p["name"] for p in ports["outputs"]]

    samples = sorted(SAMPLES_DIR.glob("*.json"))
    if not samples:
        raise ValueError("no samples/*.json to verify reads/writes against")

    reads_seen: set = set()
    writes_seen: set = set()
    for s in samples:
        payload = json.loads(s.read_text())
        tracked = _TrackingDict(payload["state"])
        result = organ.decide(tracked, payload.get("context"))
        reads_seen |= tracked.reads
        out = (result or {}).get("output") or {}
        if isinstance(out, dict):
            writes_seen |= set(out)

    missing_reads = [n for n in in_names if n not in reads_seen]
    if missing_reads:
        raise ValueError(
            f"declared input port(s) never read by decide() across samples: {missing_reads}"
        )
    missing_writes = [n for n in out_names if n not in writes_seen]
    if missing_writes:
        raise ValueError(
            f"declared output port(s) never written under 'output' across samples: {missing_writes}"
        )


def main() -> int:
    try:
        ports = load_ports()
        vocab = load_vocabulary()
        check_types_exist(ports, vocab)
        organ = _load_organ()
        check_reads_and_writes(ports, organ)
    except Exception as e:  # noqa: BLE001 — surface the message, fail the gate
        print(f"PORTS CONFORMANCE FAILED: {e}", file=sys.stderr)
        return 1

    print("ports.json conformance OK:")
    print(f"  inputs : {[(p['name'], p['type']) for p in ports['inputs']]}")
    print(f"  outputs: {[(p['name'], p['type']) for p in ports['outputs']]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
