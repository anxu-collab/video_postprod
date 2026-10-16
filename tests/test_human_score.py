# -*- coding: utf-8 -*-
"""Human score table: schema, hold gate, apply, ledger."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def _minimal_library():
    return {
        "library_sha256": "b" * 64,
        "candidates": [
            {
                "candidate_id": "cand-open",
                "source_asset_id": "a1",
                "source_sha256": "c" * 64,
                "source_path": "F:/x.mp4",
                "in_tick": 0,
                "out_tick": 96000,
                "use": "open_box",
                "supported_actions": [],
                "action_text": "open",
                "product_description": "box",
                "product_identity_verified": True,
                "occlusion": "none",
                "clarity": "clear",
                "speech_locked": False,
                "evidence": {"observation_texts": ["open box"]},
                "fixed_role": None,
                "usable": True,
                "unusable_reason": None,
            }
        ],
    }


def _minimal_snapshot():
    return {
        "schema_version": "clef-candidates/v1",
        "candidates": [
            {
                "candidate_id": "clef-aaa",
                "asset_id": "a1",
                "source_sha256": "c" * 64,
                "in_tick": 0,
                "out_tick": 96000,
                "selection_eligible": True,
                "gate_reasons": [],
                "description": "open box shot",
                "native": {
                    "candidate_id": "cand-open",
                    "source_asset_id": "a1",
                    "source_sha256": "c" * 64,
                    "in_tick": 0,
                    "out_tick": 96000,
                },
            }
        ],
    }


def _minimal_decision():
    return {
        "schema_version": "edit-decision/v1",
        "job_id": "job-hs",
        "revision": 1,
        "clips": [
            {
                "clip_id": "clip-001",
                "source": "pool",
                "candidate_id": None,
                "asset_id": "a1",
                "source_sha256": "c" * 64,
                "source_in_tick": 0,
                "source_out_tick": 48000,
                "timeline_start_tick": 0,
                "duration_tick": 48000,
                "sentence_ids": ["s001"],
                "match_reason": "baseline",
                "match_evidence": {},
            }
        ],
        "missing": [],
        "narration": {"audio_sha256": "d" * 64, "body_start_tick": 0, "sentences": [{"sentence_id": "s001", "in_tick": 0, "out_tick": 48000}]},
        "inputs": {},
        "tail_pause_ticks": 0,
        "compiled_timeline_sha256": None,
    }


def _minimal_audit():
    return {
        "schema_version": "clef-audit/v1",
        "mode": "shadow",
        "rule_version": "clef-choice/v2",
        "fallback_reason": "RuntimeError:CLEF_GPU_MEMORY_INSUFFICIENT",
        "model": {
            "sentences": [
                {
                    "sentence_id": "s001",
                    "status": "ERROR",
                    "ranked_ids": [],
                    "preferred_ids": [],
                    "alternatives": [],
                    "candidate_results": [],
                }
            ]
        },
        "sentences": [{"sentence_id": "s001", "text": "打开包装。"}],
    }


def test_schema_file_loads_and_accepts_pending_template(tmp_path):
    from scheduler.human_score import build_template, SCHEMA_PATH
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    work = tmp_path / "job"
    work.mkdir()
    decision = _minimal_decision()
    audit = _minimal_audit()
    library = _minimal_library()
    snapshot = _minimal_snapshot()
    script = {"sentences": [{"sentence_id": "s001", "text": "打开包装。"}], "product_terms": []}
    (work / "clef-audit.json").write_text(json.dumps(audit), encoding="utf-8")
    (work / "shot-library.json").write_text(json.dumps(library), encoding="utf-8")
    (work / "clef-candidates.json").write_text(json.dumps(snapshot), encoding="utf-8")
    table = build_template("job-hs", 1, work, decision, audit, library, snapshot, script)
    Draft202012Validator(schema).validate(table)
    assert table["status"] == "pending"
    assert table["clef_ref"]["status"] == "unavailable"
    assert table["sentences"][0]["candidates"][0]["candidate_id"] == "clef-aaa"


def test_require_submitted_blocks_until_submit(tmp_path):
    from scheduler.human_score import (
        ensure_pending_table, require_submitted_table, validate_submission,
        persist_submission, HOLD_CODE, apply_to_decision,
    )
    work = tmp_path / "job"
    work.mkdir()
    decision = _minimal_decision()
    audit = _minimal_audit()
    library = _minimal_library()
    snapshot = _minimal_snapshot()
    script = {"sentences": [{"sentence_id": "s001", "text": "打开包装。", "visual": {"required": []}}], "product_terms": []}
    (work / "clef-audit.json").write_text(json.dumps(audit), encoding="utf-8")
    (work / "shot-library.json").write_text(json.dumps(library), encoding="utf-8")
    (work / "clef-candidates.json").write_text(json.dumps(snapshot), encoding="utf-8")
    table, wrote = ensure_pending_table("job-hs", 1, work, decision, audit, library, snapshot, script)
    assert wrote and table["status"] == "pending"
    with pytest.raises(ValueError, match=HOLD_CODE):
        require_submitted_table(work)
    filled = copy.deepcopy(table)
    filled["sentences"][0].update({
        "selected": True,
        "user_selected_id": "clef-aaa",
        "user_rank": 1,
        "user_score": 4,
        "note": "开箱清楚",
    })
    submitted = validate_submission(filled, work, operator_id="tester")
    meta = persist_submission(work, submitted)
    assert Path(meta["archive_path"]).is_file()
    assert Path(meta["ledger"]).is_file()
    got = require_submitted_table(work)
    assert got["status"] == "submitted" and got["operator_id"] == "tester"


def test_pipeline_stage_e_orders_human_score_before_edit_decision():
    import ast
    source = (ROOT / "scheduler/pipeline.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run_v2")
    calls = [n for n in ast.walk(run) if isinstance(n, ast.Call)]
    clef = next(n for n in calls if isinstance(n.func, ast.Name) and n.func.id == "select_with_clef")
    require = next(n for n in calls if isinstance(n.func, ast.Name) and n.func.id == "require_submitted_table")
    apply = next(n for n in calls if isinstance(n.func, ast.Name) and n.func.id == "apply_to_decision")
    # edit-decision write after apply
    writes = []
    for n in ast.walk(run):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "atomic_json":
            if n.args and isinstance(n.args[0], ast.BinOp):
                # rough: look at constant 'edit-decision.json'
                seg = ast.get_source_segment(source, n)
                if seg and "edit-decision.json" in seg:
                    writes.append(n)
    assert writes, "edit-decision.json write missing"
    assert clef.lineno < require.lineno < apply.lineno <= min(w.lineno for w in writes)


def test_service_maps_human_score_hold_and_exposes_routes():
    source = (ROOT / "scheduler/service.py").read_text(encoding="utf-8")
    assert "HUMAN_SCORE_REQUIRED" in source and "AWAIT_HUMAN_SCORE" in source
    assert "/v2/jobs/{id}/human-score-table" in source
    assert "/v2/human-score-tables" in source


def test_ui_exposes_await_human_score_panel():
    app = (ROOT / "ui/app.js").read_text(encoding="utf-8")
    assert "AWAIT_HUMAN_SCORE" in app
    assert "renderHumanScore" in app
    assert "human-score-table" in app
