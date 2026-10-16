import contextlib,datetime,hashlib,json,os,sqlite3,threading,uuid
from pathlib import Path
from scheduler.contracts import canonical_hash
ROOT=Path(__file__).resolve().parents[1]
def utc():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def encode(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

class Store:
    def __init__(self,path):
        self.path=Path(path).resolve();self.path.parent.mkdir(parents=True,exist_ok=True);self.lock=threading.RLock()
        self.connection=sqlite3.connect(self.path,timeout=5,check_same_thread=False,isolation_level=None)
        self.connection.row_factory=sqlite3.Row
        self.connection.execute('PRAGMA journal_mode=WAL');self.connection.execute('PRAGMA synchronous=FULL')
        self.connection.execute('PRAGMA foreign_keys=ON');self.connection.execute('PRAGMA busy_timeout=5000')
        version=self.connection.execute('PRAGMA user_version').fetchone()[0]
        if version>2:raise RuntimeError('DATABASE_VERSION_UNSUPPORTED')
        if version==0:
            self.connection.executescript((ROOT/'migrations/001.sql').read_text(encoding='utf-8'))
            version=1
        if version==1:
            with self.lock:
                try:
                    self.connection.executescript((ROOT/'migrations/002.sql').read_text(encoding='utf-8'))
                except BaseException:
                    if self.connection.in_transaction:self.connection.execute('ROLLBACK')
                    raise
    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self.connection.execute('BEGIN IMMEDIATE')
            try:yield self.connection;self.connection.execute('COMMIT')
            except BaseException:self.connection.execute('ROLLBACK');raise
    def get_job(self,job_id,revision=1):
        with self.lock:r=self.connection.execute('SELECT * FROM jobs WHERE job_id=? AND revision=?',(job_id,revision)).fetchone()
        if not r:return None
        d=dict(r);d['payload']=json.loads(d['payload']);return d
    def submit_job(self,idempotency_key,payload):
        h=canonical_hash(payload);job=payload['job_id'];rev=payload['revision']
        with self.transaction() as c:
            previous=c.execute("SELECT * FROM idempotency WHERE endpoint='jobs' AND key=?",(idempotency_key,)).fetchone()
            if previous:
                if previous['body_hash']!=h:raise ValueError('409:IDEMPOTENCY_BODY_CONFLICT')
                ref=json.loads(previous['response_ref']);return self.get_job(ref['job_id'],ref['revision'])
            if c.execute('SELECT 1 FROM jobs WHERE job_id=? AND revision=?',(job,rev)).fetchone():raise ValueError('409:IMMUTABLE_REVISION_EXISTS')
            c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(job,rev,h,payload.get('parent_revision'),'RECEIVED',payload.get('profile'),utc(),encode(payload),None))
            c.execute('INSERT INTO idempotency VALUES(?,?,?,?)',('jobs',idempotency_key,h,encode({'job_id':job,'revision':rev})))
        self.event(job,rev,'RECEIVED',{'input_hash':h});return self.get_job(job,rev)
    def set_state(self,job_id,revision,state,error=None):
        with self.transaction() as c:
            n=c.execute('UPDATE jobs SET state=?,error=? WHERE job_id=? AND revision=?',(state,encode(error) if error else None,job_id,revision)).rowcount
            if n!=1:raise ValueError('JOB_NOT_FOUND')
        self.event(job_id,revision,state,error or {})
    def event(self,job_id,revision,kind,payload):
        with self.transaction() as c:c.execute('INSERT INTO events(job_id,revision,type,utc,payload) VALUES(?,?,?,?,?)',(job_id,revision,kind,utc(),encode(payload)))
    def cache_get(self,node_key):
        with self.lock:r=self.connection.execute("SELECT result FROM nodes WHERE node_key=? AND state='SUCCEEDED' AND result IS NOT NULL LIMIT 1",(node_key,)).fetchone()
        if not r:return None
        result=json.loads(r[0])
        from workers.common import digest
        if any(not Path(ref['path']).is_file() or digest(ref['path'])!=ref['sha256'] for ref in result.get('output_refs',[])):return None
        return result
    def cache_put(self,node_key,result):
        with self.transaction() as c:c.execute('INSERT OR REPLACE INTO nodes(node_id,type,node_key,state,result) VALUES(?,?,?,?,?)',('cache:'+node_key,'cache',node_key,'SUCCEEDED',encode(result)))
    def backup(self,dest):
        dest=Path(dest).resolve();dest.parent.mkdir(parents=True,exist_ok=True)
        if dest.exists():raise ValueError('BACKUP_EXISTS')
        partial=dest.with_suffix(dest.suffix+'.partial')
        with self.lock:
            target=sqlite3.connect(partial)
            try:self.connection.backup(target)
            finally:target.close()
        os.replace(partial,dest)
