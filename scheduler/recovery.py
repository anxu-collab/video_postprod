import datetime,json
from scheduler.db import utc
def acquire_lease(store,resource,owner,ttl=60):
    now=datetime.datetime.now(datetime.timezone.utc);expiry=(now+datetime.timedelta(seconds=ttl)).isoformat()
    with store.transaction() as c:
        r=c.execute('SELECT * FROM leases WHERE resource_id=?',(resource,)).fetchone()
        if r and r['owner']!=owner and r['expires_utc']>now.isoformat():raise RuntimeError('RESOURCE_LEASE_BUSY')
        token=(r['token']+1) if r else 1
        c.execute('INSERT OR REPLACE INTO leases VALUES(?,?,?,?,?)',(resource,owner,token,expiry,now.isoformat()))
        return token
def require_lease(store,resource,owner,token):
    with store.lock:r=store.connection.execute('SELECT * FROM leases WHERE resource_id=?',(resource,)).fetchone()
    if not r or r['owner']!=owner or r['token']!=token or r['expires_utc']<utc():raise RuntimeError('STALE_LEASE_PUBLICATION_REJECTED')
def heartbeat(store,resource,owner,token,ttl=60):
    with store.transaction() as c:
        require_lease(store,resource,owner,token)
        expiry=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=ttl)).isoformat()
        c.execute('UPDATE leases SET expires_utc=?,heartbeat=? WHERE resource_id=? AND token=?',(expiry,utc(),resource,token))
def recover(store):
    # No network submissions or deletion during recovery; journals are reconciled
    # by worker_client on explicit runnable local dependencies.
    with store.transaction() as c:
        changed=c.execute("UPDATE paid_intents SET state='SUBMISSION_UNKNOWN',reason='PROCESS_RESTART_BEFORE_RECEIPT' WHERE state='RUNNING'").rowcount
        c.execute("UPDATE jobs SET state='READY' WHERE state IN ('RUNNING','VALIDATING')")
        c.execute("UPDATE attempts SET state='RETRY_WAIT',end_utc=?,error_code='RESTART_RECONCILE' WHERE state='RUNNING'",(utc(),))
    return {'paid_unknown':changed,'automatic_paid_resubmissions':0,'local_nodes':'reconcile saved manifests and worker journals before continuation'}
