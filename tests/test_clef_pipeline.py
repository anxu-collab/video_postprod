"""Bounded scheduler integration checks; no model, synthesis or production DB."""
import ast
import json
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator


ROOT=Path(__file__).resolve().parents[1]


def test_schema_accepts_bounded_modes_and_rejects_unbounded_budget():
    schema=json.loads((ROOT/'contracts/edit-job-v2.schema.json').read_text(encoding='utf-8'))
    prop=schema['properties']['clef_selector']
    validator=Draft202012Validator(prop)
    for mode in ('off','shadow','active'):
        validator.validate({'mode':mode,'backend':'mock','timeout_sec':20,'max_requests':64,'max_retries':0})
    for invalid in ({'mode':'magic'},{'backend':'cloud'},{'max_requests':0},{'max_retries':90},{'timeout_sec':0}):
        assert list(validator.iter_errors(invalid))


def test_stage_a_snapshot_precedes_script_gate_and_stage_e_is_single_entry():
    source=(ROOT/'scheduler/pipeline.py').read_text(encoding='utf-8')
    tree=ast.parse(source)
    run=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='run_v2')
    calls=[n for n in ast.walk(run) if isinstance(n,ast.Call)]
    snapshots=[n for n in calls if isinstance(n.func,ast.Attribute) and n.func.attr=='_clef_snapshot']
    script_gate=next(n for n in calls if isinstance(n.func,ast.Attribute) and n.func.attr=='_gate' and n.args and isinstance(n.args[0],ast.Constant) and n.args[0].value=='SCRIPT_CONFIRM')
    assert snapshots and max(n.lineno for n in snapshots)<script_gate.lineno
    structure=next(n for n in calls if isinstance(n.func,ast.Name) and n.func.id=='review_selling_script')
    narration=next(n for n in calls if isinstance(n.func,ast.Name) and n.func.id=='prepare_narration')
    assert structure.lineno<script_gate.lineno<narration.lineno
    entries=[n for n in calls if isinstance(n.func,ast.Name) and n.func.id=='select_with_clef']
    assert len(entries)==1 and entries[0].lineno>script_gate.lineno


def test_usage_is_guarded_after_technical_render_success():
    source=(ROOT/'scheduler/pipeline.py').read_text(encoding='utf-8')
    tree=ast.parse(source)
    finish=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_finish')
    usage=next(n for n in ast.walk(finish) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='record_usage')
    guarded=next(n for n in ast.walk(finish) if isinstance(n,ast.If) and usage in list(ast.walk(n)))
    assert "qa['passed']" in ast.get_source_segment(source,guarded.test)
    assert 'render_evidence' in ast.get_source_segment(source,guarded)


def test_stage_a_failure_leaves_asset_snapshot_and_production_refusal(tmp_path):
    from scheduler.pipeline import Pipeline
    from scheduler.db import Store
    from scheduler.clef_store import ClefRepository
    pipeline=object.__new__(Pipeline)
    pipeline.id='failed-analysis';pipeline.rev=1;pipeline.work=tmp_path/'job';pipeline.work.mkdir()
    pipeline.store=Store(tmp_path/'audit.db')
    pipeline.job={'schema_version':'edit-job/v2','assets':[{'asset_id':'not-analyzed','role':'product',
        'sha256':'a'*64,'path':'F:/fixture/source.mp4'}]}
    pipeline.clef_stage_a={'analysis_refs':[],'library':{},'current_asset_id':'not-analyzed'}
    def fail():raise RuntimeError('NARRATO_INJECTED_FAILURE')
    pipeline.run_v2=fail
    with pytest.raises(RuntimeError,match='NARRATO_INJECTED_FAILURE'):pipeline.run()
    snapshot=json.loads((pipeline.work/'clef-candidates.json').read_text(encoding='utf-8'))
    assert snapshot['stage_status']=='ERROR' and snapshot['failed_asset_id']=='not-analyzed'
    assert len(snapshot['candidates'])==1
    assert not snapshot['candidates'][0]['selection_eligible']
    assert snapshot['candidates'][0]['quality_status']=='error'
    assert 'NARRATO_INJECTED_FAILURE' in snapshot['stage_error']
    assert ClefRepository(pipeline.store).view(pipeline.id,1)['snapshots']
