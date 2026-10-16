import copy,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def test_bgm_diff_only_audio_mix_and_mux():
    from scheduler.dag import revision_diff
    a=json.loads((ROOT/'contracts/examples/edit-job-ab.example.json').read_text(encoding='utf-8'));b=copy.deepcopy(a)
    b['revision']=2;b['parent_revision']=1;b['templates']['bgm']=None
    assert revision_diff(a,b)=={'music.select','mix.raw','mix.loudnorm','mux.final','review.audio'}
def test_subtitle_edit_does_not_invalidate_tts_asr():
    from scheduler.dag import revision_diff
    a=json.loads((ROOT/'contracts/examples/edit-job-ab.example.json').read_text(encoding='utf-8'));b=copy.deepcopy(a)
    b['overrides']['subtitle_edits']=[{'cue_id':'cue-1','text':'字幕修正'}]
    d=revision_diff(a,b);assert 'speech.synthesize' not in d and 'speech.transcribe' not in d and 'picture.caption' in d
def test_fencing_refuses_stale_publish_and_recovery_preserves_unknown(tmp_path):
    from scheduler.db import Store
    from scheduler.recovery import acquire_lease,require_lease,recover
    from providers.ledger import Ledger
    import pytest
    s=Store(tmp_path/'db.sqlite');a=acquire_lease(s,'gpu','old',ttl=-1);b=acquire_lease(s,'gpu','new')
    assert b>a
    with pytest.raises(RuntimeError):require_lease(s,'gpu','old',a)
    l=Ledger(s);l.reserve('k','job',100)
    report=recover(s);assert report['paid_unknown']==1
    assert l.snapshot()['held_micros']==100
