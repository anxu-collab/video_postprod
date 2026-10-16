import datetime,json,uuid
from scheduler.db import encode,utc
from scheduler.contracts import canonical_hash
class BudgetHold(RuntimeError):pass
class SubmissionUnknown(RuntimeError):pass

class Ledger:
    def __init__(self,store,global_limit_micros=2000000,max_requests=40):self.store=store;self.limit=global_limit_micros;self.requests=max_requests
    def reserve(self,request_key,job_id,amount_micros,job_limit_micros=2000000,max_paid_requests=40,daily_limit_micros=2000000):
        if not isinstance(amount_micros,int) or amount_micros<=0:raise ValueError('INVALID_RESERVE')
        day=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
        with self.store.transaction() as c:
            previous=c.execute('SELECT * FROM paid_intents WHERE request_key=?',(request_key,)).fetchone()
            if previous:
                if previous['state']=='SUCCEEDED':return {'intent_id':previous['intent_id'],'state':'SUCCEEDED','result':json.loads(previous['result'])}
                raise SubmissionUnknown('SUBMISSION_UNKNOWN:'+previous['intent_id'])
            rows=c.execute('SELECT * FROM paid_intents').fetchall()
            held=lambda values:sum(r['settled_micros'] if r['settled_micros'] is not None else r['reserve_micros'] for r in values)
            count=c.execute('SELECT COUNT(*) FROM paid_attempts').fetchone()[0]
            if count>=min(self.requests,max_paid_requests):raise BudgetHold('REQUEST_CAP')
            if held(rows)+amount_micros>self.limit:raise BudgetHold('DEPLOYMENT_COST_CAP')
            if held([r for r in rows if r['date_local']==day])+amount_micros>min(self.limit,daily_limit_micros):raise BudgetHold('DAILY_COST_CAP')
            if held([r for r in rows if r['job_id']==job_id])+amount_micros>min(self.limit,job_limit_micros):raise BudgetHold('JOB_COST_CAP')
            intent=uuid.uuid4().hex
            c.execute('INSERT INTO paid_intents(intent_id,request_key,provider,account_ref,job_id,state,reserve_micros,date_local,created_utc) VALUES(?,?,?,?,?,?,?,?,?)',(intent,request_key,'ark','approved-project-env',job_id,'RUNNING',amount_micros,day,utc()))
            c.execute('INSERT INTO paid_attempts(intent_id,seq,started_utc,acceptance_status,reserved) VALUES(?,?,?,?,?)',(intent,1,utc(),'UNKNOWN',amount_micros))
        return {'intent_id':intent,'state':'RUNNING','result':None}
    def complete(self,intent_id,result,cost_micros=None):
        if cost_micros is not None and (not isinstance(cost_micros,int) or cost_micros<0):raise ValueError('INVALID_COST')
        with self.store.transaction() as c:
            r=c.execute('SELECT * FROM paid_intents WHERE intent_id=?',(intent_id,)).fetchone()
            if not r or r['state']!='RUNNING':raise SubmissionUnknown('INTENT_NOT_RUNNING')
            if cost_micros is not None and cost_micros>r['reserve_micros']:raise BudgetHold('PROVIDER_EXCEEDED_RESERVED_BOUND')
            receipt=canonical_hash(result)
            c.execute("UPDATE paid_intents SET state='SUCCEEDED',result=?,receipt_hash=?,settled_micros=?,remote_id=? WHERE intent_id=?",(encode(result),receipt,cost_micros,result.get('id'),intent_id))
            c.execute("UPDATE paid_attempts SET acceptance_status='ACCEPTED',receipt_hash=?,settled=?,request_id=? WHERE intent_id=?",(receipt,cost_micros,result.get('id'),intent_id))
    def unknown(self,intent_id,reason):
        with self.store.transaction() as c:c.execute("UPDATE paid_intents SET state='SUBMISSION_UNKNOWN',reason=? WHERE intent_id=?",(reason,intent_id))
    def snapshot(self):
        with self.store.lock:
            rows=self.store.connection.execute('SELECT * FROM paid_intents').fetchall();count=self.store.connection.execute('SELECT COUNT(*) FROM paid_attempts').fetchone()[0]
        return {'requests':count,'request_cap':self.requests,'limit_micros':self.limit,'held_micros':sum(r['settled_micros'] if r['settled_micros'] is not None else r['reserve_micros'] for r in rows),'unknown':sum(r['state']=='SUBMISSION_UNKNOWN' for r in rows),'settlement_note':'Amounts without provider billing receipt remain reserved upper bounds; not claimed as billed.'}
