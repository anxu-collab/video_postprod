"""Independent immutable script inputs and results; never a video queue."""
import contextlib
import copy
import hashlib
import json
import re
import sqlite3
import threading
from pathlib import Path
from scheduler.contracts import canonical_hash, validate_named
from scheduler.db import encode, utc
from scheduler import account_profile
from scheduler.script_skill import pin_skill, load_skill
from providers.ark_text_agent import load_config
from scheduler.expression_catalog import load_catalog
from scheduler.controlled_assembly import controlled, assemble, check_polish, output_from_text


def text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def safe_id(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', value)
            or value.upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(i) for i in range(10)), *('LPT'+str(i) for i in range(10))}):
        raise ValueError('DRAFT_ID_INVALID')
    return value


class DraftStore:
    def __init__(self, path, snapshots):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(snapshots); self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.execute('CREATE TABLE IF NOT EXISTS drafts (id TEXT PRIMARY KEY, idem TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL, input TEXT NOT NULL, status TEXT NOT NULL, result TEXT, created TEXT NOT NULL)')

    def close(self):
        self.connection.close()

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self.connection.execute('BEGIN IMMEDIATE')
            try:
                yield self.connection
                self.connection.execute('COMMIT')
            except BaseException:
                self.connection.execute('ROLLBACK')
                raise

    def folder(self, id):
        path = self.root / safe_id(id)
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('DRAFT_PATH_INVALID')
        return path

    def snapshot(self, id, name, data):
        folder = self.folder(id); folder.mkdir(exist_ok=True)
        path = folder / name
        content = encode(data)
        # Exclusive writes: old requests, raw responses and confirmations never change.
        try:
            with path.open('x', encoding='utf-8') as f:
                f.write(content)
        except FileExistsError:
            if path.read_text(encoding='utf-8') != content:
                raise ValueError('SNAPSHOT_CONFLICT')
        return path

    def get(self, id):
        safe_id(id)
        with self.lock:
            row = self.connection.execute('SELECT * FROM drafts WHERE id=?', (id,)).fetchone()
        if row is None:
            raise ValueError('DRAFT_NOT_FOUND')
        result = json.loads(row['result']) if row['result'] else {}
        return dict(result, draft_id=id, status=row['status'], input=json.loads(row['input']), created_utc=row['created'])

    def list(self):
        with self.lock:
            ids = [r[0] for r in self.connection.execute('SELECT id FROM drafts ORDER BY created DESC LIMIT 100')]
        return [self.get(id) for id in ids]

    def create(self, payload, key):
        validate_named(payload, 'script-draft-v1')
        id = safe_id(payload['draft_id'])
        if not isinstance(key, str) or not key.strip() or len(key)>200:
            raise ValueError('IDEMPOTENCY_KEY_REQUIRED')
        request_hash = canonical_hash(payload)
        with self.transaction() as c:
            row = c.execute('SELECT id,request_hash FROM drafts WHERE idem=?', (key,)).fetchone()
            if row:
                if row['request_hash'] != request_hash:
                    raise ValueError('409:IDEMPOTENCY_CONFLICT')
                return self.get(row['id'])
            if c.execute('SELECT 1 FROM drafts WHERE id=?', (id,)).fetchone():
                raise ValueError('409:IMMUTABLE_DRAFT_CONFLICT')
            p = copy.deepcopy(payload)
            if any(not p['product'][k].strip() for k in ('product_id','name')):
                raise ValueError('PRODUCT_IDENTITY_EMPTY')
            if not p['brief'].strip():raise ValueError('DRAFT_BRIEF_EMPTY')
            catalog=load_catalog()
            known={e['id'] for e in catalog['entries']}
            if any(id not in known for id in p.get('entry_ids',[])):
                raise ValueError('EXPRESSION_ENTRY_UNKNOWN')
            docs = p['product']['documents']
            if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', d['document_id']) or d['document_id']=='NONE' for d in docs):
                raise ValueError('DOCUMENT_ID_INVALID')
            if len({d['document_id'] for d in docs}) != len(docs):
                raise ValueError('DUPLICATE_DOCUMENT_ID')
            if any(not d['text'].strip() for d in docs):
                raise ValueError('PRODUCT_DOCUMENT_EMPTY')
            if p.get('parent_id'):
                parent = self.get(safe_id(p['parent_id']))
                if parent['input']['account'] != p['account']:
                    raise ValueError('DRAFT_PARENT_ACCOUNT_MISMATCH')
            if controlled(p['skill']):
                p['assembly_snapshot'] = assemble(p)
                if p.get('parent_id') and p['assembly_snapshot'].get('repair_policy') and 'script_text' not in p:
                    if (parent['input']['product'] != p['product'] or not parent.get('semantic_review')
                            or not parent.get('raw_polish') or not parent.get('checks',{}).get('passed')):
                        raise ValueError('REPAIR_PARENT_SOURCE_OR_REVIEW_MISMATCH')
                    context={'draft_id':parent['draft_id'],'script_sha256':parent['script_sha256'],
                             'previous':parent['raw_polish'],'review_feedback':parent['semantic_review']}
                    context['sha256']=canonical_hash(context)
                    p['repair_context']=context
                if 'script_text' in p:
                    candidate = output_from_text(p['assembly_snapshot'],p['script_text'])
                    if not check_polish(p['assembly_snapshot'],candidate)['passed']:
                        raise ValueError('MANUAL_CONTROLLED_TEXT_OUTSIDE_ALLOWED_SCOPE')
            elif p.get('assembly_recipe'):
                raise ValueError('ASSEMBLY_SKILL_MISMATCH')
            elif 'script_text' in p and len([s for s in p['script_text'].splitlines() if s.strip()]) != 5:
                raise ValueError('MANUAL_SCRIPT_REQUIRES_FIVE_LINES')
            caps = load_config()['global_authorization']
            cost = p['cost']
            if cost['job_limit_micros']>2000000 or cost['daily_limit_micros']>2000000 or cost['max_paid_requests']>caps['requests']:
                raise ValueError('DRAFT_COST_EXCEEDS_AUTHORIZATION')
            profile, sha = account_profile.current(p['account'])
            p.update(schema_version='script-draft/v1', revision=1, skill=pin_skill(p['skill']))
            p['skill_snapshot'] = load_skill(p['skill'])
            p['account_snapshot'] = {'account':p['account'], 'display_name':profile['display_name'], 'persona':profile['persona'], 'profile_sha256':sha,
                                     'script_rules':copy.deepcopy(profile['prompt_rules']['script'])}
            p['catalog_sha256']=catalog['catalog_sha256']
            self.snapshot(id,'catalog.json',catalog)
            self.snapshot(id, 'input.json', p)
            c.execute('INSERT INTO drafts VALUES(?,?,?,?,?,?,?)', (id,key,request_hash,encode(p),'created',None,utc()))
        return self.get(id)

    def get_job(self, job_id, revision=1):
        # Minimal Gateway routing view. No jobs/nodes are inserted in either video queue.
        p = self.get(job_id)['input']
        return {'payload':{'providers':{'text':'ark-text-agent'},'cost':p['cost']}}

    def claim(self, id):
        with self.transaction() as c:
            item=self.get(id)
            if item['status'] in ('generating','selecting','writing','reviewing','polishing'):
                raise ValueError('409:DRAFT_RUNNING')
            if item['status']!='created':
                return False
            c.execute("UPDATE drafts SET status='generating' WHERE id=?",(id,))
            return True

    def stage(self, id, status):
        with self.transaction() as c:
            c.execute('UPDATE drafts SET status=? WHERE id=?',(status,id))

    def finish(self, id, result):
        with self.transaction() as c:
            self.snapshot(id, 'result.json', result)
            c.execute('UPDATE drafts SET status=?,result=? WHERE id=?',(result['status'],encode(result),id))
        return self.get(id)

    def confirm(self, id, sha, operator):
        if not isinstance(operator,str) or not operator.strip():
            raise ValueError('OPERATOR_ID_REQUIRED')
        with self.transaction() as c:
            item=self.get(id)
            if item['status'] not in ('ready_for_user_review','confirmed') or item.get('script_sha256')!=sha:
                raise ValueError('409:DRAFT_CONFIRMATION_MISMATCH')
            if item['status']=='confirmed':return item
            self.snapshot(id,'confirmation.json',{'script_sha256':sha,'operator_id':operator,'utc':utc()})
            c.execute("UPDATE drafts SET status='confirmed' WHERE id=?",(id,))
        return self.get(id)

    def handoff(self, id):
        item=self.get(id)
        if item['status']!='confirmed':raise ValueError('409:DRAFT_NOT_CONFIRMED')
        source={'draft_id':id,'script_sha256':item['script_sha256']}
        if controlled(item['input']['skill']):
            source.update(assembly_sha256=item['input']['assembly_snapshot']['sha256'],unit_count=len(item['sentences']))
        return {'account':item['input']['account'], 'brief':item['input']['brief'], 'product':item['input']['product'],
                'script_input':{'mode':'pasted','script_text':item['script_text'],'script_sha256':item['script_sha256'],
                'generation_note':'已确认的独立口播稿','draft_source':source}}

    def validate_handoff(self, job):
        si=job.get('script_input') or {}; source=si.get('draft_source')
        if source is None:return
        expected=self.handoff(source.get('draft_id'))
        if (job['account']!=expected['account'] or si.get('mode')!='pasted'
                or source!=expected['script_input']['draft_source']
                or si.get('script_text')!=expected['script_input']['script_text']
                or si.get('script_sha256')!=expected['script_input']['script_sha256']):
            raise ValueError('DRAFT_SOURCE_MISMATCH')
