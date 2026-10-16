"""Append-only SQLite audit repository for the Clef selector."""
import datetime
import hashlib
import json
import re
import uuid

from scheduler.db import encode, utc

_SECRET = re.compile(r"(?i)(authorization\s*:\s*bearer\s+|bearer\s+|(?:api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token|token|password|secret|credential|authorization)\s*[:=]\s*)[^\s,;]+")
_SECRET_KEYS = {'api_key','api-key','access_token','access-token','auth_token','auth-token','refresh_token','refresh-token','authorization','password','secret','token','client_secret','client-secret','credential','credentials'}


def _sensitive_key(value):
    return str(value).strip().lower() in _SECRET_KEYS


def _redact(value):
    if isinstance(value, dict):
        return {str(k): ('[REDACTED]' if _sensitive_key(k) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET.sub('[REDACTED]', value)
    return value


def _hash(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


class ClefRepository:
    def __init__(self, store):
        self.store = store
        version = self.store.connection.execute('PRAGMA user_version').fetchone()[0]
        if version != 2:
            raise RuntimeError('DATABASE_VERSION_UNSUPPORTED')

    def record_snapshot(self, job_id, revision, snapshot):
        payload = _redact(snapshot)
        sid = str(uuid.uuid4())
        with self.store.transaction() as c:
            c.execute('INSERT INTO clef_snapshots VALUES(?,?,?,?,?,?)',
                      (sid, job_id, revision, utc(), _hash(payload), encode(payload)))
        return sid

    def record_decision(self, job_id, revision, snapshot, audit, final_decision):
        sid = str(uuid.uuid4())
        safe_snapshot, safe_audit, safe_final = _redact(snapshot), _redact(audit), _redact(final_decision)
        with self.store.transaction() as c:
            c.execute('INSERT INTO clef_decisions VALUES(?,?,?,?,?,?,?,?)',
                      (sid, job_id, revision, utc(), _hash(safe_snapshot), encode(safe_snapshot), encode(safe_audit), encode(safe_final)))
        return sid

    def record_usage(self, job_id, revision, decision_id, timeline):
        evidence = timeline.get('render_evidence') if isinstance(timeline, dict) else None
        if not isinstance(evidence, dict) or evidence.get('status') != 'SUCCEEDED' or evidence.get('technical_status') != 'PASS':
            raise ValueError('CLEF_USAGE_REQUIRES_SUCCESSFUL_RENDER_RECORD')
        rendered_hash = evidence.get('output_sha256')
        if not isinstance(rendered_hash, str) or not rendered_hash:
            raise ValueError('CLEF_USAGE_REQUIRES_RENDERED_HASH')
        safe = _redact(timeline)
        with self.store.transaction() as c:
            if not c.execute('SELECT 1 FROM clef_decisions WHERE decision_id=? AND job_id=? AND revision=?',
                             (decision_id, job_id, revision)).fetchone():
                raise ValueError('CLEF_USAGE_DECISION_NOT_FOUND')
            # The immutable output is the idempotency boundary, even if a retry
            # created another append-only selector decision before recording it.
            if not c.execute('SELECT 1 FROM clef_usage WHERE job_id=? AND revision=? AND rendered_hash=?',
                             (job_id, revision, rendered_hash)).fetchone():
                c.execute('INSERT INTO clef_usage VALUES(?,?,?,?,?,?,?)',
                          (str(uuid.uuid4()), job_id, revision, decision_id, rendered_hash, utc(), encode(safe)))

    def record_adjustment(self, job_id, revision, payload):
        aid = str(uuid.uuid4())
        with self.store.transaction() as c:
            c.execute('INSERT INTO clef_adjustments VALUES(?,?,?,?,?)',
                      (aid, job_id, revision, utc(), encode(_redact(payload))))
        return aid

    def cache_get(self, key):
        with self.store.lock:
            row = self.store.connection.execute('SELECT value FROM clef_cache WHERE cache_key=?', (key,)).fetchone()
        return json.loads(row['value']) if row else None

    def cache_put(self, key, value):
        if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key):
            raise ValueError('CLEF_CACHE_KEY_MUST_BE_SHA256')
        if not isinstance(value, dict):
            raise ValueError('CLEF_CACHE_VALUE_MUST_BE_OBJECT')
        with self.store.transaction() as c:
            c.execute('INSERT OR IGNORE INTO clef_cache VALUES(?,?,?)', (key, utc(), encode(_redact(value))))

    @staticmethod
    def cache_key(source_sha256, rule_version, model_version=None):
        return _hash({'source_sha256': source_sha256, 'rule_version': rule_version, 'model_version': model_version})

    def view(self, job_id, revision):
        with self.store.lock:
            snapshots = self.store.connection.execute('SELECT * FROM clef_snapshots WHERE job_id=? AND revision=? ORDER BY created_utc,snapshot_id', (job_id, revision)).fetchall()
            decisions = self.store.connection.execute('SELECT * FROM clef_decisions WHERE job_id=? AND revision=? ORDER BY created_utc,decision_id', (job_id, revision)).fetchall()
            usage = self.store.connection.execute('SELECT * FROM clef_usage WHERE job_id=? AND revision=? ORDER BY created_utc,usage_id', (job_id, revision)).fetchall()
            adjustments = self.store.connection.execute('SELECT * FROM clef_adjustments WHERE job_id=? AND revision=? ORDER BY created_utc,adjustment_id', (job_id, revision)).fetchall()
        snapshot_items = [self._decode_row(r, ('payload',)) for r in snapshots]
        return {'snapshots': snapshot_items,
                'candidates': (snapshot_items[-1]['payload'].get('candidates', []) if snapshot_items else
                           (self._decode_row(decisions[-1], ('snapshot',))['snapshot'].get('candidates', []) if decisions else [])),
                'decisions': [self._decode_row(r, ('snapshot', 'audit', 'final_decision')) for r in decisions],
                'usage': [self._decode_row(r, ('timeline',)) for r in usage],
                'adjustments': [self._decode_row(r, ('payload',)) for r in adjustments]}

    @staticmethod
    def _decode_row(row, json_fields):
        item = dict(row)
        for field in json_fields:
            item[field] = json.loads(item[field])
        return item

    def asset_history(self, asset_id):
        """Query a global source hash across jobs or an exact job-local alias."""
        requested = str(asset_id)
        requested_sha = requested[6:] if requested.startswith('asset-') else requested
        global_query = bool(re.fullmatch(r'[0-9a-f]{64}', requested_sha))
        if not global_query: requested_sha = None
        with self.store.lock:
            snaps = self.store.connection.execute('SELECT job_id,revision,payload FROM clef_snapshots').fetchall()
            decisions = self.store.connection.execute('SELECT * FROM clef_decisions ORDER BY created_utc,decision_id').fetchall()
        contexts, alias_hashes = set(), set()
        decision_items = []
        candidate_ids = set()
        for row in decisions:
            item = self._decode_row(row, ('snapshot', 'audit', 'final_decision'))
            candidates = item['snapshot'].get('candidates', [])
            matches = [c for c in candidates if
                       (c.get('source_sha256') == requested_sha if global_query else
                        c.get('asset_id') == requested or c.get('source_asset_id') == requested)]
            if matches:
                decision_items.append(item)
                contexts.add((item['job_id'], item['revision']))
                candidate_ids.update(c.get('candidate_id') for c in matches if c.get('candidate_id'))
                alias_hashes.update(c.get('source_sha256') for c in matches if c.get('source_sha256'))
        for row in snaps:
            candidates = json.loads(row['payload']).get('candidates', [])
            matches = [c for c in candidates if
                       (c.get('source_sha256') == requested_sha if global_query else
                        c.get('asset_id') == requested or c.get('source_asset_id') == requested)]
            if matches:
                contexts.add((row['job_id'], row['revision']))
                candidate_ids.update(c.get('candidate_id') for c in matches if c.get('candidate_id'))
                alias_hashes.update(c.get('source_sha256') for c in matches if c.get('source_sha256'))
        if not global_query:
            if len(alias_hashes) > 1: raise ValueError('ASSET_ID_AMBIGUOUS_USE_GLOBAL_ASSET_ID')
            requested_sha = next(iter(alias_hashes), None)
        if not requested_sha:
            return {'usage': [], 'decisions': [], 'adjustments': [], 'cumulative_count': 0}

        with self.store.lock:
            usage = self.store.connection.execute('SELECT * FROM clef_usage ORDER BY created_utc').fetchall()
            adjustments = self.store.connection.execute('SELECT * FROM clef_adjustments ORDER BY created_utc').fetchall()

        usage_items, cumulative_count = [], 0
        for row in usage:
            item = self._decode_row(row, ('timeline',))
            if (item['job_id'], item['revision']) not in contexts: continue
            timeline = item['timeline']
            resources = {r.get('ref_id'): r for r in timeline.get('resources', []) if isinstance(r, dict)}
            related = next((d for d in decision_items if d.get('decision_id') == item.get('decision_id')), None)
            final_clips = (related or {}).get('final_decision', {}).get('clips', [])
            clips = []
            for clip in (timeline.get('video_clips') or timeline.get('clips') or []):
                refs = [clip.get('base_source_ref'), clip.get('video_ref'), clip.get('source_ref')]
                refs = [r for r in refs if r]
                matched = clip.get('candidate_id') in candidate_ids
                matched = matched or clip.get('asset_id') == requested or clip.get('source_asset_id') == requested
                matched = matched or any(ref in resources and resources[ref].get('sha256') == requested_sha for ref in refs)
                matched = matched or clip.get('source_sha256') == requested_sha
                if matched:
                    source_start=clip.get('source_in_tick');source_end=clip.get('source_out_tick')
                    timeline_start=clip.get('timeline_start_tick');duration=clip.get('duration_tick')
                    final_match=next((f for f in final_clips if f.get('source_sha256')==requested_sha
                                      and f.get('source_in_tick')==source_start and f.get('source_out_tick')==source_end
                                      and f.get('timeline_start_tick')==timeline_start),None)
                    clips.append({'clip_id': clip.get('clip_id'), 'candidate_id': clip.get('candidate_id') or (final_match or {}).get('candidate_id'),
                                  'source_ref': next((r for r in refs if r in resources), None),
                                  'source_sha256': requested_sha,
                                  'source_in_tick': source_start,
                                  'source_out_tick': source_end,
                                  'timeline_start_tick': timeline_start,
                                  'timeline_end_tick': timeline_start + duration if isinstance(timeline_start,int) and isinstance(duration,int) else None,
                                  'duration_tick': duration,
                                  'sentence_ids': (final_match or {}).get('sentence_ids',[])})
            if clips:
                cumulative_count += len(clips)
                item['clips'] = clips
                item['cumulative_count'] = cumulative_count
                usage_items.append(item)

        adjustment_items = []
        for row in adjustments:
            item = self._decode_row(row, ('payload',))
            if (item['job_id'], item['revision']) not in contexts: continue
            if any((n.get('source_sha256') == requested_sha or n.get('global_asset_id') == 'asset-' + requested_sha
                    or (not global_query and (n.get('asset_id') == requested or n.get('source_asset_id') == requested)))
                   for n in _walk(item['payload'])):
                adjustment_items.append(item)
        return {'usage': usage_items, 'decisions': decision_items, 'adjustments': adjustment_items,
                'cumulative_count': cumulative_count, 'global_asset_id': 'asset-' + requested_sha}

    def query_decisions(self, job_id=None, asset_id=None, sentence_id=None, since=None, until=None):
        clauses, args = [], []
        if job_id is not None:
            clauses.append('job_id=?'); args.append(job_id)
        if since is not None:
            clauses.append('created_utc>=?'); args.append(since)
        if until is not None:
            clauses.append('created_utc<=?'); args.append(until)
        sql = 'SELECT * FROM clef_decisions' + (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY created_utc,decision_id'
        with self.store.lock:
            rows = self.store.connection.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = self._decode_row(row, ('snapshot', 'audit', 'final_decision'))
            candidates = item['snapshot'].get('candidates', [])
            requested_sha = asset_id[6:] if asset_id and asset_id.startswith('asset-') else asset_id
            if asset_id and not any(
                c.get('source_sha256') == requested_sha if re.fullmatch(r'[0-9a-f]{64}', requested_sha or '') else
                c.get('asset_id') == asset_id or c.get('source_asset_id') == asset_id
                for c in candidates):
                continue
            if sentence_id is not None and not any(n.get('sentence_id') == sentence_id for n in _walk(item)):
                continue
            out.append(item)
        return out

