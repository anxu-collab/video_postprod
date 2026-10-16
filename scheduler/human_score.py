# -*- coding: utf-8 -*-
"""Human scoring table over Clef candidates. Clef is advisory; humans are authority."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from workers.common import ROOT, atomic_json, digest

SCHEMA_VERSION = "human-score-table/v1"
SCHEMA_PATH = ROOT / "contracts" / "human-score-table-v1.schema.json"
ARCHIVE_DIR = ROOT / "state" / "human-score-tables"
LEDGER_PATH = ARCHIVE_DIR / "index.jsonl"
TABLE_NAME = "human-score-table.json"
HOLD_CODE = "HUMAN_SCORE_REQUIRED"
CONFIRM_KIND = "HUMAN_SCORE"


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_schema():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _sha_json(obj):
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _read_json(path):
    p = Path(path)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def table_path(work):
    return Path(work) / TABLE_NAME


def file_sha(path):
    p = Path(path)
    if not p.is_file():
        return None
    return digest(p)


def clef_status_from_audit(audit):
    if not audit:
        return {"mode": "unknown", "status": "unavailable", "fallback_reason": "CLEF_AUDIT_MISSING", "rule_version": None}
    mode = audit.get("mode") or "shadow"
    if mode == "off":
        return {"mode": mode, "status": "off", "fallback_reason": None, "rule_version": audit.get("rule_version")}
    reason = audit.get("fallback_reason")
    model = audit.get("model") or {}
    if reason:
        low = str(reason).lower()
        status = "unavailable" if any(x in low for x in ("gpu", "memory", "budget", "timeout", "unavailable")) else "error"
        return {"mode": mode, "status": status, "fallback_reason": str(reason)[:500], "rule_version": audit.get("rule_version")}
    sentences = model.get("sentences") or []
    if sentences and all(s.get("status") == "OK" for s in sentences):
        return {"mode": mode, "status": "ok", "fallback_reason": None, "rule_version": audit.get("rule_version")}
    if not sentences:
        return {"mode": mode, "status": "unavailable", "fallback_reason": reason or "CLEF_NO_SENTENCE_ROWS",
                "rule_version": audit.get("rule_version")}
    return {"mode": mode, "status": "error", "fallback_reason": reason or "CLEF_PARTIAL_OR_FAILED",
            "rule_version": audit.get("rule_version")}


def _candidate_score(row, candidate_id):
    for item in row.get("candidate_results") or []:
        if item.get("candidate_id") == candidate_id:
            for ev in reversed(item.get("evaluations") or []):
                if ev.get("score") is not None:
                    return ev.get("score")
            return None
    return None


def _clip_for_sentence(decision, sentence_id):
    for clip in decision.get("clips") or []:
        if clip.get("source") == "pool" and sentence_id in (clip.get("sentence_ids") or []):
            return clip
    return None


def build_template(job_id, revision, work, decision, clef_audit, library, snapshot, script_plan):
    """Build a pending human-score-table prefilled with Clef ranks."""
    work = Path(work)
    audit_path = work / "clef-audit.json"
    lib_path = work / "shot-library.json"
    snap_path = work / "clef-candidates.json"
    # Prefer on-disk hashes when files already written.
    audit_sha = file_sha(audit_path) or _sha_json(clef_audit)
    library_sha = file_sha(lib_path) or library.get("library_sha256") or _sha_json(library)
    snapshot_sha = file_sha(snap_path) or _sha_json(snapshot)

    model = (clef_audit or {}).get("model") or {}
    model_rows = {r.get("sentence_id"): r for r in (model.get("sentences") or []) if r.get("sentence_id")}
    by_clef = {c.get("candidate_id"): c for c in (snapshot.get("candidates") or []) if c.get("candidate_id")}
    script_by_id = {s["sentence_id"]: s for s in (script_plan.get("sentences") or [])}

    # Prefer body sentences from narration layout in decision; else script order.
    sentence_ids = []
    for s in ((decision.get("narration") or {}).get("sentences") or []):
        if s.get("sentence_id"):
            sentence_ids.append(s["sentence_id"])
    if not sentence_ids:
        sentence_ids = [s["sentence_id"] for s in script_plan.get("sentences") or []]

    sentences = []
    for sid in sentence_ids:
        row = model_rows.get(sid) or {}
        ranked = list(row.get("ranked_ids") or [])
        # Include eligible snapshot candidates not ranked (Clef failed) so humans can still pick.
        extras = []
        for c in snapshot.get("candidates") or []:
            cid = c.get("candidate_id")
            if not cid or cid in ranked:
                continue
            if c.get("selection_eligible") and not (c.get("native") or {}).get("fixed_role") and not (c.get("native") or {}).get("speech_locked"):
                extras.append(cid)
        ordered = ranked + extras
        candidates = []
        for idx, cid in enumerate(ordered):
            c = by_clef.get(cid) or {}
            native = c.get("native") or {}
            candidates.append({
                "candidate_id": cid,
                "library_candidate_id": native.get("candidate_id"),
                "source_sha256": c.get("source_sha256") or native.get("source_sha256"),
                "asset_id": c.get("asset_id") or native.get("source_asset_id"),
                "in_tick": c.get("in_tick") if c.get("in_tick") is not None else native.get("in_tick"),
                "out_tick": c.get("out_tick") if c.get("out_tick") is not None else native.get("out_tick"),
                "clef_rank": (idx + 1) if cid in ranked else None,
                "clef_score": _candidate_score(row, cid),
                "selection_eligible": bool(c.get("selection_eligible")),
                "description": str(c.get("description") or native.get("product_description") or "")[:500],
                "gate_reasons": list(c.get("gate_reasons") or []),
            })
        clip = _clip_for_sentence(decision, sid)
        script = script_by_id.get(sid) or {}
        sentences.append({
            "sentence_id": sid,
            "script_text": script.get("text") or "",
            "clip_id": (clip or {}).get("clip_id"),
            "clef_ranked_ids": ranked,
            "clef_preferred_id": (row.get("preferred_ids") or [None])[0],
            "candidates": candidates,
            "user_rank": None,
            "user_score": None,
            "selected": None,
            "user_selected_id": None,
            "note": "",
        })

    table = {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "revision": int(revision),
        "status": "pending",
        "authority": "human_over_clef",
        "operator_id": "",
        "created_utc": _utc(),
        "submitted_utc": None,
        "bindings": {
            "clef_audit_sha256": audit_sha,
            "library_sha256": library_sha,
            "snapshot_sha256": snapshot_sha,
        },
        "clef_ref": clef_status_from_audit(clef_audit),
        "sentences": sentences,
    }
    _load_schema().validate(table)
    return table


def ensure_pending_table(job_id, revision, work, decision, clef_audit, library, snapshot, script_plan):
    """Write pending table if missing or stale vs current bindings. Never overwrite submitted matching bindings."""
    work = Path(work)
    path = table_path(work)
    existing = _read_json(path)
    desired = build_template(job_id, revision, work, decision, clef_audit, library, snapshot, script_plan)
    if existing and existing.get("status") == "submitted":
        if existing.get("bindings") == desired["bindings"]:
            return existing, False
        # stale submitted — keep archive via ledger already; rewrite pending
    if existing and existing.get("status") == "pending" and existing.get("bindings") == desired["bindings"]:
        return existing, False
    atomic_json(path, desired)
    return desired, True


def validate_submission(table, work, *, operator_id):
    """Validate a human-filled table against live bindings and selection rules."""
    work = Path(work)
    validator = _load_schema()
    doc = copy.deepcopy(table)
    doc["operator_id"] = (operator_id or doc.get("operator_id") or "").strip()
    doc["status"] = "submitted"
    if not doc["operator_id"]:
        raise ValueError("OPERATOR_ID_REQUIRED")
    validator.validate(doc)

    live_audit = file_sha(work / "clef-audit.json")
    live_lib = file_sha(work / "shot-library.json")
    live_snap = file_sha(work / "clef-candidates.json")
    bindings = doc["bindings"]
    if live_audit and bindings["clef_audit_sha256"] != live_audit:
        raise ValueError("HUMAN_SCORE_STALE_CLEF_AUDIT")
    if live_lib and bindings["library_sha256"] != live_lib:
        raise ValueError("HUMAN_SCORE_STALE_LIBRARY")
    if live_snap and bindings["snapshot_sha256"] != live_snap:
        raise ValueError("HUMAN_SCORE_STALE_SNAPSHOT")

    selected_rows = []
    for row in doc["sentences"]:
        eligible = [c for c in row.get("candidates") or [] if c.get("selection_eligible")]
        # Sentences with a pool clip (clip_id) and at least one eligible candidate must be selected.
        needs = bool(row.get("clip_id")) and bool(eligible)
        if not needs:
            if row.get("selected") is True:
                raise ValueError("HUMAN_SCORE_SELECT_WITHOUT_ELIGIBLE:" + row["sentence_id"])
            continue
        if row.get("selected") is not True:
            raise ValueError("HUMAN_SCORE_SELECTION_REQUIRED:" + row["sentence_id"])
        cid = row.get("user_selected_id")
        if not cid:
            raise ValueError("HUMAN_SCORE_CANDIDATE_REQUIRED:" + row["sentence_id"])
        match = next((c for c in eligible if c["candidate_id"] == cid), None)
        if match is None:
            raise ValueError("HUMAN_SCORE_CANDIDATE_NOT_ELIGIBLE:" + row["sentence_id"])
        if row.get("user_score") is None or not (1 <= int(row["user_score"]) <= 5):
            raise ValueError("HUMAN_SCORE_SCORE_REQUIRED:" + row["sentence_id"])
        if row.get("user_rank") is None or int(row["user_rank"]) < 1:
            raise ValueError("HUMAN_SCORE_RANK_REQUIRED:" + row["sentence_id"])
        selected_rows.append(row)

    if not selected_rows:
        raise ValueError("HUMAN_SCORE_EMPTY_SELECTION")
    ranks = [int(r["user_rank"]) for r in selected_rows]
    if len(ranks) != len(set(ranks)):
        raise ValueError("HUMAN_SCORE_RANK_NOT_UNIQUE")

    doc["submitted_utc"] = _utc()
    return doc


def persist_submission(work, table):
    """Write submitted table, immutable archive copy, append ledger. Returns paths + sha."""
    work = Path(work)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = table_path(work)
    atomic_json(path, table)
    sha = file_sha(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_job = re.sub(r"[^A-Za-z0-9_.-]+", "_", table["job_id"])[:80]
    archive = ARCHIVE_DIR / f"{stamp}_{safe_job}_r{int(table['revision']):04d}_{sha[:16]}.json"
    archive.write_bytes(path.read_bytes())
    line = {
        "utc": table["submitted_utc"],
        "job_id": table["job_id"],
        "revision": table["revision"],
        "table_sha256": sha,
        "archive_path": str(archive.relative_to(ROOT)).replace("\\", "/"),
        "operator_id": table["operator_id"],
        "selected_ids": [r["user_selected_id"] for r in table["sentences"] if r.get("selected") is True],
        "clef_status": (table.get("clef_ref") or {}).get("status"),
        "bindings": table["bindings"],
    }
    with LEDGER_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return {"path": str(path), "archive_path": str(archive), "table_sha256": sha, "ledger": str(LEDGER_PATH)}


def require_submitted_table(work, expected_bindings=None):
    """Return submitted table or raise HUMAN_SCORE_REQUIRED."""
    work = Path(work)
    doc = _read_json(table_path(work))
    if not doc or doc.get("status") != "submitted" or doc.get("authority") != "human_over_clef":
        raise ValueError(HOLD_CODE + ":pending_or_missing")
    if expected_bindings and doc.get("bindings") != expected_bindings:
        raise ValueError(HOLD_CODE + ":stale_bindings")
    # Live file hashes must still match.
    for key, fname in (
        ("clef_audit_sha256", "clef-audit.json"),
        ("library_sha256", "shot-library.json"),
        ("snapshot_sha256", "clef-candidates.json"),
    ):
        live = file_sha(work / fname)
        if live and doc["bindings"].get(key) != live:
            raise ValueError(HOLD_CODE + ":stale_" + key)
    return doc


def apply_to_decision(decision, table, library, script_plan, confirmed_windows=None, reusable_intervals=None):
    """Apply human selections onto the edit decision via operator replacements."""
    from scheduler.shot_planner import apply_replacements

    decision = copy.deepcopy(decision)
    lib_by_id = {c["candidate_id"]: c for c in (library.get("candidates") or [])}
    replacements = []
    for row in table.get("sentences") or []:
        if row.get("selected") is not True:
            continue
        clip_id = row.get("clip_id")
        if not clip_id:
            continue
        chosen = next((c for c in row.get("candidates") or [] if c["candidate_id"] == row.get("user_selected_id")), None)
        if not chosen:
            raise ValueError("HUMAN_SCORE_APPLY_CANDIDATE_MISSING:" + row["sentence_id"])
        lib_id = chosen.get("library_candidate_id")
        if not lib_id or lib_id not in lib_by_id:
            # Fallback: match library by sha + window
            lib_id = None
            for c in library.get("candidates") or []:
                if c.get("source_sha256") == chosen.get("source_sha256") and c.get("in_tick") == chosen.get("in_tick") and c.get("out_tick") == chosen.get("out_tick"):
                    lib_id = c["candidate_id"]
                    break
        if not lib_id or lib_id not in lib_by_id:
            raise ValueError("HUMAN_SCORE_LIBRARY_CANDIDATE_MISSING:" + row["sentence_id"])
        replacements.append({
            "clip_id": clip_id,
            "candidate_id": lib_id,
            "reason": "human score table: " + (row.get("note") or "operator selected"),
            "allow_source_window_overlap": True,
        })
    if replacements:
        decision = apply_replacements(
            decision, library, replacements,
            script_plan=script_plan,
            confirmed=confirmed_windows,
            reusable_intervals=reusable_intervals,
        )
    for clip in decision.get("clips") or []:
        if clip.get("source") == "pool":
            clip["match_reason"] = (clip.get("match_reason") or "") + " | authority=human_over_clef"
    decision["human_score"] = {
        "authority": "human_over_clef",
        "table_sha256": _sha_json({k: table[k] for k in ("bindings", "sentences", "operator_id", "submitted_utc") if k in table}),
        "operator_id": table.get("operator_id"),
    }
    return decision


def list_ledger(limit=100):
    if not LEDGER_PATH.is_file():
        return []
    lines = LEDGER_PATH.read_text(encoding="utf-8").splitlines()
    rows = []
    for line in lines[-max(1, int(limit)):]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    rows.reverse()
    return rows


def bindings_from_work(work):
    work = Path(work)
    return {
        "clef_audit_sha256": file_sha(work / "clef-audit.json"),
        "library_sha256": file_sha(work / "shot-library.json"),
        "snapshot_sha256": file_sha(work / "clef-candidates.json"),
    }
