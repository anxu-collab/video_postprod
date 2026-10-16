import asyncio,contextlib,copy,hmac,json,secrets,threading,time,uuid
from pathlib import Path
from fastapi import FastAPI,Header,HTTPException,Request,Response
from fastapi.responses import FileResponse,HTMLResponse,StreamingResponse
from fastapi.staticfiles import StaticFiles
from scheduler import account_profile
from scheduler.contracts import validate_job,canonical_hash
from scheduler.dag import revision_diff
from scheduler.db import encode,utc
from scheduler.pipeline import Pipeline,import_file
from scheduler.recovery import recover
from workers.common import ROOT,atomic_json,digest

# text=claude-cli: script generation on Opus 5.5, fact checks and Clef text review on Sonnet 5.5.
# text=ark-text-agent: script-* on the script tier, *-review on the review tier
# (config/ark-text-agent.json). New v2 tasks default to the stable Ark route; old
# tasks and their revisions keep replaying the route they were created under.
# tts is the account's own voice route from its profile.
TEXT_ROUTES={'ark-approved','claude-cli','ark-text-agent'}
FIXED_ROUTES={'vision':'ark-approved','asr':'mpt-cuda-small','lipsync':None}
SUBTITLE_TEMPLATE={'template_id':'subtitle-bottom','version':'1.0.0'}

def validate_routes(p):
    profile=account_profile.for_job(p);pr=p['providers']
    if pr['text'] not in TEXT_ROUTES or {k:pr[k] for k in FIXED_ROUTES}!=FIXED_ROUTES or pr['tts']!=profile['voice']['tts_route']:raise ValueError('UNAPPROVED_PROVIDER_ROUTE')
    if p['profile'] not in profile['profiles']:raise ValueError('ACCOUNT_PROFILE_LINE_UNKNOWN:'+p['profile'])
    music=profile['music']['template'] if profile['music'] else None
    if p['templates']['subtitle']!=SUBTITLE_TEMPLATE or p['templates']['bgm'] not in [None,music]:raise ValueError('TEMPLATE_VERSION_UNAVAILABLE')
    ni=p.get('narration_input') or {}
    if ni.get('mode')=='auto' and ni.get('preset') not in profile['voice']['presets']:raise ValueError('VOICE_PRESET_NOT_IN_ACCOUNT_PROFILE')

def pin_account_profile(p):
    """A new v2 job pins the account profile it was created under."""
    if p.get('schema_version')!='edit-job/v2' or p.get('revision')!=1:return p
    wanted=p.get('account_profile_sha256')
    if not wanted or wanted==account_profile.current(p['account'])[1]:
        p['account_profile_sha256']=account_profile.pin(p['account'])
    return p

def keep_account_profile(old,new):
    """A revision stays on its parent's account and pinned profile; omitting the pin inherits it."""
    if old.get('account_profile_sha256') and not new.get('account_profile_sha256'):
        new['account_profile_sha256']=old['account_profile_sha256']
    if (old.get('account'),old.get('account_profile_sha256'))!=(new.get('account'),new.get('account_profile_sha256')):
        raise ValueError('ACCOUNT_PROFILE_CHANGE_REQUIRES_NEW_JOB')
    return new

def accepted_quality(records,artifact,has_locked):
    required={'visual','color','audio','subtitles','content'}|({'lipsync','boundaries'} if has_locked else set())
    valid={r['scope'] for r in records if r['artifact_hash']==artifact and r['decision']=='accept'}
    return required<=valid

class Queue:
    def __init__(self,store):self.store=store;self.stop=threading.Event();self.thread=threading.Thread(target=self.loop,daemon=True,name='single-job-queue')
    def loop(self):
        while not self.stop.wait(1):
            with self.store.lock:row=self.store.connection.execute("SELECT job_id,revision FROM jobs WHERE state IN ('READY','RETRY_WAIT') ORDER BY created_utc,revision LIMIT 1").fetchone()
            if not row:continue
            job=self.store.get_job(row['job_id'],row['revision']);old_error=json.loads(job['error']) if job['error'] else {}
            if old_error.get('next_after',0)>time.time():continue
            try:Pipeline(self.store,job['payload']).run()
            except Exception as e:
                if self.store.get_job(job['job_id'],job['revision'])['state']=='CANCELLED':continue
                message=str(e)[:900];code=message.split(':')[0][:100];attempt=old_error.get('retries',0)
                with self.store.lock:unknown=self.store.connection.execute("SELECT COUNT(*) FROM paid_intents WHERE job_id=? AND state='SUBMISSION_UNKNOWN'",(job['job_id'],)).fetchone()[0]
                if unknown:state='SUBMISSION_UNKNOWN'
                elif any(x in message for x in ['SCRIPT_CONFIRM_REQUIRED','NARRATION_CONFIRM_REQUIRED','SUBTITLE_REVIEW_REQUIRED','SHOT_COVERAGE_MISSING','SPEECH_ERROR_REDUB_REQUIRED','CONFIRMED_WINDOW_','HUMAN_SCORE_REQUIRED']):
                    head=message.split(':')[0]
                    state={'SCRIPT_CONFIRM_REQUIRED':'AWAIT_SCRIPT_CONFIRM','NARRATION_CONFIRM_REQUIRED':'AWAIT_NARRATION_CONFIRM','SUBTITLE_REVIEW_REQUIRED':'AWAIT_SUBTITLE_REVIEW','SHOT_COVERAGE_MISSING':'AWAIT_SHOT_GAP','HUMAN_SCORE_REQUIRED':'AWAIT_HUMAN_SCORE','SPEECH_ERROR_REDUB_REQUIRED':'NEEDS_REDUB'}.get(head,'AWAIT_SHOT_GAP' if head.startswith('CONFIRMED_WINDOW_') else 'FAILED_FINAL')
                elif any(x in message for x in ['CUDA out of memory','GPU_OOM','RESOURCE_LEASE_BUSY']):state='WAITING_RESOURCE'
                elif any(x in message for x in ['REVIEW_REQUIRED','COLOR_QUALIFICATION_REQUIRED']):state='REVIEW_REQUIRED'
                elif ('RENDER_FAILED' in message or isinstance(e,(TimeoutError,ConnectionError))) and attempt<2:state='RETRY_WAIT'
                else:state='FAILED_FINAL'
                self.store.set_state(job['job_id'],job['revision'],state,{'code':code,'message':message,'retries':attempt+1,'next_after':time.time()+[5,30,60][min(attempt,2)]})

def create_app(store,gateway,operator_token,run_queue=True,operator_origin=None):
    # Per-instance EXACT local origin the browser may come from. Default keeps
    # the production boundary byte-identical; the only other permitted value is
    # the isolated drill origin (http://127.0.0.1:8763) supplied explicitly by
    # trusted startup code — never derived from Host/Origin/task payloads, no
    # wildcards. Token/cookie/hmac and every operator/provider gate unchanged.
    allowed_origins=('http://127.0.0.1:8760','http://127.0.0.1:8763')
    operator_origin=operator_origin or allowed_origins[0]
    if operator_origin not in allowed_origins:raise ValueError('OPERATOR_ORIGIN_NOT_ALLOWED')
    queue=Queue(store)
    @contextlib.asynccontextmanager
    async def lifespan(app):
        if run_queue:recover(store);queue.thread.start()
        yield
        queue.stop.set()
        app.state.script_drafts.close()
    app=FastAPI(title='本地自动后期',lifespan=lifespan);app.state.queue=queue
    def operator(req):
        origin=req.headers.get('origin')
        if origin and origin!=operator_origin:raise HTTPException(403,'LOCAL_ORIGIN_REQUIRED')
        value=req.headers.get('authorization','').removeprefix('Bearer ') or req.cookies.get('postprod_operator','')
        if not hmac.compare_digest(value,operator_token):raise HTTPException(401,'OPERATOR_SESSION_REQUIRED')
    from scheduler.script_draft_api import install as install_script_drafts
    install_script_drafts(app,store,gateway,operator)
    def get_job(id,rev):
        job=store.get_job(id,rev)
        if not job:raise HTTPException(404,'JOB_NOT_FOUND')
        return job
    def result(id,rev):
        job=get_job(id,rev);p=ROOT/'jobs'/id/('rev-'+format(rev,'04d'))/'result.json'
        return json.loads(p.read_text(encoding='utf-8')) if p.is_file() else None
    @app.get('/',response_class=HTMLResponse)
    def home():return (ROOT/'ui/index.html').read_text(encoding='utf-8')
    app.mount('/ui',StaticFiles(directory=ROOT/'ui'),name='ui')
    @app.post('/v1/session')
    def session(req:Request,response:Response):
        operator(req);response.set_cookie('postprod_operator',operator_token,httponly=True,samesite='strict',max_age=43200);return {'status':'authenticated'}
    @app.get('/v1/jobs')
    def list_jobs(req:Request):
        operator(req)
        with store.lock:rows=store.connection.execute('SELECT job_id,revision,state,created_utc,error FROM jobs ORDER BY created_utc DESC,revision DESC LIMIT 100').fetchall()
        return [dict(r) for r in rows]
    @app.post('/v1/jobs',status_code=202)
    def submit(p:dict,req:Request,idempotency_key:str|None=Header(default=None)):
        operator(req)
        if not idempotency_key:raise HTTPException(400,'IDEMPOTENCY_KEY_REQUIRED')
        try:
            validate_job(p);bind_script_skill(p);pin_account_profile(p);validate_routes(p)
            if p['revision']!=1:raise ValueError('USE_REVISION_ENDPOINT')
            if not __import__('re').fullmatch('[A-Za-z0-9_.-]{1,128}',p['job_id']):raise ValueError('JOB_ID_WINDOWS_PATH_UNSUPPORTED')
            job=store.submit_job(idempotency_key,p)
            if job['state']=='RECEIVED':store.set_state(p['job_id'],1,'READY')
            return {'job_id':p['job_id'],'revision':1,'state':store.get_job(p['job_id'])['state'],'account_profile_sha256':p.get('account_profile_sha256')}
        except ValueError as e:raise HTTPException(409 if str(e).startswith('409:') else 422,str(e))
    @app.get('/v1/jobs/{id}')
    def status(id:str,req:Request,revision:int=1):
        operator(req);job=get_job(id,revision);out=result(id,revision)
        with store.lock:nodes=[dict(r) for r in store.connection.execute('SELECT node_id,type,state,result FROM nodes WHERE job_id=? AND revision=?',(id,revision))]
        tp=ROOT/'jobs'/id/('rev-'+format(revision,'04d'))/'timeline.json';timeline=json.loads(tp.read_text(encoding='utf-8')) if tp.exists() else None
        music=None
        if timeline:
            refs={r['ref_id']:r for r in timeline['resources']};event=next((a for a in timeline['audio_events'] if a['role']=='bgm'),None)
            if event:
                ref=refs[event['audio_ref']]
                if ref['provenance_ref']:license_ref=refs[ref['provenance_ref']]['path']
                else:
                    try:license_ref=(account_profile.for_job(job['payload'])['music'] or {}).get('license_ref')
                    except (ValueError,OSError):license_ref=None
                music={'path':ref['path'],'sha256':ref['sha256'],'provenance_ref':license_ref,'gain_db':event['gain_db']}
        return {'job_id':id,'revision':revision,'state':job['state'],'input_hash':job['input_hash'],'payload':job['payload'],'nodes':nodes,'result':out,'timeline':timeline,'music':music,'errors':json.loads(job['error']) if job['error'] else [],'cost':gateway.ledger.snapshot(),'quality_accepted':job['state']=='ACCEPTED'}
    @app.post('/v1/jobs/{id}/revisions',status_code=202)
    def revise(id:str,p:dict,req:Request,if_match:str|None=Header(default=None),idempotency_key:str|None=Header(default=None)):
        operator(req)
        try:
            validate_job(p);old=get_job(id,p['parent_revision'])
            if id!=p['job_id'] or if_match!=old['input_hash']:raise ValueError('409:IF_MATCH_CONFLICT')
            keep_account_profile(old['payload'],p);bind_script_skill(p,old['payload']);validate_routes(p)
            # Only v1 edit lists (subtitle/shot/speech) resolve cue/clip/event ids
            # against a base timeline; pure v2 changes like sentence redubs from a
            # NEEDS_REDUB hold legitimately have no rendered parent yet.
            needs_base=bool(p['overrides']['subtitle_edits'] or p['overrides']['shot_edits'] or p['overrides']['speech_edits'])
            if needs_base and not result(id,p['parent_revision']):
                candidates=[ROOT/'jobs'/id/('rev-'+format(n,'04d'))/'timeline.json' for n in range(p['parent_revision']-1,0,-1)]
                if not any(f.is_file() and digest(f)==p['overrides']['base_timeline_sha256'] for f in candidates):raise ValueError('409:BASE_TIMELINE_NOT_AVAILABLE')
            if not idempotency_key:raise ValueError('IDEMPOTENCY_KEY_REQUIRED')
            dirty=sorted(revision_diff(old['payload'],p));job=store.submit_job(idempotency_key,p)
            if job['state']=='RECEIVED':store.set_state(id,p['revision'],'READY')
            store.event(id,p['revision'],'REVISION_DIFF',{'invalidated':dirty});return {'job_id':id,'revision':p['revision'],'state':store.get_job(id,p['revision'])['state'],'invalidated':dirty}
        except ValueError as e:raise HTTPException(409 if str(e).startswith('409:') else 422,str(e))
    @app.post('/v1/jobs/{id}/resume')
    def resume(id:str,p:dict,req:Request):
        operator(req);job=get_job(id,int(p['revision']))
        if job['state'] not in ['WAITING_RESOURCE','RETRY_WAIT','READY']:raise HTTPException(409,'NOT_SAFE_TO_RESUME_USE_NEW_REVISION_OR_RESOLVE_HOLD')
        store.set_state(id,job['revision'],'READY');return {'state':'READY'}
    @app.post('/v1/jobs/{id}/cancel')
    def cancel(id:str,p:dict,req:Request):
        operator(req);job=get_job(id,int(p['revision']))
        if job['state']=='ACCEPTED':raise HTTPException(409,'ACCEPTED_REVISION_IMMUTABLE')
        store.set_state(id,job['revision'],'CANCELLED');return {'state':'CANCELLED','remote_cancellation_confirmed':False,'note':'停止后续本地节点；已有调用保留回执和预算，不自动退款或重提'}
    @app.get('/v1/jobs/{id}/events')
    async def events(id:str,req:Request,revision:int=1,after:int=0):
        operator(req);get_job(id,revision)
        async def stream():
            cursor=after
            while not await req.is_disconnected():
                with store.lock:rows=store.connection.execute('SELECT * FROM events WHERE job_id=? AND revision=? AND event_id>? ORDER BY event_id',(id,revision,cursor)).fetchall()
                for r in rows:cursor=r['event_id'];yield 'id: '+str(cursor)+'\ndata: '+json.dumps(dict(r),ensure_ascii=False)+'\n\n'
                if not rows:yield ': heartbeat\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(stream(),media_type='text/event-stream')
    @app.get('/v1/jobs/{id}/video')
    def video(id:str,req:Request,revision:int=1):
        operator(req);out=result(id,revision)
        if not out:raise HTTPException(404,'OUTPUT_NOT_READY')
        p=Path(out['output_path']).resolve()
        if not p.is_relative_to(ROOT/'jobs') or digest(p)!=out['artifact_sha256']:raise HTTPException(409,'OUTPUT_HASH_CHANGED')
        return FileResponse(p,media_type='video/mp4')
    @app.post('/v1/jobs/{id}/reviews')
    def review(id:str,p:dict,req:Request):
        operator(req);rev=int(p['revision']);out=result(id,rev)
        scopes=['visual','color','audio','subtitles','content','lipsync','boundaries']
        if not out or out['technical_status']!='PASS' or p['artifact_sha256']!=out['artifact_sha256']:raise HTTPException(409,'REVIEW_ARTIFACT_INVALID')
        if p.get('scope') not in scopes or p.get('decision') not in ['accept','reject'] or not p.get('notes','').strip() or not p.get('operator_id','').strip():raise HTTPException(422,'REVIEW_SCOPE_DECISION_NOTES_OPERATOR_REQUIRED')
        evidence=uuid.uuid4().hex;record=dict(p,utc=utc(),authenticated=True,validator='human-ui/v1');sha=canonical_hash(record)
        with store.transaction() as c:
            c.execute('INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)',(evidence,out['artifact_sha256'],p['scope'],p['decision'],p['operator_id'],'human-ui/v1',sha,encode(record)))
            rows=[dict(r) for r in c.execute('SELECT artifact_hash,scope,decision FROM reviews WHERE artifact_hash=? ORDER BY rowid',(out['artifact_sha256'],))]
        latest={r['scope']:r for r in rows};job=get_job(id,rev);has_locked=any(a['mode']=='spoken_locked' for a in job['payload']['assets'])
        state='ACCEPTED' if accepted_quality(latest.values(),out['artifact_sha256'],has_locked) else 'REVIEW_REQUIRED'
        from scheduler.output_catalog import publish
        catalog='审核' if state=='ACCEPTED' else ('拒绝' if p['decision']=='reject' else '待审核')
        publish(job['payload'], out['output_path'], catalog, out['artifact_sha256'])
        store.set_state(id,rev,state);return {'state':state,'evidence_id':evidence,'output_status':catalog}
    @app.post('/v1/jobs/{id}/paid-intents/{intent}/resolve')
    def resolve(id:str,intent:str,p:dict,req:Request):
        operator(req)
        if not p.get('evidence'):raise HTTPException(422,'PROVIDER_EVIDENCE_REQUIRED')
        with store.transaction() as c:
            row=c.execute('SELECT * FROM paid_intents WHERE intent_id=? AND job_id=?',(intent,id)).fetchone()
            if not row:raise HTTPException(404,'INTENT_NOT_FOUND')
            c.execute('UPDATE paid_intents SET reason=? WHERE intent_id=?',(encode({'operator_resolution':p,'utc':utc(),'automatic_resubmit':False}),intent))
        return {'state':row['state'],'hold_retained':True,'automatic_resubmit':False,'note':'已记录查询证据；本期未实现供应商账单自动核销，未知提交不释放预算、不重提'}
    @app.post('/v1/imports')
    async def upload(req:Request,filename:str,kind:str='video',license_note:str=''):
        operator(req);suffix=Path(filename).suffix.lower()
        if kind=='bgm':
            if suffix not in ['.mp3','.wav','.flac'] or not license_note.strip():raise HTTPException(422,'BGM_TYPE_AND_LICENSE_REQUIRED')
        elif kind=='narration':
            if suffix not in ['.wav','.mp3']:raise HTTPException(422,'NARRATION_FILE_TYPE_REQUIRED')
        elif kind=='document':
            if suffix not in ['.txt','.md','.json']:raise HTTPException(422,'DOCUMENT_FILE_TYPE_REQUIRED')
        elif suffix not in ['.mp4','.mov','.mkv']:raise HTTPException(422,'VIDEO_FILE_TYPE_REQUIRED')
        staging=ROOT/'jobs/import-staging';staging.mkdir(exist_ok=True);tmp=staging/(uuid.uuid4().hex+suffix);size=0
        try:
            with tmp.open('xb') as f:
                async for chunk in req.stream():
                    size+=len(chunk)
                    if size>256*1024*1024:raise HTTPException(413,'IMPORT_SIZE_CAP')
                    f.write(chunk)
            if kind=='document':
                text=tmp.read_text(encoding='utf-8-sig')  # must decode as declared text
                dest=import_file(tmp);sha=digest(dest);prov=staging/(sha+'-provenance.json')
                atomic_json(prov,{'source_sha256':sha,'origin':'user_product_document','original_filename':Path(filename).name,
                                  'license_note':license_note or 'operator-provided product material','product_identity_verified':False,'real_product_test':False})
                return {'path':str(dest),'sha256':sha,'provenance_ref':str(prov),'text_chars':len(text),'media_type':{'txt':'text/plain','md':'text/markdown','json':'application/json'}[suffix[1:]]}
            from renderer.ffmpeg import probe,decode
            q=probe(tmp);decode(tmp,staging);dest=import_file(tmp);sha=digest(dest);prov=staging/(sha+'-provenance.json')
            if kind in ('bgm','narration') and q['stream_indices']['audio'] is None:raise HTTPException(422,'AUDIO_STREAM_MISSING')
            atomic_json(prov,{'source_sha256':sha,'origin':'user_imported_bgm' if kind=='bgm' else ('user_imported_narration' if kind=='narration' else 'user_imported_h3'),'original_filename':Path(filename).name,'license_note':license_note,'product_identity_verified':False,'real_product_test':False})
            evidence=staging/(sha+'-boundary.json');atomic_json(evidence,{'validator':'full-source/v1','source_sha256':sha,'validated':True,'in_tick':0,'out_tick':((q['duration_ticks']+1919)//1920)*1920,'dialogue_completeness_verified':False,'quality':'REVIEW_REQUIRED'})
            return {'path':str(dest),'sha256':sha,'provenance_ref':str(prov),'probe':q,'boundary_ref':str(evidence)}
        finally:
            if tmp.exists():tmp.unlink()
    @app.post('/v1/color-previews')
    def color(p:dict,req:Request):
        operator(req)
        if p.get('decision')!='allow_preview_bt709' or not p.get('source_hashes'):raise HTTPException(422,'EXPLICIT_PREVIEW_AUTHORIZATION_REQUIRED')
        path=ROOT/'state/deployment/color-preview-authorization.json';old=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'source_hashes':[]}
        for sha in p['source_hashes']:
            if not __import__('re').fullmatch('[a-f0-9]{64}',sha) or not (ROOT/'artifacts/imports'/sha[:2]/(sha+'.mp4')).exists():raise HTTPException(422,'IMPORTED_MP4_HASH_REQUIRED')
        old.update(decision='allow_preview_bt709',source_hashes=sorted(set(old['source_hashes']+p['source_hashes'])),production_color_qualified=False,operator_record_utc=utc());atomic_json(path,old);return {'status':'preview_authorized','color_quality':'pending'}
    def account_for_template(account):
        accounts=sorted(account_profile.registry())
        if account is None:
            if len(accounts)!=1:raise HTTPException(422,'ACCOUNT_REQUIRED:'+','.join(accounts))
            account=accounts[0]
        try:return account_profile.current(account)[0]
        except ValueError as e:raise HTTPException(422,str(e))
    def apply_account(p,profile):
        music=profile['music']
        p['account']=profile['account'];p['profile']=profile['profiles'][0]
        p['templates']['bgm']=music['template'] if music else None
        return p
    @app.get('/v2/accounts')
    def accounts(req:Request):
        operator(req);out=[]
        for account in sorted(account_profile.registry()):
            try:out.append(account_profile.summary(account_profile.current(account)[0]))
            except (ValueError,OSError) as e:out.append({'account':account,'error':str(e)})
        return out
    @app.get('/v1/job-template')
    def template(req:Request,account:str|None=None):
        operator(req);profile=account_for_template(account)
        p=apply_account(json.loads((ROOT/'contracts/examples/edit-job-ab.example.json').read_text(encoding='utf-8')),profile)
        p['assets']=[];p['providers']=dict(FIXED_ROUTES,text='ark-approved',tts=profile['voice']['tts_route'])
        p['cost']={'paid_enabled':False,'currency':'CNY','job_limit_micros':2000000,'daily_limit_micros':2000000,'max_paid_requests':40};return p
    @app.get('/v1/capabilities')
    def capabilities(req:Request):
        operator(req);return {'versions':json.loads((ROOT/'config/versions.json').read_text(encoding='utf-8')),'phase':'A+B','avatar_redub':{'available':False,'code':'CAPABILITY_UNAVAILABLE_LIPSYNC'},'paid_budget':gateway.ledger.snapshot(),'quality_acceptance':'authenticated artifact-bound review','developer_agents_required':False}
    @app.post('/v1/backup')
    def backup(req:Request):
        operator(req);dest=ROOT/'state/backups'/(uuid.uuid4().hex+'.sqlite');store.backup(dest);return {'path':str(dest),'sha256':digest(dest)}
    # ---- versioned v2 operator endpoints; UI never exposes their JSON shapes ----
    def v2_work(id,rev):
        get_job(id,rev)
        return ROOT/'jobs'/id/('rev-'+format(rev,'04d'))
    def read_sidecar(folder,name):
        p=folder/name
        return json.loads(p.read_text(encoding='utf-8')) if p.is_file() else None
    def confirmations(folder):
        p=folder/'stage-confirmations.json'
        return json.loads(p.read_text(encoding='utf-8')) if p.is_file() else {}
    def record_confirmation(folder,kind,sha,operator_id):
        data=confirmations(folder)
        import datetime
        data[kind]={'sha256':sha,'operator_id':operator_id,'utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}
        atomic_json(folder/'stage-confirmations.json',data)
    def bind_script_skill(p,old=None):
        app.state.script_drafts.validate_handoff(p)
        if p.get('schema_version')!='edit-job/v2':
            from scheduler.script_generation import validate_legacy_generation
            validate_legacy_generation(p,old)
            return
        from scheduler.script_skill import pin_skill,validate_script_skill_requirement
        si=p['script_input']
        if si['mode']!='generated':return
        # An identical retry must retain its already chosen snapshot, even if
        # the registry's current files have changed since first submission.
        existing=store.get_job(p['job_id'],p['revision'])
        previous=existing['payload'] if existing else old
        selection=si.get('skill')
        prior=(previous or {}).get('script_input',{}).get('skill')
        if selection and not selection.get('snapshot_sha256') and prior and all(selection.get(k)==prior.get(k) for k in ('id','version')):
            si['skill']=dict(prior)
        if not si.get('skill') and old and not prior:
            from scheduler.script_generation import find_saved_script
            if find_saved_script(ROOT,p,store) is not None:return
        validate_script_skill_requirement(p)
        si['skill']=pin_skill(si['skill'])

    @app.get('/v2/script-skills')
    def script_skills(req:Request):
        operator(req)
        from scheduler.script_skill import list_skills
        try:return list_skills()
        except ValueError as e:raise HTTPException(422,str(e))

    @app.get('/v2/job-template')
    def v2_template(req:Request,account:str|None=None):
        operator(req);profile=account_for_template(account)
        p=apply_account(json.loads((ROOT/'contracts/examples/edit-job-ab.example.json').read_text(encoding='utf-8')),profile)
        p['schema_version']='edit-job/v2';p['assets']=[]
        p['clef_selector']={'mode':'shadow','backend':'local','timeout_sec':20,'max_requests':64,'max_retries':0}
        p['providers']=dict(FIXED_ROUTES,text='ark-text-agent',tts=profile['voice']['tts_route'])
        p['script_input']={'mode':'pasted','script_text':None,'script_sha256':None,'generation_note':None}
        p['narration_input']={'mode':'auto','preset':profile['voice']['default_preset'],'uploaded':None,'history':None}
        p['v2_overrides']={'clip_replacements':[],'sentence_redubs':[]}
        p['cost']={'paid_enabled':False,'currency':'CNY','job_limit_micros':2000000,'daily_limit_micros':2000000,'max_paid_requests':40}
        return p
    @app.post('/v2/jobs',status_code=202)
    def v2_submit(p:dict,req:Request,idempotency_key:str|None=Header(default=None)):
        operator(req)
        if p.get('schema_version')!='edit-job/v2':raise HTTPException(422,'V2_ENDPOINT_REQUIRES_EDIT_JOB_V2')
        if not idempotency_key:raise HTTPException(400,'IDEMPOTENCY_KEY_REQUIRED')
        try:
            validate_job(p);bind_script_skill(p);pin_account_profile(p);validate_routes(p)
            if p['revision']!=1:raise ValueError('USE_REVISION_ENDPOINT')
            job=store.submit_job(idempotency_key,p)
            if job['state']=='RECEIVED':store.set_state(p['job_id'],1,'READY')
            return {'job_id':p['job_id'],'revision':1,'state':store.get_job(p['job_id'])['state'],'account_profile_sha256':p['account_profile_sha256']}
        except ValueError as e:raise HTTPException(409 if str(e).startswith('409:') else 422,str(e))
    @app.get('/v2/jobs/{id}/review-data')
    def v2_review_data(id:str,req:Request,revision:int=1):
        operator(req);get_job(id,revision);folder=v2_work(id,revision)
        return {'script_plan':read_sidecar(folder,'script-plan.json'),
                'script_generation':read_sidecar(folder,'script-generation.json'),
                'narration_plan':read_sidecar(folder,'narration-plan.json'),
                'subtitle_review':read_sidecar(folder,'subtitle-review.json'),
                'shot_library':read_sidecar(folder,'shot-library.json'),
                'clef_audit':read_sidecar(folder,'clef-audit.json'),
                'human_score_table':read_sidecar(folder,'human-score-table.json'),
                'edit_decision':read_sidecar(folder,'edit-decision.json'),
                'confirmations':confirmations(folder)}
    @app.post('/v2/jobs/{id}/confirm-script')
    def v2_confirm_script(id:str,p:dict,req:Request):
        operator(req);rev=int(p.get('revision',1));job=get_job(id,rev)
        if not p.get('operator_id','').strip():raise HTTPException(422,'OPERATOR_ID_REQUIRED')
        if job['state']!='AWAIT_SCRIPT_CONFIRM':raise HTTPException(409,'NOT_AWAITING_SCRIPT_CONFIRM')
        folder=v2_work(id,rev);plan=read_sidecar(folder,'script-plan.json')
        if not plan:raise HTTPException(409,'SCRIPT_PLAN_NOT_READY')
        actual_sha=__import__('hashlib').sha256(plan['script_text'].encode('utf-8')).hexdigest()
        if actual_sha!=plan['script_sha256']:raise HTTPException(409,'SCRIPT_PLAN_HASH_MISMATCH')
        # Hash is optional for older API callers. The current UI always sends
        # the hash of the saved text it displayed, never unsaved textarea text.
        if 'script_sha256' in p and p['script_sha256']!=actual_sha:
            raise HTTPException(409,'SCRIPT_CONFIRM_HASH_MISMATCH')
        drops=p.get('drop_sentences') or []
        if drops:
            # deterministic script cleanup: drop whole generated sentences (e.g.
            # usage lines the skill forbids), then rebuild text/sha and re-persist.
            kept=[s for s in plan.get('sentences',[]) if s.get('sentence_id') not in set(drops)]
            if not kept:raise HTTPException(422,'DROP_ALL_SENTENCES')
            plan['sentences']=kept
            plan['script_text']=''.join(s['text'] for s in kept)
            plan['script_sha256']=__import__('hashlib').sha256(plan['script_text'].encode('utf-8')).hexdigest()
            atomic_json(folder/'script-plan.json',plan)
        record_confirmation(folder,'SCRIPT_CONFIRM',plan['script_sha256'],p['operator_id'])
        store.event(id,rev,'SCRIPT_CONFIRMED',{'script_sha256':plan['script_sha256'],'operator_id':p['operator_id']})
        store.set_state(id,rev,'READY');return {'state':'READY'}
    @app.post('/v2/jobs/{id}/confirm-narration')
    def v2_confirm_narration(id:str,p:dict,req:Request):
        operator(req);rev=int(p.get('revision',1));job=get_job(id,rev)
        if not p.get('operator_id','').strip():raise HTTPException(422,'OPERATOR_ID_REQUIRED')
        if job['state']!='AWAIT_NARRATION_CONFIRM':raise HTTPException(409,'NOT_AWAITING_NARRATION_CONFIRM')
        folder=v2_work(id,rev);plan=read_sidecar(folder,'narration-plan.json')
        if not plan:raise HTTPException(409,'NARRATION_PLAN_NOT_READY')
        sha=plan['source']['audio_sha256']
        path=Path(plan['source']['audio_path'])
        if not path.is_file() or digest(path)!=sha:raise HTTPException(409,'NARRATION_AUDIO_HASH_MISMATCH')
        record_confirmation(folder,'NARRATION_CONFIRM',sha,p['operator_id'])
        store.event(id,rev,'NARRATION_CONFIRMED',{'audio_sha256':sha,'operator_id':p['operator_id']})
        store.set_state(id,rev,'READY');return {'state':'READY'}
    @app.post('/v2/jobs/{id}/subtitle-decisions')
    def v2_subtitle_decisions(id:str,p:dict,req:Request):
        operator(req);rev=int(p.get('revision',1));job=get_job(id,rev)
        if job['state']!='AWAIT_SUBTITLE_REVIEW':raise HTTPException(409,'NOT_AWAITING_SUBTITLE_REVIEW')
        if not p.get('operator_id','').strip():raise HTTPException(422,'OPERATOR_ID_REQUIRED')
        from scheduler.subtitle_review import apply_decision,unresolved_items,REDUB
        folder=v2_work(id,rev);review=read_sidecar(folder,'subtitle-review.json')
        if not review:raise HTTPException(409,'SUBTITLE_REVIEW_NOT_READY')
        review=apply_decision(review,p['sentence_id'],{'choice':p.get('choice'),'operator_id':p['operator_id'],'notes':p.get('notes','')})
        atomic_json(folder/'subtitle-review.json',review)
        decided=next(i['decision'] for i in review['items'] if i['sentence_id']==p['sentence_id'])
        data=confirmations(folder);data.setdefault('subtitle_decisions',{})[p['sentence_id']]=decided
        atomic_json(folder/'stage-confirmations.json',data)
        unresolved=unresolved_items(review,stop_on_speech_error=True)
        if REDUB in unresolved:
            store.set_state(id,rev,'NEEDS_REDUB',{'code':REDUB,'message':'声音错读需重配：'+p['sentence_id']});return {'state':'NEEDS_REDUB','unresolved':unresolved}
        if unresolved:return {'state':'AWAIT_SUBTITLE_REVIEW','unresolved':unresolved}
        store.set_state(id,rev,'READY');store.event(id,rev,'SUBTITLE_REVIEW_CLOSED',{'decisions':sorted(data['subtitle_decisions'])})
        return {'state':'READY','unresolved':[]}
    @app.get('/v2/jobs/{id}/narration-audio')
    def v2_narration_audio(id:str,req:Request,revision:int=1):
        operator(req);get_job(id,revision);folder=v2_work(id,revision)
        plan=read_sidecar(folder,'narration-plan.json')
        if not plan:raise HTTPException(404,'NARRATION_AUDIO_NOT_READY')
        p=Path(plan['source']['audio_path'])
        if not p.is_file() or digest(p)!=plan['source']['audio_sha256']:raise HTTPException(409,'NARRATION_AUDIO_HASH_MISMATCH')
        return FileResponse(p,media_type='audio/wav' if p.suffix=='.wav' else 'audio/mpeg')
    @app.get('/v2/narration-library')
    def v2_narration_library(req:Request,account:str|None=None):
        operator(req);entries=[]
        for plan_path in sorted((ROOT/'jobs').glob('*/rev-*/narration-plan.json')):
            try:plan=json.loads(plan_path.read_text(encoding='utf-8'))
            except ValueError:continue
            conf=plan.get('confirmation')
            if not conf or conf.get('confirmed') is not True or not conf.get('operator_id'):continue
            if conf.get('audio_sha256_at_confirm')!=plan.get('source',{}).get('audio_sha256'):continue
            parts=plan_path.parts;rev=parts[-2].replace('rev-','')
            origin_job=store.get_job(parts[-3],int(rev))
            origin_account=(origin_job or {}).get('payload',{}).get('account')
            if account is not None and origin_account!=account:continue
            audio=Path(plan['source']['audio_path'])
            if not audio.is_file() or digest(audio)!=plan['source']['audio_sha256']:continue
            sibling=plan_path.parent/'script-plan.json'
            script_preview=''
            try:script_preview=(json.loads(sibling.read_text(encoding='utf-8')).get('script_text') or '')[:60]
            except Exception:pass
            entries.append({'job_id':parts[-3],'revision':int(rev),'account':origin_account,'audio_sha256':plan['source']['audio_sha256'],
                'duration_ticks':plan['source']['actual_duration_ticks'],'mode':plan['source']['mode'],
                'preset':plan['source'].get('preset'),'sentence_count':plan['script']['sentence_count'],
                'script_sha256':plan['script']['script_sha256'],'script_preview':script_preview,
                'confirmed_utc':conf.get('utc'),
                'operator_id':conf['operator_id'],'path':str(audio)})
        # most recently confirmed first for reuse; a fixed-size tail of an
        # alphabetical listing would crowd fresh entries out of the window
        entries.sort(key=lambda e:e.get('confirmed_utc') or '')
        return {'entries':entries[-50:]}
    @app.get('/v2/narration-library/{job_id}/{revision}/audio')
    def v2_narration_library_audio(job_id:str,revision:int,req:Request,account:str|None=None):
        operator(req)
        if account is not None:
            origin_job=store.get_job(job_id,revision)
            origin_account=(origin_job or {}).get('payload',{}).get('account')
            if not origin_account:raise HTTPException(409,'NARRATION_HISTORY_ACCOUNT_UNKNOWN')
            if origin_account!=account:raise HTTPException(409,'NARRATION_HISTORY_ACCOUNT_MISMATCH')
        p=ROOT/'jobs'/job_id/('rev-'+format(revision,'04d'))/'narration-plan.json'
        if not p.is_file():raise HTTPException(404,'ENTRY_NOT_FOUND')
        plan=json.loads(p.read_text(encoding='utf-8'))
        conf=plan.get('confirmation')
        if not conf or conf.get('confirmed') is not True or not conf.get('operator_id'):raise HTTPException(409,'ENTRY_NOT_CONFIRMED')
        if conf.get('audio_sha256_at_confirm')!=plan['source']['audio_sha256']:raise HTTPException(409,'NARRATION_HISTORY_CONFIRMATION_STALE')
        audio=Path(plan['source']['audio_path'])
        if not audio.is_file() or digest(audio)!=plan['source']['audio_sha256']:raise HTTPException(409,'NARRATION_AUDIO_HASH_MISMATCH')
        return FileResponse(audio,media_type='audio/wav' if audio.suffix=='.wav' else 'audio/mpeg')
    @app.get('/v2/jobs/{id}/shot-alternatives')
    def v2_shot_alternatives(id:str,clip_id:str,req:Request,revision:int=1):
        operator(req);get_job(id,revision);folder=v2_work(id,revision)
        decision=read_sidecar(folder,'edit-decision.json');library=read_sidecar(folder,'shot-library.json')
        script_plan=read_sidecar(folder,'script-plan.json')
        if not decision or not library:raise HTTPException(404,'EDIT_DECISION_NOT_READY')
        from scheduler.shot_semantics import RULE_VERSION
        if (library.get('config') or {}).get('use_rule_version')!=RULE_VERSION:
            # r2-era libraries carry no per-observation semantics; new decisions
            # must not pass the old weak gate (M5). Any new revision rebuilds the
            # library locally from the cached observations under current rules.
            raise HTTPException(409,'LIBRARY_RULE_VERSION_STALE:任何新修订会按现行规则从缓存观测本地重建镜头库')
        clip=next((c for c in decision['clips'] if c['clip_id']==clip_id),None)
        if not clip or clip['source']!='pool':raise HTTPException(422,'CLIP_NOT_IN_FREE_POOL')
        from scheduler.shot_planner import candidate_eligibility,_sentence_action,_proposed_windows
        from scheduler.shot_semantics import load_confirmed_intervals
        confirmed_pool=load_confirmed_intervals(ROOT)
        sentences={s['sentence_id']:s for s in (script_plan or {}).get('sentences',[])}
        product_terms=(script_plan or {}).get('product_terms') or []
        sentence=next((sentences[sid] for sid in clip.get('sentence_ids') or [] if sid in sentences),None)
        used={(c['source_sha256'],c['source_in_tick'],c['source_out_tick']) for c in decision['clips'] if c['clip_id']!=clip_id}
        alts=[]
        for c in library['candidates']:
            if not c.get('usable'):continue
            fits=(c['out_tick']-c['in_tick'])>=clip['duration_tick']
            overlap=any(c['source_sha256']==u[0] and c['in_tick']<u[2] and c['in_tick']+clip['duration_tick']>u[1] for u in used)
            if sentence is not None:
                eligible,reason=candidate_eligibility(c,sentence,product_terms,used=None,
                                                      need_ticks=clip['duration_tick'] if fits else None,
                                                      confirmed=confirmed_pool)
            else:
                eligible,reason=fits,None
            alts.append({'candidate_id':c['candidate_id'],'use':c['use'],'in_tick':c['in_tick'],'out_tick':c['out_tick'],
                'description':c.get('product_description'),'window_fits':fits,'window_overlap':overlap,
                'identity_verified':c.get('product_identity_verified',False),
                'supported_actions':[{'kind':a['kind'],'object':a['object'],'evidence':a['evidence'],
                                      'ticks':a['ticks'],'span':a['span'],'gaps':a.get('gaps',[])} for a in c.get('supported_actions') or []],
                'eligible':bool(eligible and fits and not overlap),'ineligible_reason':None if (eligible and fits and not overlap) else (reason or ('WINDOW_TOO_SHORT' if not fits else 'SOURCE_WINDOW_ALREADY_USED')),
                'source_path':c.get('source_path'),'source_sha256':c['source_sha256']})
        alts.sort(key=lambda a:(not a['eligible'],not a['window_fits'],a['window_overlap'],a['candidate_id']))
        proposals=[]
        if sentence is not None:
            intent,_is_action=_sentence_action(sentence)
            if intent['action_kind']:
                proposals=_proposed_windows(library,intent)
        return {'clip':clip,'alternatives':alts,'proposals':proposals,
                'evidence_note':'confirmed intervals are operator-reviewed and reusable by source hash; '
                                'inferred runs/ticks are PROPOSALS awaiting confirmation and never auto-select'}
    @app.post('/v2/jobs/{id}/shot-replacement',status_code=202)
    def v2_shot_replacement(id:str,p:dict,req:Request,if_match:str|None=Header(default=None),idempotency_key:str|None=Header(default=None)):
        operator(req)
        try:
            parent=get_job(id,int(p.get('parent_revision',1)))
            if not p.get('clip_id') or not p.get('candidate_id') or not (p.get('reason') or '').strip():raise ValueError('CLIP_CANDIDATE_REASON_REQUIRED')
            new=copy.deepcopy(parent['payload']);new['revision']=parent['revision']+1;new['parent_revision']=parent['revision']
            # a repeated replacement of the same clip supersedes the earlier entry
            new['v2_overrides']['clip_replacements']=[r for r in new['v2_overrides']['clip_replacements'] if r['clip_id']!=p['clip_id']]
            new['v2_overrides']['clip_replacements'].append({'clip_id':p['clip_id'],'candidate_id':p['candidate_id'],
                'reason':p['reason'],'allow_source_window_overlap':bool(p.get('allow_source_window_overlap',False))})
            new['changed_paths']=['/v2_overrides/clip_replacements']
            tp=ROOT/'jobs'/id/('rev-'+format(parent['revision'],'04d'))/'timeline.json'
            new['overrides']['base_timeline_sha256']=digest(tp) if tp.is_file() else None
            validate_job(new);validate_routes(new)
            if if_match!=parent['input_hash']:raise ValueError('409:IF_MATCH_CONFLICT')
            if not idempotency_key:raise ValueError('IDEMPOTENCY_KEY_REQUIRED')
            from scheduler.revision_plan import plan_revision
            plan=plan_revision(parent['payload'],new,{})
            job=store.submit_job(idempotency_key,new)
            if job['state']=='RECEIVED':store.set_state(id,new['revision'],'READY')
            store.event(id,new['revision'],'REVISION_DIFF',{'invalidated':plan['invalidated'],'reuse':plan['reuse'],'via':'shot-replacement'})
            return {'job_id':id,'revision':new['revision'],'state':store.get_job(id,new['revision'])['state'],'reuse':plan['reuse'],'invalidated':plan['invalidated']}
        except ValueError as e:raise HTTPException(409 if str(e).startswith('409:') else 422,str(e))
    @app.post('/v2/jobs/{id}/confirmed-window',status_code=202)
    def v2_confirmed_window(id:str,p:dict,req:Request,if_match:str|None=Header(default=None),idempotency_key:str|None=Header(default=None)):
        """r3: operator confirms a REAL source window for a sentence whose action
        evidence is insufficient. The confirmation is BOUND to the sentence's own
        (action_kind, action_object) and an explicit source range with basis and
        operator id; it never unlocks a different action and never bypasses
        product/bounds/fixed/reuse constraints (M2)."""
        operator(req)
        try:
            parent=get_job(id,int(p.get('parent_revision',1)))
            for k in ('sentence_id','asset_id','basis'):
                if not p.get(k) or not str(p.get(k)).strip():raise ValueError('CONFIRMED_WINDOW_FIELD_REQUIRED:'+k)
            for k in ('in_tick','out_tick'):
                if not isinstance(p.get(k),int):raise ValueError('CONFIRMED_WINDOW_FIELD_REQUIRED:'+k)
            folder=v2_work(id,int(p.get('parent_revision',1)))
            library=read_sidecar(folder,'shot-library.json');script_plan=read_sidecar(folder,'script-plan.json')
            if not library or not script_plan:raise HTTPException(409,'PLAN_NOT_READY')
            from scheduler.shot_semantics import RULE_VERSION,parse_action_intent
            if (library.get('config') or {}).get('use_rule_version')!=RULE_VERSION:
                raise ValueError('LIBRARY_RULE_VERSION_STALE')
            sentence=next((s for s in script_plan['sentences'] if s['sentence_id']==p['sentence_id']),None)
            if sentence is None:raise ValueError('CONFIRMED_WINDOW_SENTENCE_NOT_FOUND')
            intent=parse_action_intent(sentence['text'])
            if intent['uncertainty'] or not intent['action_kind']:
                raise ValueError('CONFIRMED_WINDOW_SENTENCE_UNRESOLVED')
            src=(library.get('sources') or {}).get(p['asset_id'])
            if src is None or src.get('sha256')!=p.get('source_sha256'):
                raise ValueError('CONFIRMED_WINDOW_SOURCE_NOT_IN_LIBRARY')
            if not (0<=p['in_tick']<p['out_tick']<=src.get('duration_ticks',0)):
                raise ValueError('CONFIRMED_WINDOW_OUT_OF_BOUNDS')
            if p['action_kind']!=intent['action_kind'] or p['action_object']!=intent['action_object']:
                raise ValueError('CONFIRMED_WINDOW_ACTION_MISMATCH:%s needs %s/%s'
                                 %(p['sentence_id'],intent['action_kind'],intent['action_object']))
            product_terms=script_plan.get('product_terms') or []
            grid=' '.join((o.get('text') or '') for o in (library.get('observation_grid') or {}).get(src['sha256'],[])
                          if p['in_tick']<=o.get('tick',-1)<p['out_tick'])
            if [t for t in product_terms if len(t)>=2] and not src.get('product_identity_verified') \
                    and not any(t in grid for t in product_terms):
                raise ValueError('CONFIRMED_WINDOW_PRODUCT_NOT_SHOWN')
            entry={'sentence_id':p['sentence_id'],'source_asset_id':p['asset_id'],
                   'source_sha256':src['sha256'],'in_tick':p['in_tick'],'out_tick':p['out_tick'],
                   'action_kind':intent['action_kind'],'action_object':intent['action_object'],
                   'basis':p['basis'][:2000],'operator_id':(p.get('operator_id') or 'operator')[:64],
                   'allow_source_window_overlap':bool(p.get('allow_source_window_overlap',False))}
            # persist as hash-reusable reviewed interval evidence (M1: this is the
            # only auto-selectable basis; the record carries reviewer + basis)
            from scheduler.shot_semantics import record_confirmed_interval
            record_confirmed_interval(ROOT,{k:entry[k] for k in
                                            ('source_sha256','action_kind','action_object','in_tick','out_tick','basis','operator_id')}
                                      | {'origin':{'kind':'confirmed-window-endpoint','job_id':id,
                                                  'sentence_id':entry['sentence_id']},'utc':utc()})
            new=copy.deepcopy(parent['payload']);new['revision']=parent['revision']+1;new['parent_revision']=parent['revision']
            new['v2_overrides']['confirmed_windows']=[c for c in new['v2_overrides'].get('confirmed_windows',[])
                                                       if not (c['sentence_id']==entry['sentence_id']
                                                               and c['source_sha256']==entry['source_sha256'])]
            new['v2_overrides']['confirmed_windows'].append(entry)
            new['changed_paths']=['/v2_overrides/confirmed_windows']
            tp=ROOT/'jobs'/id/('rev-'+format(parent['revision'],'04d'))/'timeline.json'
            new['overrides']['base_timeline_sha256']=digest(tp) if tp.is_file() else None
            validate_job(new);validate_routes(new)
            if if_match!=parent['input_hash']:raise ValueError('409:IF_MATCH_CONFLICT')
            if not idempotency_key:raise ValueError('IDEMPOTENCY_KEY_REQUIRED')
            job=store.submit_job(idempotency_key,new)
            if job['state']=='RECEIVED':store.set_state(id,new['revision'],'READY')
            store.event(id,new['revision'],'CONFIRMED_WINDOW',{'sentence_id':entry['sentence_id'],
                'window':[entry['in_tick'],entry['out_tick']],'bound_action':[entry['action_kind'],entry['action_object']]})
            return {'job_id':id,'revision':new['revision'],'state':store.get_job(id,new['revision'])['state'],
                    'confirmed':entry}
        except ValueError as e:raise HTTPException(409 if str(e).startswith('409:') else 422,str(e))
    @app.post('/v2/jobs/{id}/whole-clip-replacement',status_code=202)
    def v2_whole_clip_replacement(id:str,p:dict,req:Request,if_match:str|None=Header(default=None),idempotency_key:str|None=Header(default=None)):
        """r4 whole-clip mode: swap one chosen clip's WHOLE source file via the
        normal UI (no JSON). Coarse relevance is re-enforced at planning; the
        picture timeline re-flows and nothing re-synthesizes."""
        operator(req)
        try:
            parent=get_job(id,int(p.get('parent_revision',1)))
            if not p.get('clip_id') or not p.get('source_asset_id') or not (p.get('reason') or '').strip():
                raise ValueError('CLIP_SOURCE_REASON_REQUIRED')
            if parent['payload']['intent'].get('selection_mode')!='whole_clip':
                raise ValueError('NOT_WHOLE_CLIP_MODE')
            new=copy.deepcopy(parent['payload']);new['revision']=parent['revision']+1;new['parent_revision']=parent['revision']
            new['v2_overrides']['whole_clip_replacements']=[r for r in new['v2_overrides'].get('whole_clip_replacements',[])
                                                             if r['clip_id']!=p['clip_id']]
            new['v2_overrides']['whole_clip_replacements'].append({'clip_id':p['clip_id'],
                                                                   'source_asset_id':p['source_asset_id'],
                                                                   'reason':p['reason'][:2000]})
            new['changed_paths']=['/v2_overrides/whole_clip_replacements']
            tp=ROOT/'jobs'/id/('rev-'+format(parent['revision'],'04d'))/'timeline.json'
            new['overrides']['base_timeline_sha256']=digest(tp) if tp.is_file() else None
            validate_job(new);validate_routes(new)
            if if_match!=parent['input_hash']:raise ValueError('409:IF_MATCH_CONFLICT')
            if not idempotency_key:raise ValueError('IDEMPOTENCY_KEY_REQUIRED')
            from scheduler.revision_plan import plan_revision
            plan=plan_revision(parent['payload'],new,{})
            job=store.submit_job(idempotency_key,new)
            if job['state']=='RECEIVED':store.set_state(id,new['revision'],'READY')
            store.event(id,new['revision'],'REVISION_DIFF',{'invalidated':plan['invalidated'],'reuse':plan['reuse'],'via':'whole-clip-replacement'})
            return {'job_id':id,'revision':new['revision'],'state':store.get_job(id,new['revision'])['state']}
        except ValueError as e:raise HTTPException(409 if str(e).startswith('409:') else 422,str(e))
    @app.get('/v2/jobs/{id}/source-media')
    def v2_source_media(id:str,req:Request,revision:int=1,candidate_id:str=None,asset_id:str=None):
        operator(req);get_job(id,revision);folder=v2_work(id,revision)
        path=None;sha=None
        if candidate_id:
            library=read_sidecar(folder,'shot-library.json') or {}
            cand=next((c for c in library.get('candidates',[]) if c.get('candidate_id')==candidate_id),None)
            if not cand:raise HTTPException(404,'CANDIDATE_NOT_FOUND')
            path=cand.get('source_path');sha=cand.get('source_sha256')
        elif asset_id:
            timeline=read_sidecar(folder,'timeline.json') or {}
            ref=next((r for r in timeline.get('resources',[]) if r.get('ref_id')==asset_id),None)
            if not ref:raise HTTPException(404,'ASSET_NOT_IN_TIMELINE')
            path=ref.get('path');sha=ref.get('sha256')
        if not path or not sha:raise HTTPException(404,'SOURCE_MEDIA_NOT_AVAILABLE')
        p=Path(path)
        if not p.is_file() or digest(p)!=sha:raise HTTPException(409,'SOURCE_MEDIA_HASH_MISMATCH')
        return FileResponse(p,media_type='video/mp4')
    @app.get('/v2/jobs/{id}/clef')
    def v2_clef_view(id:str,req:Request,revision:int=1):
        operator(req);get_job(id,revision)
        from scheduler.clef_store import ClefRepository
        return ClefRepository(store).view(id,revision)
    @app.get('/v2/clef/assets/{asset_id}/history')
    def v2_clef_asset_history(asset_id:str,req:Request):
        operator(req)
        from scheduler.clef_store import ClefRepository
        try:
            return ClefRepository(store).asset_history(asset_id)
        except ValueError as e:raise HTTPException(409,str(e))
    @app.get('/v2/clef/decisions')
    def v2_clef_decisions(req:Request,job_id:str|None=None,asset_id:str|None=None,
                          sentence_id:str|None=None,since:str|None=None,until:str|None=None):
        operator(req)
        from scheduler.clef_store import ClefRepository
        return {'decisions':ClefRepository(store).query_decisions(job_id,asset_id,sentence_id,since,until)}
    @app.get('/v2/jobs/{id}/clef-media')
    def v2_clef_media(id:str,req:Request,candidate_id:str,revision:int=1):
        operator(req);get_job(id,revision)
        from scheduler.clef_store import ClefRepository
        view=ClefRepository(store).view(id,revision)
        matches=[row for row in view['candidates'] if row.get('candidate_id')==candidate_id]
        if not matches:raise HTTPException(404,'CLEF_CANDIDATE_NOT_FOUND')
        candidate=matches[0]
        path=candidate.get('source_path') or (candidate.get('native') or {}).get('source_path')
        expected=candidate.get('file_hash') or candidate.get('source_sha256') or (candidate.get('native') or {}).get('source_sha256')
        if not path or not expected:raise HTTPException(404,'CLEF_MEDIA_NOT_BOUND')
        media=Path(path)
        if not media.is_file() or digest(media)!=expected:raise HTTPException(409,'CLEF_MEDIA_HASH_MISMATCH')
        return FileResponse(media,media_type='video/mp4')

    @app.get('/v2/jobs/{id}/human-score-table')
    def v2_human_score_get(id:str,req:Request,revision:int=1):
        operator(req);get_job(id,revision);folder=v2_work(id,revision)
        doc=read_sidecar(folder,'human-score-table.json')
        if not doc:raise HTTPException(404,'HUMAN_SCORE_TABLE_NOT_READY')
        return doc
    @app.post('/v2/jobs/{id}/human-score-table')
    def v2_human_score_submit(id:str,p:dict,req:Request):
        """Validate and persist the operator scoring table, then resume the job."""
        operator(req);rev=int(p.get('revision',1));job=get_job(id,rev)
        if job['state']!='AWAIT_HUMAN_SCORE':raise HTTPException(409,'NOT_AWAITING_HUMAN_SCORE')
        if not p.get('operator_id','').strip():raise HTTPException(422,'OPERATOR_ID_REQUIRED')
        folder=v2_work(id,rev)
        if not (folder/'clef-audit.json').is_file():raise HTTPException(409,'CLEF_AUDIT_NOT_READY')
        from scheduler.human_score import validate_submission, persist_submission, CONFIRM_KIND
        try:
            submitted=validate_submission(p.get('table') or p, folder, operator_id=p['operator_id'])
            meta=persist_submission(folder, submitted)
        except ValueError as e:
            raise HTTPException(422, str(e))
        record_confirmation(folder, CONFIRM_KIND, meta['table_sha256'], p['operator_id'])
        store.event(id,rev,'HUMAN_SCORE_SUBMITTED',{'table_sha256':meta['table_sha256'],'operator_id':p['operator_id'],
                                                     'archive_path':meta['archive_path']})
        store.set_state(id,rev,'READY');return {'state':'READY','table_sha256':meta['table_sha256'],'archive_path':meta['archive_path']}
    @app.get('/v2/human-score-tables')
    def v2_human_score_ledger(req:Request,limit:int=50):
        operator(req)
        from scheduler.human_score import list_ledger
        return {'entries':list_ledger(limit=max(1,min(int(limit or 50),200)))}

    @app.get('/health/live')
    def live():return {'status':'alive','component':'scheduler','pipeline_ready':run_queue}
    @app.get('/health/ready')
    def ready():
        import urllib.request
        results={}
        for kind,port in [('mpt',8761),('narrato',8762),('cosy',9235)]:
            url=f'http://127.0.0.1:{port}/'+('postprod/device-evidence' if kind=='cosy' else 'health/ready')
            try:
                with urllib.request.urlopen(url,timeout=3) as r:results[kind]=json.load(r)
            except Exception:raise HTTPException(503,{'component':kind,'status':'unavailable'})
        return {'status':'ready','components':results,'quality_auto_accepted':False}
    return app
