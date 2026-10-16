"""Execute the approved A+B DAG using isolated upstream components and FFmpeg."""
import copy,json,math,re,shutil,threading,uuid
from pathlib import Path
from scheduler.contracts import validate_job,canonical_hash
from scheduler.semantic import checked_file,validate_timeline
from scheduler.worker_client import operation,request
from scheduler.db import encode,utc
from scheduler.recovery import acquire_lease,heartbeat,require_lease
from workers.common import ROOT,atomic_json,digest,output_ref
from renderer.ffmpeg import probe,decode
from renderer.compiler import build_picture,build_voice,build_mix,caption_and_mux

def cues_from_words(words,duration):
    cues=[];group=[]
    for w in words:
        if not 0<=w['start_tick']<w['end_tick']<=duration:raise ValueError('ASR_TIME_OUT_OF_RANGE')
        if group and (len(''.join(x['text'] for x in group))+len(w['text'])>16 or w['start_tick']-group[-1]['end_tick']>19200):
            cues.append({'cue_id':'cue-'+str(len(cues)+1),'start_tick':group[0]['start_tick'],'end_tick':group[-1]['end_tick'],'text':''.join(x['text'] for x in group),'claim_ids':[]});group=[]
        group.append(w)
    if group:cues.append({'cue_id':'cue-'+str(len(cues)+1),'start_tick':group[0]['start_tick'],'end_tick':group[-1]['end_tick'],'text':''.join(x['text'] for x in group),'claim_ids':[]})
    if not cues:raise ValueError('ASR_EMPTY_RESULT')
    return cues

def apply_subtitle_edits(t,edits):
    cues={c['cue_id']:c for c in t['subtitles']['cues']}
    for e in edits:
        if e['cue_id'] not in cues:raise ValueError('UNKNOWN_SUBTITLE_CUE')
        cues[e['cue_id']].update({k:v for k,v in e.items() if k!='cue_id'})

def json_object(text):
    """First JSON object in a model reply; tolerates code fences or a sentence around it."""
    raw=re.sub(r'^```(?:json)?\s*|\s*```$','',text.strip())
    try:return json.loads(raw)
    except ValueError:
        match=re.search(r'\{.*\}',raw,re.S)
        if not match:raise ValueError('MODEL_REPLY_NOT_JSON')
        return json.loads(match.group())

def allowed_path(path):
    from scheduler.account_profile import asset_roots
    p=Path(path).resolve(strict=True)
    roots=[ROOT]+asset_roots()
    if str(p).startswith('\\\\') or not p.is_file() or not any(p.is_relative_to(r.resolve()) for r in roots):raise ValueError('INPUT_PATH_OUTSIDE_ALLOWLIST')
    return p

def import_file(path,sha=None):
    src=allowed_path(path);actual=digest(src)
    if sha is not None and actual!=sha:raise ValueError('INPUT_HASH_CHANGED')
    dst=ROOT/'artifacts/imports'/actual[:2]/(actual+src.suffix.lower());dst.parent.mkdir(parents=True,exist_ok=True)
    if not dst.exists():
        temp=dst.with_suffix(dst.suffix+'.partial');shutil.copyfile(src,temp)
        if digest(temp)!=actual:raise ValueError('IMPORT_HASH_CHANGED')
        temp.replace(dst)
    if digest(dst)!=actual:raise ValueError('IMMUTABLE_IMPORT_CORRUPTED')
    return dst

def recorded_tts_text(path):
    """Recover the intended speech from its verified local synthesis receipt.
    This does not replace/correct ASR words; actual hearing remains a review.
    """
    p=Path(path);operation_id=p.parent.name
    receipt=ROOT/'state/workers/mpt'/(operation_id+'.json')
    state=json.loads(receipt.read_text(encoding='utf-8'))
    if state['state']!='SUCCEEDED' or not any(r['kind']=='audio' and r['sha256']==digest(p) for r in state['output_refs']):raise ValueError('TTS_SPEECH_BINDING_MISSING')
    envelope=json.loads((ROOT/'state/dispatch/mpt'/(state['node_key']+'.json')).read_text(encoding='utf-8'))['envelope']
    if envelope['op']!='speech.synthesize' or envelope['params']['mode']!='broll':raise ValueError('TTS_SPEECH_BINDING_MISSING')
    return envelope['params']['text']

class Pipeline:
    def __init__(self,store,job):
        self.store=store;self.job=validate_job(job);self.id=job['job_id'];self.rev=job['revision']
        if not re.fullmatch('[A-Za-z0-9_.-]{1,128}',self.id):raise ValueError('JOB_ID_WINDOWS_PATH_UNSUPPORTED')
        self.work=ROOT/'jobs'/self.id/('rev-'+format(self.rev,'04d'));self.work.mkdir(parents=True,exist_ok=True)
        atomic_json(self.work/'job.json',job);self.previous=None;self.last_node=None;self.metrics={};self.holds=[]
        from scheduler.account_profile import for_job,summary
        self.profile=for_job(job);atomic_json(self.work/'account-profile.json',summary(self.profile))
        if job['parent_revision']:
            p=ROOT/'jobs'/self.id/('rev-'+format(job['parent_revision'],'04d'))/'timeline.json'
            if not p.is_file():
                candidates=[ROOT/'jobs'/self.id/('rev-'+format(n,'04d'))/'timeline.json' for n in range(job['parent_revision']-1,0,-1)]
                p=next((f for f in candidates if f.is_file() and digest(f)==job['overrides']['base_timeline_sha256']),p)
            if p.is_file():
                self.previous=json.loads(p.read_text(encoding='utf-8'))
                if job['overrides']['base_timeline_sha256'] and digest(p)!=job['overrides']['base_timeline_sha256']:raise ValueError('BASE_TIMELINE_HASH_MISMATCH')
            elif job['overrides']['base_timeline_sha256']:
                raise ValueError('BASE_TIMELINE_NOT_AVAILABLE')
            # else: a v2 revision before any render (e.g. sentence redub from a
            # NEEDS_REDUB hold) legitimately has no parent timeline yet
            self.previous=None if not p.is_file() else self.previous
    def cancelled(self):
        if self.store.get_job(self.id,self.rev)['state']=='CANCELLED':raise RuntimeError('JOB_CANCELLED')
    def node(self,name,fn):
        self.cancelled();node_id=f'{self.id}:{self.rev}:{name}';attempt=uuid.uuid4().hex;now=utc()
        with self.store.transaction() as c:
            c.execute('INSERT OR REPLACE INTO nodes(node_id,job_id,revision,type,node_key,state) VALUES(?,?,?,?,?,?)',(node_id,self.id,self.rev,name,canonical_hash({'job':self.id,'rev':self.rev,'node':name}),'RUNNING'))
            if self.last_node:c.execute('INSERT OR IGNORE INTO dependencies VALUES(?,?,?)',(node_id,self.last_node,'previous'))
            c.execute('INSERT INTO attempts(attempt_id,node_id,lease_token,state,start_utc) VALUES(?,?,?,?,?)',(attempt,node_id,1,'RUNNING',now))
        self.store.event(self.id,self.rev,'NODE_RUNNING',{'node':name})
        try:
            result=fn()
            with self.store.transaction() as c:
                c.execute("UPDATE nodes SET state='SUCCEEDED',result=? WHERE node_id=?",(encode(result),node_id));c.execute("UPDATE attempts SET state='SUCCEEDED',end_utc=? WHERE attempt_id=?",(utc(),attempt))
            self.last_node=node_id;self.store.event(self.id,self.rev,'NODE_SUCCEEDED',{'node':name});return result
        except Exception as e:
            code=str(e).split(':')[0][:100]
            with self.store.transaction() as c:
                c.execute("UPDATE nodes SET state='FAILED' WHERE node_id=?",(node_id,));c.execute("UPDATE attempts SET state='FAILED',end_utc=?,error_code=? WHERE attempt_id=?",(utc(),code,attempt));c.execute('INSERT INTO errors VALUES(?,?,?,?,?,?)',(uuid.uuid4().hex,node_id,attempt,code,0,None))
            raise
    def worker(self,kind,op,refs,params,name):
        lease_guard=None;lease_value=1
        def fn():
            result,cached=operation(self.store,kind,op,refs,params,publication_guard=lease_guard,lease_token=lease_value);self.metrics[name]={'cached':cached,'operation_id':result['operation_id']};return result
        if op not in ['speech.synthesize','speech.transcribe']:return self.node(name,fn)
        owner=self.id+':'+str(self.rev);token=acquire_lease(self.store,'gpu:0',owner);done=threading.Event()
        lease_guard=lambda:require_lease(self.store,'gpu:0',owner,token);lease_value=token
        def beat():
            while not done.wait(15):heartbeat(self.store,'gpu:0',owner,token)
        thread=threading.Thread(target=beat,daemon=True);thread.start()
        try:
            result=self.node(name,fn);require_lease(self.store,'gpu:0',owner,token);return result
        finally:
            done.set();thread.join(1)
            with self.store.transaction() as c:c.execute('UPDATE leases SET expires_utc=? WHERE resource_id=? AND owner=? AND token=?',(utc(),'gpu:0',owner,token))
    def provider(self,name):return request('http://127.0.0.1:8760/internal/provider-token',{'job_id':self.id,'revision':self.rev,'node_key':name})
    def chat(self,name,system,user,max_tokens=256):
        """One gateway chat call; the gateway picks the route model per node and replays its receipt on re-runs."""
        provider=self.provider(name)
        payload={'model':provider['model'],'max_tokens':max_tokens,'temperature':0,'messages':[{'role':'system','content':system},{'role':'user','content':user}]}
        import urllib.request
        req=urllib.request.Request(provider['base_url']+'/chat/completions',data=json.dumps(payload,ensure_ascii=False).encode(),headers={'Authorization':'Bearer '+provider['operation_token'],'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=900) as r:reply=json.load(r)
        return reply,reply['choices'][0]['message']['content']
    def fact_review(self,text,observations,claims,name):
        """Record an account's own fact-check rules. The engine never stops an edit on narration content."""
        from scheduler.account_profile import rules_text
        rules=rules_text(self.profile,'fact_review','账号附加核查规则：')
        if not rules:
            return {'grounded':None,'status':'SKIPPED','reason':'no_account_rules',
                    'review_scope':'account_fact_rules'}
        system='按下面的账号规则核对旁白，只返回JSON {"grounded":true/false,"reason":"理由"}。'
        reply,content=self.chat(name,system+rules,json.dumps({'旁白':text,'可见观察':observations,'批准的资料声明':claims},ensure_ascii=False))
        result=json_object(content)
        atomic_json(self.work/(name.replace(':','_')+'.json'),dict(result,request_id=reply.get('id'),text_sha256=canonical_hash(text),status='recorded_not_blocking'))
        return result
    def add_ref(self,t,id,path,kind,media=False,provenance=None):
        if any(r['ref_id']==id for r in t['resources']):return id
        path=Path(path);probe_id=None
        if media:
            q=probe(path);f=self.work/(id+'-probe.json');atomic_json(f,q);probe_id=self.add_ref(t,id+'-probe',f,'probe')
        t['resources'].append({'ref_id':id,'path':str(path),'sha256':digest(path),'kind':kind,'probe_ref':probe_id,'provenance_ref':provenance});return id
    def analyze(self,asset,path):
        brief=self.job['intent']['brief']+'；只记录可见外观、人物和动作，不推断功效或口感。'
        return self.worker('narrato','video.analyze',[output_ref(path,'video')],{'provider':self.provider('vision-v1:'+digest(path)),'brief':brief},'analyze-'+asset['asset_id'])
    def run(self):
        try:
            if self.job['schema_version']=='edit-job/v2':return self.run_v2()
            return self.run_v1()
        except Exception as exc:
            if getattr(self,'clef_stage_a',None) is not None:
                self._clef_snapshot(error=type(exc).__name__+':'+str(exc))
            raise

    def _clef_snapshot(self,error=None):
        from scheduler.clef_candidates import build_candidate_snapshot
        from scheduler.clef_store import ClefRepository
        from scheduler.clef_adapter import safe
        stage=self.clef_stage_a
        snapshot=build_candidate_snapshot(self.job['assets'],stage['analysis_refs'],stage.get('library') or {},
                                          material_report=stage.get('material_report'))
        snapshot['stage_status']='COMPLETE' if stage.get('complete') and not error else 'ERROR' if error else 'INCOMPLETE'
        if error:
            snapshot['stage_error']=safe(error)
            snapshot['failed_asset_id']=stage.get('current_asset_id')
        if not stage.get('complete') or error:
            for candidate in snapshot['candidates']:
                candidate['selection_eligible']=False
                candidate['gate_reasons']=list(dict.fromkeys((candidate.get('gate_reasons') or [])+['STAGE_A_INCOMPLETE' if not error else 'STAGE_A_FAILED']))
                if error and candidate.get('asset_id')==stage.get('current_asset_id'):
                    candidate['quality_status']='error'
                    candidate['gate_reasons'].append(safe(error))
        atomic_json(self.work/'clef-candidates.json',snapshot)
        ClefRepository(self.store).record_snapshot(self.id,self.rev,snapshot)
        self.clef_snapshot=snapshot
        return snapshot

    def run_v1(self):
        from scheduler.script_generation import validate_legacy_generation
        parent=self.store.get_job(self.id,self.job['parent_revision']) if self.job.get('parent_revision') else None
        validate_legacy_generation(self.job,parent['payload'] if parent else None)
        self.store.set_state(self.id,self.rev,'RUNNING');t={'schema_version':'timeline/v1','timeline_id':self.id,'job_id':self.id,'revision':self.rev,'timescale':48000,'canvas':{'width':768,'height':1376,'fps':25,'sar':'1:1'},'duration_ticks':0,'resources':[],'video_clips':[],'audio_events':[],'voice_stem_ref':'voice-stem','subtitles':{},'template_events':[],'review':{'status':'pending','evidence_refs':[]}}
        documents={}
        for d in self.job['product_documents']:
            p=import_file(d['path'],d['sha256'])
            if d['media_type']=='application/pdf':raise ValueError('PDF_FACT_EXTRACTION_UNAVAILABLE_REVIEW_REQUIRED')
            documents[d['document_id']]=p.read_text(encoding='utf-8-sig')
        for c in self.job['claims']:
            if c['text'] not in documents[c['document_id']] or c['source_span'] not in documents[c['document_id']]:raise ValueError('CLAIM_SOURCE_NOT_VERIFIED')
        o=self.job['overrides']
        # A revision that edits only caption/BGM uses the exact previous voices and analyses.
        parent=self.store.get_job(self.id,self.job['parent_revision']) if self.job['parent_revision'] else None
        base_equal=parent and all(parent['payload'][k]==self.job[k] for k in ['intent','product_documents','claims','providers'])
        reuse=base_equal and not o['speech_edits'] and (parent['payload']['assets']==self.job['assets'] or bool(o['shot_edits']))
        if reuse:
            t=copy.deepcopy(self.previous);t['revision']=self.rev;t['review']={'status':'pending','evidence_refs':[]}
            clips={c['clip_id']:c for c in t['video_clips']};assets={a['asset_id']:a for a in self.job['assets']}
            for edit in o['shot_edits']:
                clip=clips.get(edit['clip_id']);asset=assets.get(edit['asset_id'])
                if not clip or not asset or clip['mode']!='broll' or asset['mode']!='broll':raise ValueError('LOCKED_SHOT_CHANGE_FORBIDDEN')
                if not edit['preserve_narration']:raise ValueError('SHOT_SPEECH_REVISION_REQUIRED')
                if edit['source_out_tick']-edit['source_in_tick']!=clip['duration_tick']:raise ValueError('SHOT_DURATION_MUST_MATCH')
                path=import_file(asset['path'],asset['sha256']);self.worker('mpt','material.inspect',[output_ref(path,'video')],{},'inspect-replacement-'+asset['asset_id'])
                analysis=self.analyze(asset,path);af=Path(analysis['output_refs'][0]['path']);rid='replacement-'+asset['sha256'][:12]
                self.add_ref(t,rid,path,'video',True);self.add_ref(t,rid+'-visual',af,'evidence')
                observations=[w['text'] for b in json.loads(af.read_text(encoding='utf-8'))['coverage'] for w in b.get('observations',[])]
                oldrefs={r['ref_id']:r for r in t['resources']}
                narration=''.join(recorded_tts_text(oldrefs[e['audio_ref']]['path']) for e in t['audio_events'] if e['role']=='narration' and e['timeline_start_tick']<clip['timeline_start_tick']+clip['duration_tick'] and e['timeline_start_tick']+e['duration_tick']>clip['timeline_start_tick'])
                self.holds.append('替换画面的事实检查绑定原配音请求；实际字幕和同音字仍需独立听审，未用原稿覆盖ASR')
                self.node('replacement-claims-'+asset['asset_id'],lambda:self.fact_review(narration,observations,self.job['claims'],'replacement-review:'+canonical_hash({'narration':narration,'observations':observations})))
                clip.update(base_source_ref=rid,video_ref=rid,source_in_tick=edit['source_in_tick'],source_out_tick=edit['source_out_tick'],visual_evidence_ref=rid+'-visual')
        else:
            cursor=0
            for asset in self.job['assets']:
                id=asset['asset_id'];path=import_file(asset['path'],asset['sha256']);q=probe(path);decode(path,self.work)
                provenance=allowed_path(asset['provenance_ref']);prov=self.add_ref(t,id+'-provenance',import_file(provenance),'manifest')
                self.add_ref(t,id,path,'video',True,prov)
                self.worker('mpt','material.inspect',[output_ref(path,'video')],{},'inspect-'+id)
                analysis=self.analyze(asset,path);af=Path(analysis['output_refs'][0]['path']);self.add_ref(t,id+'-visual',af,'evidence')
                observations=[o['text'] for batch in json.loads(af.read_text(encoding='utf-8'))['coverage'] for o in batch.get('observations',[])]
                mode=asset['mode'];duration=((q['duration_ticks']+1919)//1920 if mode=='spoken_locked' else q['duration_ticks']//1920)*1920
                clip={'clip_id':'clip-'+id,'mode':mode,'base_source_ref':id,'video_ref':id,'source_in_tick':0,'source_out_tick':duration,'timeline_start_tick':cursor,'duration_tick':duration,'speed':1,'boundary_evidence_ref':None,'visual_evidence_ref':id+'-visual','lip_evidence_ref':None,'lip_result_ref':None}
                if mode=='spoken_locked':
                    index=asset['locked']['audio_stream_index']
                    if index!=q['stream_indices']['audio']:raise ValueError('LOCKED_AUDIO_STREAM_INVALID')
                    boundary=allowed_path(asset['locked']['evidence_ref']);proof=json.loads(boundary.read_text(encoding='utf-8'))
                    if proof.get('source_sha256')!=asset['sha256']:raise ValueError('LOCKED_EVIDENCE_HASH_MISMATCH')
                    clip['boundary_evidence_ref']=self.add_ref(t,id+'-boundary',import_file(boundary),'evidence')
                    audio_id=id;self.holds.append('原音轨台词、完整性及嘴型需全段核对：'+id)
                    self.node('claims-'+id,lambda:self.fact_review(asset['locked']['transcript'],observations,self.job['claims'],'locked-review:'+asset['sha256']))
                else:
                    provider=self.provider('script-short-v1:'+asset['sha256'])
                    prompt='根据以下画面观察，写一句不超过12个汉字的旁白。观察：'+json.dumps(observations,ensure_ascii=False)
                    if self.job['claims']:prompt+='。批准资料声明：'+json.dumps(self.job['claims'],ensure_ascii=False)
                    script=self.worker('mpt','script.generate',[],{'provider':provider,'brief':self.job['intent']['brief'],'claim_ids':[c['claim_id'] for c in self.job['claims']],'prompt':prompt},'script-'+id)
                    text=json.loads(Path(script['output_refs'][0]['path']).read_text(encoding='utf-8'))['text']
                    for edit in o['speech_edits']:
                        if edit['event_id']=='speech-'+id:text=edit['text']
                    self.node('claims-'+id,lambda:self.fact_review(text,observations,self.job['claims'],'script-review:'+canonical_hash({'text':text,'obs':observations,'claims':self.job['claims']})))
                    voice=self.worker('mpt','speech.synthesize',[],{'mode':'broll','preset':self.profile['voice']['default_preset'],'text':text},'tts-'+id)
                    vp=Path(next(r['path'] for r in voice['output_refs'] if r['kind']=='audio'));vq=probe(vp)
                    if vq['duration_ticks']>duration:raise ValueError('VOICE_LONGER_THAN_PICTURE_REVIEW_REQUIRED')
                    audio_id=self.add_ref(t,id+'-voice',vp,'audio',True);index=vq['stream_indices']['audio']
                t['video_clips'].append(clip)
                t['audio_events'].append({'event_id':'speech-'+id,'role':'dialogue' if mode=='spoken_locked' else 'narration','audio_ref':audio_id,'stream_index':index,'source_in_tick':0,'source_out_tick':duration if mode=='spoken_locked' else min(duration,vq['duration_ticks']),'timeline_start_tick':cursor,'duration_tick':duration if mode=='spoken_locked' else min(duration,vq['duration_ticks']),'gain_db':0,'sync_clip_id':clip['clip_id'] if mode=='spoken_locked' else None,'claim_ids':[c['claim_id'] for c in self.job['claims']],'alignment_ref':None})
                cursor+=duration
            t['duration_ticks']=cursor
            voice,cached=build_voice(t,self.work);self.metrics['voice']={'cached':cached,'sha256':digest(voice)};self.add_ref(t,'voice-stem',voice,'audio',True)
            asr=self.worker('mpt','speech.transcribe',[output_ref(voice,'audio')],{},'asr-actual-voice-stem');alignment=Path(next(r['path'] for r in asr['output_refs'] if r['kind']=='evidence'));self.add_ref(t,'alignment',alignment,'evidence')
            words=json.loads(alignment.read_text(encoding='utf-8'))['words'];t['subtitles']={'source_audio_ref':'voice-stem','source_audio_sha256':digest(voice),'alignment_ref':'alignment','template':self.job['templates']['subtitle'],'cues':cues_from_words(words,cursor)}
            for a in t['audio_events']:a['alignment_ref']='alignment'
        return self._finish(t)

    def _finish(self,t):
        refs={r['ref_id']:r for r in t['resources']};voice=Path(refs['voice-stem']['path']);o=self.job['overrides']
        t['audio_events']=[a for a in t['audio_events'] if a['role']!='bgm']
        if self.job['templates']['bgm'] is not None:
            override=o['bgm_override'];default=self.profile['music']
            if not override and not default:raise ValueError('BGM_DEFAULT_NOT_CONFIGURED:'+self.profile['account'])
            music=import_file(override['path'],override['sha256']) if override else import_file(default['path'],default['sha256'])
            self.worker('mpt','music.select',[output_ref(music,'audio')],{},'music-select')
            # A new ref ID avoids ambiguity when the replacement has a new hash.
            license_path=import_file(override['provenance_ref'] if override else default['license_ref'])
            mid='music-'+digest(music)[:12];provenance_id=self.add_ref(t,mid+'-license',license_path,'manifest');self.add_ref(t,mid,music,'audio',True,provenance_id)
            t['audio_events'].append({'event_id':'bgm','role':'bgm','audio_ref':mid,'stream_index':probe(music)['stream_indices']['audio'],'source_in_tick':0,'source_out_tick':t['duration_ticks'],'timeline_start_tick':0,'duration_tick':t['duration_ticks'],'gain_db':override['gain_db'] if override else default['gain_db'],'sync_clip_id':None,'claim_ids':[],'alignment_ref':None})
        apply_subtitle_edits(t,o['subtitle_edits']);validate_timeline(t,self.job)
        atomic_json(self.work/'timeline.json',t)
        authpath=ROOT/'state/deployment/color-preview-authorization.json';auth=json.loads(authpath.read_text(encoding='utf-8')) if authpath.exists() else None
        picture,pm=build_picture(t,self.work,auth);mix,mm=build_mix(t,voice,self.work);final,fm=caption_and_mux(t,picture,mix,self.work)
        self.metrics.update(picture=pm,mix=mm,final=fm)
        from renderer.validate import validate_output
        qa=self.node('technical-qa',lambda:validate_output(t,final,voice,self.work))
        delivery=self.work/'delivery';delivery.mkdir(exist_ok=True);out=delivery/'preview.mp4'
        if not out.exists():shutil.copyfile(final,out)
        elif digest(out)!=digest(final):raise ValueError('IMMUTABLE_DELIVERY_CONFLICT')
        result={'job_id':self.id,'revision':self.rev,'state':'REVIEW_REQUIRED','technical_status':'PASS' if qa['passed'] else 'FAIL','output_path':str(out),'artifact_sha256':digest(out),'timeline_sha256':digest(self.work/'timeline.json'),'metrics':self.metrics,'qa':qa,'quality_holds':self.holds+['色彩、真实视觉、声音听感及实际字幕须审阅；模型描述不等于合格证据'],'new_h3_generations':0,'native_final_renderer_calls':0}
        if qa['passed'] and getattr(self,'clef_decision_id',None):
            from scheduler.clef_store import ClefRepository
            usage=copy.deepcopy(t)
            usage['render_evidence']={'status':'SUCCEEDED','technical_status':'PASS','output_sha256':digest(out),'output_path':str(out)}
            ClefRepository(self.store).record_usage(self.id,self.rev,self.clef_decision_id,usage)
        atomic_json(self.work/'result.json',result)
        with self.store.transaction() as c:c.execute('INSERT OR IGNORE INTO artifacts VALUES(?,?,?,?,?,?)',(digest(out),'video',str(out.relative_to(ROOT)),out.stat().st_size,None,1))
        if qa['passed']:
            from scheduler.output_catalog import publish
            publish(self.job, out, '待审核', digest(out))
        self.store.set_state(self.id,self.rev,'REVIEW_REQUIRED' if qa['passed'] else 'FAILED_FINAL',None if qa['passed'] else qa);return result

    # ---- edit-job/v2 orchestration ----
    VISUAL_ONLY_BRIEF='只记录可见外观、人物和动作，不推断功效或口感。'

    def _stage_confirmations(self):
        p=self.work/'stage-confirmations.json'
        return json.loads(p.read_text(encoding='utf-8')) if p.is_file() else {}

    def _parent_confirmations(self):
        parent=self.job.get('parent_revision')
        if not parent:return {}
        previous=self.store.get_job(self.id,parent)
        if previous:
            from scheduler.script_generation import source_hash
            old_input=previous['payload'].get('script_input',{})
            new_input=self.job.get('script_input',{})
            if 'generated' in (old_input.get('mode'),new_input.get('mode')) and source_hash(previous['payload'])!=source_hash(self.job):return {}
            old_skill=previous['payload'].get('script_input',{}).get('skill')
            new_skill=self.job.get('script_input',{}).get('skill')
            if old_skill!=new_skill:return {}
        p=ROOT/'jobs'/self.id/('rev-'+format(parent,'04d'))/'stage-confirmations.json'
        return json.loads(p.read_text(encoding='utf-8')) if p.is_file() else {}

    def _gate(self,kind,artifact_sha):
        conf=self._stage_confirmations().get(kind)
        if not conf or conf.get('sha256')!=artifact_sha or not conf.get('operator_id'):
            # A revision inherits a parent confirmation only when the artifact is
            # byte-identical; any script/audio change invalidates it naturally.
            inherited=self._parent_confirmations().get(kind)
            if inherited and inherited.get('sha256')==artifact_sha and inherited.get('operator_id'):
                data=self._stage_confirmations();data[kind]=inherited
                atomic_json(self.work/'stage-confirmations.json',data);conf=inherited
        if not conf or conf.get('sha256')!=artifact_sha or not conf.get('operator_id'):
            raise ValueError(kind+'_REQUIRED:'+artifact_sha)
        return conf

    def _prepare_script_source(self):
        from scheduler.script_generation import prepare_source
        self.script_source=prepare_source(ROOT,self.job,self.store)

    def _build_full_script(self,evidence,library):
        from scheduler.script_generation import build_full_script
        def chat(name,system,prompt,**kwargs):
            return self.node('script-full',lambda:self.chat(name,system,prompt,**kwargs))
        return build_full_script(self.job,self.profile,evidence,library,self.script_source,self.work,chat)

    def run_v2(self):
        from scheduler.script_plan import build_script_plan
        from scheduler.narration import cap_pauses,prepare_narration,fill_sentence_windows
        from scheduler.shot_library import build_shot_library
        from scheduler.subtitle_review import compare_script_audio,apply_decision,unresolved_items,build_cues,REDUB
        from scheduler.shot_planner import plan_shots,plan_whole_clips,apply_replacements,compile_edit_decision
        # Validate explicit binding before any paid analysis or inference.
        self._prepare_script_source()
        self.store.set_state(self.id,self.rev,'RUNNING')
        t={'schema_version':'timeline/v1','timeline_id':self.id,'job_id':self.id,'revision':self.rev,'timescale':48000,'canvas':{'width':768,'height':1376,'fps':25,'sar':'1:1'},'duration_ticks':0,'resources':[],'video_clips':[],'audio_events':[],'voice_stem_ref':'voice-stem','subtitles':{},'template_events':[],'review':{'status':'pending','evidence_refs':[]}}
        documents={}
        for d in self.job['product_documents']:
            p=import_file(d['path'],d['sha256'])
            if d['media_type']=='application/pdf':raise ValueError('PDF_FACT_EXTRACTION_UNAVAILABLE_REVIEW_REQUIRED')
            documents[d['document_id']]=p.read_text(encoding='utf-8-sig')
        for c in self.job['claims']:
            if c['text'] not in documents[c['document_id']] or c['source_span'] not in documents[c['document_id']]:raise ValueError('CLAIM_SOURCE_NOT_VERIFIED')
        # Stage A: import + per-file analysis barrier, then one library for all files
        from scheduler.beat_planner import card_coverage,material_from_card,plan_beats,script_menu
        analysis_refs=[];sources={};fixed_constraints=[];materials=[]
        self.clef_stage_a={'analysis_refs':analysis_refs,'library':{}}
        self._clef_snapshot()
        for asset in self.job['assets']:
            self.clef_stage_a['current_asset_id']=asset['asset_id']
            id=asset['asset_id'];path=import_file(asset['path'],asset['sha256']);q=probe(path);decode(path,self.work)
            provenance=allowed_path(asset['provenance_ref']);prov_file=import_file(provenance)
            prov=self.add_ref(t,id+'-provenance',prov_file,'manifest')
            self.add_ref(t,id,path,'video',True,prov)
            self.worker('mpt','material.inspect',[output_ref(path,'video')],{},'inspect-'+id)
            prov_json=json.loads(prov_file.read_text(encoding='utf-8-sig'))
            material=material_from_card(prov_json,q['duration_ticks']) if asset['role']=='product' else None
            if asset['analysis']['mode']=='material_card':
                # The library's reviewed card is the visual evidence; no paid vision call.
                if not material or not material['usable']:raise ValueError('MATERIAL_CARD_REQUIRED:'+id)
                coverage=card_coverage(material);af=self.work/(id+'-card-evidence.json')
                atomic_json(af,{'asset_hash':asset['sha256'],'coverage':coverage,'usable':True,'source':'material_card'})
            else:
                analysis=self.worker('narrato','video.analyze',[output_ref(path,'video')],{'provider':self.provider('vision-v1:'+digest(path)),'brief':self.VISUAL_ONLY_BRIEF},'analyze-'+id)
                af=Path(analysis['output_refs'][0]['path']);coverage=json.loads(af.read_text(encoding='utf-8'))['coverage']
            detect=None
            if asset['analysis']['mode']=='detect_scenes':
                det=self.worker('narrato','scene.detect',[output_ref(path,'video')],{'threshold':12,'min_scene_len_frames':12},'scenes-'+id)
                dj=json.loads(Path(det['output_refs'][0]['path']).read_text(encoding='utf-8'))
                detect={'cuts':dj['cuts'],'config_hash':dj['config_hash'],'detector':dj['config']['detector']}
            if material:materials.append(dict(material,asset_id=id,source_sha256=asset['sha256']))
            analysis_refs.append({'asset_id':id,'source_sha256':asset['sha256'],'path':str(path),
                'usable':bool(coverage) and all(c['status']=='success' for c in coverage),
                'duration_ticks':q['duration_ticks'],'coverage':coverage,'detect':detect,
                'product_identity_verified':bool(prov_json.get('product_identity_verified',False)),
                'analysis_evidence_sha256':digest(af)})
            self._clef_snapshot()
            src={'asset_id':id,'path':str(path),'sha256':asset['sha256'],
                 'probe':{'path':str(self.work/(id+'-probe.json')),'sha256':digest(self.work/(id+'-probe.json')),'stream_indices':q['stream_indices'],'duration_ticks':q['duration_ticks']},
                 'visual_evidence':{'path':str(af),'sha256':digest(af)}}
            if asset['role'] in ('fixed_opening','fixed_closing','locked_body'):
                index=asset['locked']['audio_stream_index']
                if index!=q['stream_indices']['audio']:raise ValueError('LOCKED_AUDIO_STREAM_INVALID')
                boundary=allowed_path(asset['locked']['evidence_ref']);proof=json.loads(boundary.read_text(encoding='utf-8-sig'))
                if proof.get('source_sha256')!=asset['sha256']:raise ValueError('LOCKED_EVIDENCE_HASH_MISMATCH')
                bp=import_file(boundary);self.add_ref(t,id+'-boundary',bp,'evidence')
                src['locked']={'audio_stream_index':index,'boundary':{'path':str(bp),'sha256':digest(bp)},'transcript':asset['locked']['transcript']}
                observations=[o['text'] for c in coverage for o in c.get('observations',[])]
                self.holds.append('固定片段原音画、台词完整性与嘴型需全段核对：'+id)
                self.node('claims-'+id,lambda: self.fact_review(asset['locked']['transcript'],observations,self.job['claims'],'locked-review:'+asset['sha256']))
                fixed_constraints.append({'asset_id':id,'role':asset['role'],'source_sha256':asset['sha256'],
                    'duration_ticks':((q['duration_ticks']+1919)//1920)*1920})
            sources[id]=src
        # Optional reviewed-material manifest (koc-v1): PASS entries bound to this job's
        # own product assets become explicit material candidates. The manifest gate
        # governs every asset it binds — their Narrato analysis cannot bypass a refusal —
        # and production path refuses TEST-flagged manifests.
        material_cands=None
        all_analysis_refs=list(analysis_refs)
        manifest_spec=self.job.get('material_manifest')
        if manifest_spec:
            from scheduler.material_manifest import import_material_manifest
            material_import=import_material_manifest(self.job['assets'],manifest_spec,mode='production')
            self.clef_stage_a['material_report']=dict(material_import,bound_shas=sorted(material_import.get('bound_shas') or []))
            atomic_json(self.work/'material-manifest-import.json',material_import['report'])
            material_cands=material_import['candidates']
            bound=material_import['bound_shas']
            if bound:
                analysis_refs=[r for r in analysis_refs if r.get('source_sha256') not in bound]
        library=build_shot_library(self.job['assets'],analysis_refs,{'job_id':self.id,'revision':self.rev},material_candidates=material_cands)
        atomic_json(self.work/'shot-library.json',library)
        self.clef_stage_a.update(analysis_refs=all_analysis_refs,library=library,complete=True)
        snapshot=self._clef_snapshot()
        self.clef_stage_a=None
        # Stage B: full script (pasted never generates), confirmed before any dependent stage
        product_evidence={'brief':self.job['intent']['brief'],'claims':self.job['claims'],'documents':documents,
                          'product_terms':self.job['intent'].get('product_terms') or []}
        if materials:
            atomic_json(self.work/'material-cards.json',materials)
            product_evidence['materials']=script_menu(materials)
        cps=self.profile['voice'].get('chars_per_second')
        if cps:
            seconds=self.job['intent']['target_duration_ticks']/48000
            product_evidence.update(max_seconds=round(seconds),max_chars=int(seconds*cps))
        script_plan=self._build_full_script(product_evidence,library)
        if self.job.get('profile')=='selling':
            from scheduler.clef_adapter import ClefDecisionAdapter
            from scheduler.clef_script_gate import block_message,review_selling_script
            from scheduler.clef_store import ClefRepository
            clef_mode=(self.job.get('clef_selector') or {}).get('mode')
            if clef_mode=='off':
                # The job explicitly turned the structure gate off: record the skip
                # instead of forcing a local GPU Clef run (backend=local otherwise
                # fails on busy GPUs and stalls every script as NEEDS_REVIEW).
                verdict={'schema_version':'script-structure/v1','status':'SKIPPED','clef_status':'OFF',
                         'fact_status':'SKIPPED','hits':[],'unread':[],'hard_blocks':[],'efficacy_risk':False,
                         'note':'clef_selector.mode=off: this job explicitly disables the structure gate'}
            else:
                verdict=review_selling_script(ClefDecisionAdapter(self.job.get('clef_selector')),script_plan,self.job,
                                              ClefRepository(self.store),self.profile.get('voice',{}).get('chars_per_second'),
                                              documents,self.profile['prompt_rules']['script'])
            atomic_json(self.work/'script-structure.json',verdict)
            if verdict['status']=='HARD_BLOCK':raise ValueError('SCRIPT_FACT_BLOCK:'+block_message(verdict))
            if verdict['status']=='REPAIR':raise ValueError('SCRIPT_STRUCTURE_REPAIR:'+block_message(verdict))
            if verdict['status']=='NEEDS_REVIEW':raise ValueError('SCRIPT_STRUCTURE_NEEDS_REVIEW:'+block_message(verdict))
            if verdict.get('efficacy_risk'):self._gate('EFFICACY_REVIEW',script_plan['script_sha256'])
        self._gate('SCRIPT_CONFIRM',script_plan['script_sha256'])
        observations=[o for c in library['candidates'] for o in (c['evidence'].get('observation_texts') or [])][:40]
        self.node('claims-script',lambda:self.fact_review(script_plan['script_text'],observations,self.job['claims'],'script-review:'+canonical_hash({'text':script_plan['script_text'],'obs':observations,'claims':self.job['claims']})))
        # Stage C: exactly one narration entrance; uploaded/history never synthesize
        def resolver(job_id,revision):
            p=ROOT/'jobs'/job_id/('rev-'+format(revision,'04d'))/'narration-plan.json'
            if not p.is_file():return None
            prior=json.loads(p.read_text(encoding='utf-8'))
            record=self.store.get_job(job_id,revision)
            # Resolve ownership from the authoritative job; never infer it from
            # a voice label shared by more than one account.
            prior['_account']=(record or {}).get('payload',{}).get('account')
            return prior
        def tts(text,preset):
            voice=self.worker('mpt','speech.synthesize',[],{'mode':'broll','preset':preset,'text':text},'tts-narration')
            vp=Path(next(r['path'] for r in voice['output_refs'] if r['kind']=='audio'))
            config={'preset':preset,'voice':self.profile['voice']['voice_id']}
            cap=self.profile['voice'].get('max_pause_ms')
            if cap:
                vp=cap_pauses(vp,vp.parent,cap);config['max_pause_ms']=cap
            return {'path':str(vp),'sha256':digest(vp),'config':config}
        narration_plan=prepare_narration(self.job,script_plan,None,tts=tts,resolver=resolver,voice=self.profile['voice']['voice_id'])
        atomic_json(self.work/'narration-plan.json',narration_plan)
        limit=self.job['intent'].get('max_duration_ticks')
        if limit and narration_plan['source']['actual_duration_ticks']>limit:
            raise ValueError('NARRATION_EXCEEDS_MAX_DURATION:%.2fs>%.2fs'%(narration_plan['source']['actual_duration_ticks']/48000,limit/48000))
        nconf=self._gate('NARRATION_CONFIRM',narration_plan['source']['audio_sha256'])
        narration_plan['confirmation']={'confirmed':True,'operator_id':nconf['operator_id'],'utc':nconf.get('utc'),'audio_sha256_at_confirm':nconf['sha256']}
        # persist the confirmation into the sidecar so the narration library can
        # list this audio as a reusable confirmed source
        atomic_json(self.work/'narration-plan.json',narration_plan)
        npath=Path(narration_plan['source']['audio_path']);self.add_ref(t,'narration-audio',npath,'audio',True)
        # Stage D: real-audio alignment and script comparison, then operator listening decisions
        asr=self.worker('mpt','speech.transcribe',[output_ref(npath,'audio')],{},'asr-narration')
        alignment=Path(next(r['path'] for r in asr['output_refs'] if r['kind']=='evidence'));self.add_ref(t,'alignment',alignment,'evidence')
        review=compare_script_audio(script_plan,json.loads(alignment.read_text(encoding='utf-8')),narration_plan)
        review['audio']['alignment_evidence_sha256']=digest(alignment)
        # Operator sentence redubs replace that sentence's audio and text (S6).
        # The replacement audio source is explicit: TTS only when the job itself is
        # auto-narrated (same preset); uploaded/history narration requires the
        # operator's own corrected audio for the sentence.
        redubs={r['sentence_id']:r for r in self.job['v2_overrides']['sentence_redubs']}
        redub_audio={}
        redub_orig_window={}
        if redubs:
            sp_by_id={s['sentence_id']:s for s in script_plan['sentences']}
            from scheduler.script_plan import classify_visual
            for sid,r in redubs.items():
                if sid not in sp_by_id:raise ValueError('REDUB_SENTENCE_NOT_FOUND:'+sid)
                if self.job['narration_input']['mode']=='auto' and not r.get('audio'):
                    voice=self.worker('mpt','speech.synthesize',[],{'mode':'broll','preset':self.job['narration_input']['preset'],'text':r['text']},'redub-'+sid)
                    rp=Path(next(r2['path'] for r2 in voice['output_refs'] if r2['kind']=='audio'))
                elif r.get('audio'):
                    rp=Path(import_file(r['audio']['path'],r['audio']['sha256']))
                else:
                    raise ValueError('REDUB_AUDIO_SOURCE_REQUIRED:'+sid)
                rq=probe(rp)
                self.add_ref(t,'redub-'+sid,rp,'audio',True)
                redub_audio[sid]={'path':str(rp),'sha256':digest(rp),'probe':{'path':str(self.work/('redub-'+sid+'-probe.json')),'sha256':digest(self.work/('redub-'+sid+'-probe.json')),'stream_indices':rq['stream_indices'],'duration_ticks':rq['duration_ticks']},'duration_ticks':rq['duration_ticks']}
                mini_asr=self.worker('mpt','speech.transcribe',[output_ref(rp,'audio')],{},'asr-redub-'+sid)
                mini_align=Path(next(r2['path'] for r2 in mini_asr['output_refs'] if r2['kind']=='evidence'))
                mini_plan={'schema_version':'narration-plan/v1','source':{'audio_sha256':digest(rp),'actual_duration_ticks':rq['duration_ticks']},'sentences':[]}
                mini_script={'schema_version':'script-plan/v1','job_id':self.id,'revision':self.rev,'script_text':r['text'],
                             'script_sha256':canonical_hash({'t':r['text']}),'purpose':script_plan.get('purpose',''),
                             'product_terms':script_plan.get('product_terms') or [],
                             'sentences':[dict(sp_by_id[sid],text=r['text'])],'library_sha256':script_plan.get('library_sha256')}
                mini=compare_script_audio(mini_script,json.loads(mini_align.read_text(encoding='utf-8')),mini_plan)
                item=mini['items'][0];item['sentence_id']=sid
                for old in review['items']:
                    if old['sentence_id']==sid:
                        # keep the ORIGINAL narration coordinates for timeline placement;
                        # the mini item's window is local to the redub audio
                        if old.get('window'):redub_orig_window[sid]=dict(old['window'])
                        review['items'][review['items'].index(old)]=item
                # the new sentence text re-enters fact review and visual-need extraction
                sentence=dict(sp_by_id[sid],text=r['text'],visual=classify_visual(r['text']),
                              claim_ids=[c['claim_id'] for c in self.job['claims'] if c['text'] and c['text'] in r['text']])
                sp_by_id[sid]=sentence
                self.node('claims-redub-'+sid,lambda s=sid,txt=r['text']:self.fact_review(txt,observations,self.job['claims'],'redub-review:'+canonical_hash({'sid':s,'text':txt})))
            script_plan['sentences']=[sp_by_id[s['sentence_id']] for s in script_plan['sentences']]
            script_plan['script_text']=''.join(s['text'] for s in script_plan['sentences'])
            script_plan['script_sha256']=__import__('hashlib').sha256(script_plan['script_text'].encode('utf-8')).hexdigest()
            atomic_json(self.work/'script-plan.json',script_plan)
        decisions=self._stage_confirmations().get('subtitle_decisions')
        if not decisions:
            inherited=self._parent_confirmations().get('subtitle_decisions')
            if inherited:
                # only decisions whose recorded audio hash still matches the item they
                # target (main narration or that sentence's redub clip) survive
                decisions=dict(inherited)
                if decisions:
                    data=self._stage_confirmations();data['subtitle_decisions']=decisions
                    atomic_json(self.work/'stage-confirmations.json',data)
        decisions=decisions or {}
        item_by_id={i['sentence_id']:i for i in review['items']}
        for sid,dec in decisions.items():
            item=item_by_id.get(sid)
            if item is None:continue
            recorded=dec.get('audio_sha256_at_decision')
            binding=item.get('audio_sha256') or review['audio']['audio_sha256']
            if recorded is not None and recorded!=binding:continue  # decision bound to different audio
            try: apply_decision(review,sid,dec)
            except ValueError:continue
        atomic_json(self.work/'subtitle-review.json',review)
        unresolved=unresolved_items(review,stop_on_speech_error=True)
        if REDUB in unresolved:raise ValueError('SPEECH_ERROR_REDUB_REQUIRED:'+json.dumps([u for u in unresolved if u!=REDUB]))
        if unresolved:raise ValueError('SUBTITLE_REVIEW_REQUIRED:'+json.dumps(unresolved))
        # Effective sentence layout in the CONFIRMED audio's own coordinates: every
        # sentence keeps its original window, inter-sentence pauses stay as real
        # timeline silence, and the final sentence carries the audio's FULL tail.
        # Nothing cuts or compresses confirmed narration by default; trailing
        # silence is trimmed ONLY when the operator explicitly chose tail_trim in
        # the immutable job payload (recorded there and in the quality holds).
        windowed=[i for i in review['items'] if i.get('window')]
        if not windowed:raise ValueError('NARRATION_HAS_NO_ALIGNED_SENTENCES')
        audio_total=narration_plan['source']['actual_duration_ticks']
        last_sid=windowed[-1]['sentence_id']
        tail_trim=self.job['v2_overrides'].get('tail_trim')
        if tail_trim:
            self.holds.append('已按操作者显式选择裁剪尾部静音：超过 %s ticks（理由：%s）'
                              %(tail_trim.get('max_silence_ticks'),tail_trim.get('reason','')))
        effective=[];delta=0
        for item in windowed:
            sid=item['sentence_id'];w=item['window']
            claims=item.get('claim_ids') or next((s['claim_ids'] for s in script_plan['sentences'] if s['sentence_id']==sid),[])
            if sid in redub_audio:
                # timeline position from the ORIGINAL narration window; the redub
                # audio's own length replaces the spoken length and shifts the rest
                pos=redub_orig_window.get(sid,{'in_tick':w['in_tick'],'out_tick':w['out_tick']})
                dur=redub_audio[sid]['duration_ticks']
                effective.append({'sentence_id':sid,'in_tick':pos['in_tick']+delta,'out_tick':pos['in_tick']+delta+dur,
                                  'audio_in_tick':0,'audio_out_tick':dur,'redub':True,'claim_ids':claims})
                delta+=dur-(pos['out_tick']-pos['in_tick'])
            else:
                if sid==last_sid:
                    # full confirmed tail; explicit operator trim is the only exception
                    out=audio_total
                    if tail_trim and tail_trim.get('max_silence_ticks') is not None:
                        out=min(audio_total,w['out_tick']+int(tail_trim['max_silence_ticks']))
                else:
                    out=w['out_tick']
                effective.append({'sentence_id':sid,'in_tick':w['in_tick']+delta,'out_tick':max(w['in_tick']+delta+480,out+delta),
                                  'audio_in_tick':w['in_tick'],'audio_out_tick':out,'redub':False,'claim_ids':claims})
        narr_layout=dict(narration_plan,sentences=[dict(r,status='aligned',alignment_evidence_sha256=digest(alignment)) for r in effective],
                         source=dict(narration_plan['source'],actual_duration_ticks=max(audio_total, effective[-1]['out_tick'])))
        # Stage E: plan shots from the whole library; report gaps explicitly.
        # Operator-confirmed action windows (r3) ride the immutable job payload;
        # hash-reusable reviewed intervals live in the confirmed-interval store.
        # Inferred observation runs are PROPOSALS only and never auto-select.
        from scheduler.shot_semantics import load_confirmed_intervals
        reusable=load_confirmed_intervals(ROOT)
        confirmed_windows=self.job['v2_overrides'].get('confirmed_windows') or None
        constraints={'grid_ticks':1920,'max_tail_pause_ticks':19200,'min_clip_ticks':1920,'fixed_assets':fixed_constraints}
        # whole-clip default flow (USER_SCOPE_WHOLE_CLIP_SELECTION): whole source
        # files selected/merged by coarse relevance; precise mode keeps the r3 gates
        selection_mode=(self.job['intent'].get('selection_mode') or 'precise')
        if selection_mode=='beat':
            if not materials:raise ValueError('BEAT_MODE_REQUIRES_MATERIAL_CARDS')
            hold=round((self.profile['voice'].get('tail_hold_ms') or 0)*48)
            decision=plan_beats(script_plan,narr_layout,materials,dict(constraints,library_sha256=library['library_sha256'],tail_hold_ticks=hold))
        elif selection_mode=='whole_clip':
            if self.job['v2_overrides'].get('clip_replacements'):
                raise ValueError('WHOLE_CLIP_MODE_USES_WHOLE_CLIP_REPLACEMENTS')
            decision=plan_whole_clips(script_plan,narr_layout,library,constraints,
                                      whole_clip_replacements=self.job['v2_overrides'].get('whole_clip_replacements') or None)
        else:
            decision=plan_shots(script_plan,narr_layout,library,constraints,
                                confirmed_windows=confirmed_windows,reusable_intervals=reusable)
            if self.job['v2_overrides']['clip_replacements']:
                decision=apply_replacements(decision,library,self.job['v2_overrides']['clip_replacements'],
                                            script_plan=script_plan,confirmed=confirmed_windows,
                                            reusable_intervals=reusable)
        from scheduler.clef_selector import select_with_clef
        from scheduler.clef_review import build_system
        reviewer=None
        if self.job['providers']['text']=='claude-cli':
            reviewer=lambda system,user:self.chat('clef-review:'+canonical_hash({'system':system,'user':user}),system,user,max_tokens=2048)[1]
        decision,clef_audit,self.clef_decision_id=select_with_clef(self.store,self.job,script_plan,narr_layout,
            library,snapshot,decision,dict(constraints,reusable_intervals=reusable,
                sources={aid:{'sha256':src['sha256'],'duration_ticks':src['probe']['duration_ticks']} for aid,src in sources.items()}),
            reviewer=reviewer,review_system=build_system(self.profile))
        atomic_json(self.work/'clef-audit.json',clef_audit)
        # Human scoring table is the authority for free B-roll picks. Clef audit is
        # advisory evidence only; edit-decision is not written until the operator submits.
        if decision['missing']:
            raise ValueError('SHOT_COVERAGE_MISSING:'+json.dumps([{'sentence_id':m['sentence_id'],'requirement':m['requirement']} for m in decision['missing']]))
        from scheduler.human_score import ensure_pending_table, require_submitted_table, apply_to_decision, HOLD_CODE
        atomic_json(self.work/'shot-library.json', library)
        if not (self.work/'clef-candidates.json').is_file():
            atomic_json(self.work/'clef-candidates.json', snapshot)
        ensure_pending_table(self.id, self.rev, self.work, decision, clef_audit, library, snapshot, script_plan)
        try:
            human_table = require_submitted_table(self.work)
        except ValueError as exc:
            raise ValueError(HOLD_CODE + ':' + str(exc).split(':', 1)[-1]) from exc
        decision = apply_to_decision(decision, human_table, library, script_plan,
                                     confirmed_windows=confirmed_windows, reusable_intervals=reusable)
        atomic_json(self.work/'edit-decision.json', decision)
        if decision['missing']:
            raise ValueError('SHOT_COVERAGE_MISSING:'+json.dumps([{'sentence_id':m['sentence_id'],'requirement':m['requirement']} for m in decision['missing']]))
        # Stage F: compile timeline/v1, build the real voice stem, recompile, render
        nprobe=probe(npath)
        resources={'sources':sources,
            'narration':{'path':str(npath),'sha256':narration_plan['source']['audio_sha256'],
                         'probe':{'path':str(self.work/'narration-audio-probe.json'),'sha256':digest(self.work/'narration-audio-probe.json'),'stream_indices':nprobe['stream_indices'],'duration_ticks':nprobe['duration_ticks']}},
            'alignment':{'path':str(alignment),'sha256':digest(alignment)},
            'sentence_redubs':redub_audio,'subtitle_template':self.job['templates']['subtitle'],'cues':[]}
        draft=compile_edit_decision(decision,dict(resources,voice_stem={'path':'unused','sha256':'0'*64,'probe':{'path':'unused','sha256':'0'*64,'stream_indices':{'audio':0},'duration_ticks':1}}))
        skeleton={'resources':[r for r in draft['resources'] if not r['ref_id'].startswith('voice-stem')],
                  'audio_events':draft['audio_events'],'duration_ticks':draft['duration_ticks']}
        voice,cached=build_voice(skeleton,self.work);self.metrics['voice']={'cached':cached,'sha256':digest(voice)}
        atomic_json(self.work/'voice-stem-probe.json',probe(voice))
        final_t=compile_edit_decision(decision,dict(resources,voice_stem={'path':str(voice),'sha256':digest(voice),'probe':{'path':str(self.work/'voice-stem-probe.json'),'sha256':digest(self.work/'voice-stem-probe.json'),'stream_indices':{'audio':0},'duration_ticks':draft['duration_ticks']}}))
        final_t['subtitles']['cues']=self._redub_cues(review,effective,redubs,decision['narration']['body_start_tick'])
        final_t['subtitles']['template']=self.job['templates']['subtitle']
        return self._finish(final_t)

    def _redub_cues(self,review,effective,redubs,body_start=0):
        """Rebuild cues with real word timing on TIMELINE coordinates: cue ticks come
        from each item's own aligned words (its own audio), then shift by the body
        offset (fixed opening length) plus any redub-driven shift of that sentence."""
        from scheduler.subtitle_review import build_cues
        out=[]
        for row in effective:
            item=next((i for i in review['items'] if i['sentence_id']==row['sentence_id']),None)
            if item is None:continue
            offset=body_start+row['in_tick']-row['audio_in_tick']
            if row['redub']:
                local=dict(item,window={'in_tick':0,'out_tick':row['out_tick']-row['in_tick']},script_text=redubs[row['sentence_id']]['text'])
            else:
                local=dict(item,window={'in_tick':row['audio_in_tick'],'out_tick':row['audio_out_tick']})
            for c in build_cues({'items':[local]}):
                out.append({'cue_id':'cue-%d'%(len(out)+1),'start_tick':offset+c['start_tick'],
                            'end_tick':offset+c['end_tick'],
                            'text':c['text'],'claim_ids':c['claim_ids']})
        return sorted(out,key=lambda c:c['start_tick'])
