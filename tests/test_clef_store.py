import json
import sqlite3
from pathlib import Path

import pytest

from scheduler.db import Store
from scheduler.clef_store import ClefRepository


def make_v1(path):
    db = sqlite3.connect(path)
    db.executescript((__import__('pathlib').Path(__file__).parents[1] / 'migrations/001.sql').read_text(encoding='utf-8'))
    db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)", ('j', 1, 'hash', None, 'DONE', None, 'now', '{"old":true}', None))
    db.commit()
    db.close()


def test_v1_migrates_in_place_and_preserves_history_reopen(tmp_path):
    path = tmp_path / 'jobs.sqlite'
    make_v1(path)
    repo = ClefRepository(Store(path))
    assert repo.store.get_job('j')['payload'] == {'old': True}
    assert repo.store.connection.execute('PRAGMA user_version').fetchone()[0] == 2
    assert ClefRepository(Store(path)).store.get_job('j')['state'] == 'DONE'


def test_append_only_snapshots_decisions_usage_idempotent_cache_and_queries(tmp_path):
    repo = ClefRepository(Store(tmp_path / 'jobs.sqlite'))
    snap = {'candidates': [{'candidate_id': 'c1', 'asset_id': 'a1', 'source_asset_id': 'a1',
                          'source_sha256': 'a' * 64, 'global_asset_id': 'asset-' + 'a' * 64}]}
    s1 = repo.record_snapshot('j', 1, snap)
    s2 = repo.record_snapshot('j', 1, snap)
    assert s1 != s2
    d1 = repo.record_decision('j', 1, snap, {'batches': []}, {'sentences': [{'sentence_id': 's1'}],
        'clips': [{'candidate_id': 'c1', 'source_sha256': 'a' * 64, 'source_in_tick': 10,
                   'source_out_tick': 20, 'timeline_start_tick': 30, 'duration_tick': 10,
                   'sentence_ids': ['s1']}]})
    d2 = repo.record_decision('j', 1, snap, {'batches': []}, {'sentences': []})
    assert d1 != d2
    timeline = {'render_evidence': {'status': 'SUCCEEDED', 'technical_status': 'PASS', 'output_sha256': 'f' * 64},
                'video_clips': [{'candidate_id': 'c1', 'base_source_ref': 'a1', 'source_in_tick': 10,
                                'source_out_tick': 20, 'timeline_start_tick': 30, 'duration_tick': 10}]}
    repo.record_usage('j', 1, d1, timeline)
    repo.record_usage('j', 1, d1, timeline)
    repo.record_usage('j', 1, d2, timeline)  # same rendered artifact, a later decision
    assert len(repo.view('j', 1)['usage']) == 1
    other_render = {**timeline, 'render_evidence': {**timeline['render_evidence'], 'output_sha256': 'e' * 64}}
    repo.record_usage('j', 1, d2, other_render)
    assert len(repo.view('j', 1)['usage']) == 2
    history = repo.asset_history('asset-' + 'a' * 64)
    assert len(history['usage']) == 2
    assert history['usage'][0]['clips'][0]['source_in_tick'] == 10
    assert history['usage'][0]['clips'][0]['sentence_ids'] == ['s1']
    assert history['usage'][0]['clips'][0]['timeline_end_tick'] == 40
    assert history['cumulative_count'] == 2
    assert repo.query_decisions(job_id='j')
    assert repo.query_decisions(job_id='j', asset_id='asset-' + 'a' * 64)
    assert repo.query_decisions(job_id='j', sentence_id='s1')
    assert not repo.query_decisions(job_id='j', sentence_id='unknown')
    assert repo.asset_history('a1')['decisions']  # recommended candidate history, even if not rendered
    assert repo.view('j', 1)['decisions'][0]['snapshot'] == snap
    cache_key = repo.cache_key('a' * 64, 'r1')
    repo.cache_put(cache_key, {'source_sha256': 'a' * 64, 'rule_version': 'r1', 'value': {'x': 1}})
    assert repo.cache_get(cache_key) == {'source_sha256': 'a' * 64, 'rule_version': 'r1', 'value': {'x': 1}}
    repo.cache_put(cache_key, {'raw_response': 'new'})
    assert repo.cache_get(cache_key) == {'source_sha256': 'a' * 64, 'rule_version': 'r1', 'value': {'x': 1}}
    with pytest.raises(ValueError, match='CACHE_KEY_MUST_BE_SHA256'):
        repo.cache_put('wrong', {'source_sha256': 'a' * 64, 'rule_version': 'r1'})
    with pytest.raises(ValueError, match='SUCCESSFUL_RENDER'):
        repo.record_usage('j', 1, d1, {'clips': [{'asset_id': 'a1'}]})
    assert len(repo.view('j', 1)['snapshots']) == 2
    assert len(repo.view('j', 1)['decisions']) == 2


def test_records_redact_nested_secret_keys_and_exception_text(tmp_path):
    repo = ClefRepository(Store(tmp_path / 'jobs.sqlite'))
    snapshot = {'candidates': []}
    repo.record_decision('j', 1, snapshot,
        {'error': 'Authorization: Bearer abcdef; password=hunter2 api_key: hidden', 'nested': {'api_key': 'secret'}},
        {'token': 'secret', 'sentences': []})
    result = repo.view('j', 1)['decisions'][0]
    encoded = json.dumps(result)
    assert all(v not in encoded for v in ('abcdef','hunter2','hidden','secret'))




def test_failed_v2_migration_rolls_back_all_schema_changes(tmp_path, monkeypatch):
    import scheduler.db as db_module
    root = tmp_path / 'migrations-root'
    (root / 'migrations').mkdir(parents=True)
    source = Path(__file__).parents[1] / 'migrations' / '001.sql'
    (root / 'migrations' / '001.sql').write_text(source.read_text(encoding='utf-8'), encoding='utf-8')
    (root / 'migrations' / '002.sql').write_text(
        'BEGIN IMMEDIATE; CREATE TABLE should_rollback(id); INVALID SQL; PRAGMA user_version=2; COMMIT;',
        encoding='utf-8')
    monkeypatch.setattr(db_module, 'ROOT', root)
    db_path = tmp_path / 'failed.sqlite'
    with pytest.raises(Exception):
        Store(db_path)
    check = sqlite3.connect(db_path)
    try:
        assert check.execute('PRAGMA user_version').fetchone()[0] == 1
        assert check.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='should_rollback'").fetchone() is None
    finally:
        check.close()

