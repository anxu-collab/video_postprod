"""Text-only, bounded Clef choice decisions. Scores remain batch-local evidence."""
import contextlib
import copy
import datetime
import functools
import hashlib
import importlib.util
import json
import math
import re
import subprocess
import time
import urllib.request
from pathlib import Path

from scheduler.contracts import canonical_hash

RULE_VERSION = 'clef-choice/v2'
CONTROL_PATH = Path('F:/ai_services/clef_flash/scripts/on_demand.py')
MODEL_PATH = Path('F:/models/clef-flash/Clef-Flash-Q4_K_M.gguf')


def _request_utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='microseconds')


@functools.lru_cache(maxsize=4)
def _model_digest(path,size,mtime_ns):
    if size is None:return None
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(8*1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def safe(value):
    """Sidecars and in-memory batches share the repository's secret boundary."""
    if isinstance(value, dict):
        return {k: '[REDACTED]' if re.search(r'token|authorization|api.?key|password|secret',str(k),re.I)
                else safe(v) for k,v in value.items()}
    if isinstance(value, (list,tuple)): return [safe(v) for v in value]
    if isinstance(value,float) and not math.isfinite(value): return str(value)
    if isinstance(value,str):
        return re.sub(r'(?i)(bearer\s+|(?:token|password|secret|api[_-]?key)\s*[=:]\s*)[^\s,;"}]+',r'\1[REDACTED]',value)
    return value


class ClefDecisionAdapter:
    def __init__(self, config=None, transport=None):
        self.config=dict(config or {});self.transport=transport
        self.backend=self.config.get('backend','local')
        if self.backend not in ('local','mock'): raise ValueError('CLEF_BACKEND_INVALID')
        self.timeout=float(self.config.get('timeout_sec',20))
        self.max_requests=int(self.config.get('max_requests',64))
        self.max_retries=int(self.config.get('max_retries',0))
        self.batch_size=int(self.config.get('batch_size',8))
        if not 0 < self.timeout <= 120 or not 1 <= self.max_requests <= 512 or not 0 <= self.max_retries <= 2 or not 2 <= self.batch_size <= 32:
            raise ValueError('CLEF_BUDGET_INVALID')
        self.model_version=('MOCK-clef-choice/v1' if self.backend=='mock' else
                            'INJECTED-clef-test/v1' if transport is not None else 'Clef-Flash-Q4_K_M')
        stat=MODEL_PATH.stat() if MODEL_PATH.is_file() else None
        self.model_sha256=None
        self.fingerprint=canonical_hash({'path':str(MODEL_PATH),'content_sha256':self.model_sha256,'size':stat.st_size if stat else None,
                                        'mtime_ns':stat.st_mtime_ns if stat else None,
                                        'backend':self.backend,'injected_transport':transport is not None})
        self.last_lifecycle = {}

    @staticmethod
    def _gpu_precheck():
        result=subprocess.run(['nvidia-smi','--query-gpu=memory.free','--format=csv,noheader,nounits'],
                              capture_output=True,text=True,timeout=5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode: raise RuntimeError('CLEF_GPU_STATUS_UNAVAILABLE')
        free=[int(line.strip()) for line in result.stdout.splitlines() if line.strip()]
        # Existing controller uses GPU 0. We never stop somebody else's workloads.
        needed=(MODEL_PATH.stat().st_size//(1024*1024))+1024 if MODEL_PATH.is_file() else 7500
        if not free or free[0]<needed: raise RuntimeError('CLEF_GPU_MEMORY_INSUFFICIENT')

    @contextlib.contextmanager
    def _session(self):
        if self.transport is not None or self.backend=='mock':
            yield self.transport or self._mock;return
        self._gpu_precheck()
        stat=MODEL_PATH.stat()
        self.model_sha256=_model_digest(str(MODEL_PATH),stat.st_size,stat.st_mtime_ns)
        # Cache identity is the same before and after session entry. Content digest
        # remains audit metadata; model path/size/mtime and versions key the cache.
        spec=importlib.util.spec_from_file_location('clef_installed_on_demand',CONTROL_PATH)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        # One mutex/session per bounded decision; start/stop are the installed
        # controller's responsibility, including every inference/validation error.
        with module.session():
            cfg={}
            for line in (CONTROL_PATH.parent.parent/'config/clef.env').read_text(encoding='utf-8').splitlines():
                if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                    k,v=line.split('=',1);cfg[k.strip()]=v.strip()
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),module.NoRedirect())
            def send(payload):
                req=urllib.request.Request('http://127.0.0.1:%s/v1/systemone'%cfg['ADAPTER_PORT'],
                    data=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode(),headers={'Content-Type':'application/json'})
                with opener.open(req,timeout=self.timeout) as response:return json.loads(response.read().decode('utf-8'))
            yield send

    @contextlib.contextmanager
    def _lazy_session(self):
        """Acquire the installed lifecycle only on the first uncached inference."""
        audit = {'session_started': False, 'session_exit_observed': False,
                 'session_error': None, 'started_utc': None, 'finished_utc': None,
                 'scope': 'controller context exit; not an independent GPU/process measurement'}
        self.last_lifecycle = audit
        stack = contextlib.ExitStack()
        sender = None
        def send(payload):
            nonlocal sender
            if sender is None:
                audit['started_utc'] = _request_utc()
                try:
                    sender = stack.enter_context(self._session())
                    audit['session_started'] = True
                except Exception as exc:
                    audit['session_error'] = safe(type(exc).__name__ + ':' + str(exc))
                    raise
            return sender(payload)
        try:
            yield send
        except BaseException as exc:
            if not isinstance(exc, GeneratorExit):
                audit['session_error'] = safe(type(exc).__name__ + ':' + str(exc))
            raise
        finally:
            try:
                stack.close()
                audit['session_exit_observed'] = audit['session_started']
            except BaseException as exc:
                audit['session_error'] = safe(type(exc).__name__ + ':' + str(exc))
                raise
            finally:
                audit['finished_utc'] = _request_utc()

    @staticmethod
    def _mock(payload):
        if 'selection' not in payload['questions']:
            return ClefDecisionAdapter._structure_mock(payload)
        options=payload['questions']['selection']['criteria']
        return {'model':'MOCK-clef-choice/v1','choice':next((k for k in options if k not in ('NONE','NEEDS_REVIEW')),'NONE')}

    @staticmethod
    def _structure_mock(payload):
        """Offline stand-in: the first option of each question is the one that lets a
        later stage run. Real calls return the model's own probabilities."""
        answers={}
        for qid,question in payload['questions'].items():
            keys=list(question['criteria'])
            rest=0.2/(len(keys)-1) if len(keys)>1 else 0
            scores={key:(0.8 if index==0 else rest) for index,key in enumerate(keys)}
            answers[qid]={'type':question['type'],'probabilities':scores,'scores':scores}
        return {'model':'MOCK-clef-choice/v1','answers':answers}

    @staticmethod
    def _payload(sentence,pool,phase):
        compact=[]
        for c in pool:
            native=c.get('native') or {}
            description=c.get('description') or native.get('product_description') or native.get('action_text') or ''
            tags=c.get('tags') or []
            actions=c.get('actions') or []
            compact.append({'candidate_id':c['candidate_id'],'source_sha256':c.get('source_sha256'),
                'in_tick':c.get('in_tick'),'out_tick':c.get('out_tick'),
                'description':str(description)[:500], 'scene':str(c.get('scene') or '')[:100],
                'tags':[str(t)[:60] for t in tags[:6]],
                'actions':[json.dumps(a,ensure_ascii=False)[:100] for a in actions[:4]],
                'audio_policy':c.get('audio_policy'),'gate_reasons':c.get('gate_reasons') or [],
                'metadata_sha256':canonical_hash({'description':description,'tags':tags,'actions':actions,
                    'scene':c.get('scene'),'observations':c.get('observations') or []}),
                'metadata_truncated':len(str(description))>500 or len(tags)>6 or len(actions)>4 or bool(native)})
        s={k:sentence.get(k) for k in ('text','in_tick','out_tick')}
        s['visual_intent']=json.dumps(sentence.get('visual_intent') or {},ensure_ascii=False)[:300]
        return {'state':{'sentence':s,'candidates':compact,'phase':phase,
                         'rules':'Select the strongest factual visual match. NONE if all unsuitable. Text metadata only; confidence is not accuracy.'},
                'questions':{'selection':{'type':'choice','instructions':'Which visual best matches this narration? Choose NEEDS_REVIEW when evidence is uncertain.',
                    'criteria':dict([(c['candidate_id'],c['description'][:160] or c['candidate_id']) for c in compact]+
                                    [('NONE','No suitable candidate'),('NEEDS_REVIEW','Uncertain; human review required')])}}}

    @staticmethod
    def _fits(payload):
        # Conservative bound for the installed 8192-token context, including
        # Chinese tokenization and the backend's serialization overhead.
        return len(json.dumps(payload,ensure_ascii=False))<=4500

    def decide(self,sentences,candidates,repository=None):
        result={'model_version':self.model_version,'model_fingerprint':self.fingerprint,'rule_version':RULE_VERSION,
                'model_sha256':self.model_sha256,
                'confidence_interpretation':'raw model output; not calibrated accuracy',
                'sentences':[],'batches':[],'fallback_reason':None,
                'transport_kind':'injected-test' if self.transport else self.backend}
        for sentence in sentences:
            result['sentences'].append({'sentence_id':sentence['sentence_id'],'status':'NEEDS_REVIEW',
                'ranked_ids':[],'preferred_ids':[],'alternatives':[],
                'candidate_results':[{'candidate_id':c['candidate_id'],'selection_eligible':bool(c.get('selection_eligible')),
                    'gate_reasons':c.get('gate_reasons') or [],'reason':None,'evaluations':[],
                    'status':'NOT_EVALUATED' if c.get('selection_eligible') else 'GATE_REFUSED'} for c in candidates]})
        count=0
        def ask(send,sentence,pool,phase,row):
            nonlocal count
            payload=self._payload(sentence,pool,phase)
            key=canonical_hash({'model_fingerprint':self.fingerprint,'model_version':self.model_version,'rule_version':RULE_VERSION,'request':payload})
            entry={'batch_id':len(result['batches'])+1,'sentence_id':sentence['sentence_id'],'phase':phase,
                   'request':safe(payload),'response':None,'error':None,'elapsed_ms':0,'cached':False,'cache_key':key,'attempts':[],
                   'request_started_utc':_request_utc(),'request_finished_utc':None,
                   'time_scope':'request_operation_including_lookup_and_validation'}
            started=time.monotonic()
            try:
                if not self._fits(payload):raise RuntimeError('CLEF_CONTEXT_NEEDS_REVIEW')
                cached=repository.cache_get(key) if repository else None
                if cached is not None:
                    response=copy.deepcopy(cached);entry['cached']=True;entry['time_scope']='cache_lookup_and_replay'
                else:
                    for retry in range(self.max_retries+1):
                        if count>=self.max_requests:raise RuntimeError('CLEF_REQUEST_BUDGET_EXHAUSTED')
                        count+=1
                        attempt={'request_number':count,'response':None,'error':None,
                                 'request_started_utc':_request_utc(),'request_finished_utc':None};attempt_start=time.monotonic()
                        entry['attempts'].append(attempt)
                        try:
                            response=send(payload);attempt['response']=safe(response);break
                        except Exception as exc:
                            attempt['error']=safe(type(exc).__name__+':'+str(exc))
                            if retry==self.max_retries:raise
                        finally:
                            attempt['elapsed_ms']=round((time.monotonic()-attempt_start)*1000,3)
                            attempt['request_finished_utc']=_request_utc()
                entry['response']=safe(response)
                if not isinstance(response,dict):raise ValueError('CLEF_NON_JSON_RESPONSE')
                answer=(response.get('answers') or {}).get('selection',response)
                if not isinstance(answer,dict) or answer.get('choice') not in payload['questions']['selection']['criteria']:
                    raise ValueError('CLEF_UNKNOWN_CHOICE')
                if repository and not entry['cached']:repository.cache_put(key,safe(response))
                choice=answer['choice']
                ids={c['candidate_id'] for c in pool}
                for item in row['candidate_results']:
                    if item['candidate_id'] in ids:
                        item['status']='EVALUATED'
                        item['evaluations'].append({'batch_id':entry['batch_id'],'choice':choice,
                            'score':(answer.get('scores') or {}).get(item['candidate_id']),
                            'confidence':answer.get('confidence'),'reason':answer.get('reason')})
                        if answer.get('reason') is not None:item['reason']=answer['reason']
                if choice=='NEEDS_REVIEW':raise RuntimeError('CLEF_MODEL_NEEDS_REVIEW')
                return choice
            except Exception as exc:
                entry['error']=safe(type(exc).__name__+':'+str(exc))
                if self.last_lifecycle.get('started_utc') and not self.last_lifecycle.get('session_started'):
                    entry['phase']='precheck'
                    entry['time_scope']='precheck_and_session_entry; no inference request sent'
                raise
            finally:
                entry['elapsed_ms']=round((time.monotonic()-started)*1000,3)
                entry['request_finished_utc']=_request_utc()
                result['batches'].append(entry)
        eligible=[c for c in candidates if c.get('selection_eligible')]
        precheck_started_utc=_request_utc();precheck_started=time.monotonic()
        try:
            # Empty/rejected pools need no GPU or controller session.
            with self._lazy_session() as send:
                result.update(model_sha256=self.model_sha256,model_fingerprint=self.fingerprint)
                for sentence,row in zip(sentences,result['sentences']):
                    if not eligible:row['status']='NONE';continue
                    groups=[]
                    batches=[];pending=[]
                    for c in eligible:
                        if pending and (len(pending)>=self.batch_size or not self._fits(self._payload(sentence,pending+[c],'batch'))):
                            batches.append(pending);pending=[]
                        pending.append(c)
                    if pending:batches.append(pending)
                    for batch in batches:
                        pending=list(batch);rank=[]
                        while pending:
                            winner=ask(send,sentence,pending,'batch',row)
                            if winner=='NONE':break
                            chosen=next(c for c in pending if c['candidate_id']==winner)
                            rank.append(chosen);pending=[c for c in pending if c['candidate_id']!=winner]
                        groups.append(rank)
                    # Merge batch-local rankings by COMMON CHOICE comparisons, never
                    # comparing unrelated probability scales. Entire order is bounded.
                    ranked=[]
                    while any(groups):
                        heads=[g[0] for g in groups if g]
                        if len(heads)==1:ranked.extend(c['candidate_id'] for g in groups for c in g);break
                        if len(heads)>self.batch_size:raise RuntimeError('CLEF_FINALIST_POOL_NEEDS_REVIEW')
                        winner=ask(send,sentence,heads,'finalists',row)
                        if winner=='NONE':raise RuntimeError('CLEF_FINALIST_CONFLICT_NEEDS_REVIEW')
                        ranked.append(winner)
                        for group in groups:
                            if group and group[0]['candidate_id']==winner:group.pop(0);break
                    row.update(status='OK' if ranked else 'NONE',ranked_ids=ranked,preferred_ids=ranked[:1],alternatives=ranked[1:])
        except Exception as exc:
            reason=safe(type(exc).__name__+':'+str(exc));result['fallback_reason']=reason
            status='NEEDS_REVIEW' if 'BUDGET' in reason or 'NEEDS_REVIEW' in reason else 'ERROR'
            for row in result['sentences']:
                # A bounded decision is atomic. A later request or session cleanup
                # failure invalidates earlier OK/NONE rows as production advice,
                # while their ranking and every raw response remain audit evidence.
                row['status_before_failure']=row['status']
                row['ranking_before_failure']=list(row['ranked_ids'])
                row.update(status=status,preferred_ids=[],ranked_ids=[],alternatives=[])
            if not result['batches']:
                result['batches'].append({'batch_id':1,'sentence_id':None,'phase':'precheck',
                    'request':safe({'state':{'sentences':sentences,'candidates':candidates}}),
                    'response':None,'error':reason,'elapsed_ms':round((time.monotonic()-precheck_started)*1000,3),'cached':False,
                    'request_started_utc':precheck_started_utc,'request_finished_utc':_request_utc(),
                    'time_scope':'precheck_and_session_entry; no inference request sent'})
        result['lifecycle'] = copy.deepcopy(self.last_lifecycle)
        result['model_sha256'] = self.model_sha256
        return result

    def classify(self,state,questions,repository=None):
        """One validated choice response; compatible with probability-only callers."""
        rows = list(self.classify_many([{'state': state, 'questions': questions}], repository))
        if rows[0]['error']:
            raise ValueError(rows[0]['error'])
        return rows[0]['response']

    @staticmethod
    def validate_classification(response, questions):
        if not isinstance(response, dict):
            raise ValueError('CLEF_NON_JSON_RESPONSE')
        # Nonfinite values can also hide in the native raw response.
        try:
            json.dumps(response, allow_nan=False)
        except (ValueError, TypeError):
            raise ValueError('CLEF_NONFINITE_OR_NON_JSON_RESPONSE')
        answers = response.get('answers')
        if answers is None and len(questions) == 1:
            answers = {next(iter(questions)): response}
        if not isinstance(answers, dict) or set(answers) != set(questions):
            raise ValueError('CLEF_ANSWERS_MISSING_OR_UNKNOWN')
        for qid, question in questions.items():
            answer = answers[qid]
            criteria = question.get('criteria')
            if question.get('type') != 'choice' or not isinstance(criteria, dict) or not criteria:
                raise ValueError('CLEF_CHOICE_QUESTION_INVALID')
            if not isinstance(answer, dict) or answer.get('type', 'choice') != 'choice':
                raise ValueError('CLEF_ANSWER_TYPE_INVALID')
            choice = answer.get('choice')
            if choice is not None and choice not in criteria:
                raise ValueError('CLEF_UNKNOWN_CHOICE')
            has_probabilities = False
            for field in ('probabilities', 'scores'):
                if field not in answer:
                    continue
                values = answer[field]
                if not isinstance(values, dict):
                    raise ValueError('CLEF_PROBABILITIES_INVALID')
                # Installed normalize_answer emits scores:{} for choice-only responses.
                if not values:
                    continue
                if set(values) != set(criteria):
                    raise ValueError('CLEF_PROBABILITY_OPTIONS_INVALID')
                if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
                       not math.isfinite(v) or not 0 <= v <= 1 for v in values.values()):
                    raise ValueError('CLEF_PROBABILITIES_INVALID')
                if not math.isclose(sum(values.values()), 1.0, abs_tol=1e-6):
                    raise ValueError('CLEF_PROBABILITIES_NOT_NORMALIZED')
                has_probabilities = True
            confidence = answer.get('confidence')
            if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or
                                           not math.isfinite(confidence) or not 0 <= confidence <= 1):
                raise ValueError('CLEF_CONFIDENCE_INVALID')
            if choice is None and not has_probabilities:
                raise ValueError('CLEF_CHOICE_MISSING')
        return response

    def classify_many(self, requests, repository=None):
        """Yield audited records for an iterable of {state, questions} payloads.

        Cached responses are revalidated before replay. A batch shares one lazy
        lifecycle, and the caller must exhaust or close this generator. Session
        exit failure raises (it cannot be represented as a successful last row).
        """
        count = 0
        pending_cache = {}
        try:
            with self._lazy_session() as send:
                for payload in requests:
                    started = time.monotonic()
                    row = {'request': safe(payload), 'response': None, 'error': None,
                           'cached': False, 'cache_key': None, 'attempts': [],
                           'request_started_utc': _request_utc()}
                    try:
                        if not self._fits(payload):
                            raise RuntimeError('CLEF_CONTEXT_NEEDS_REVIEW')
                        key = canonical_hash({'model_fingerprint': self.fingerprint, 'model_version': self.model_version,
                                              'rule_version': RULE_VERSION, 'request': payload, 'task': 'classify'})
                        row['cache_key'] = key
                        cached = pending_cache.get(key)
                        if cached is None and repository is not None:
                            cached = repository.cache_get(key)
                        if cached is not None:
                            response = copy.deepcopy(cached)
                            row['cached'] = True
                        else:
                            for retry in range(self.max_retries + 1):
                                if count >= self.max_requests:
                                    raise RuntimeError('CLEF_REQUEST_BUDGET_EXHAUSTED')
                                count += 1
                                attempt = {'request_number': count, 'request_started_utc': _request_utc(),
                                           'response': None, 'error': None}
                                row['attempts'].append(attempt)
                                try:
                                    response = send(payload)
                                    attempt['response'] = safe(response)
                                    break
                                except Exception as exc:
                                    attempt['error'] = safe(type(exc).__name__ + ':' + str(exc))
                                    if retry == self.max_retries:
                                        raise
                                finally:
                                    attempt['request_finished_utc'] = _request_utc()
                        row['response'] = safe(response)
                        self.validate_classification(response, payload['questions'])
                        if repository is not None and not row['cached']:
                            pending_cache[key] = safe(response)
                    except Exception as exc:
                        row['error'] = safe(type(exc).__name__ + ':' + str(exc))
                    finally:
                        row['request_finished_utc'] = _request_utc()
                        row['elapsed_ms'] = round((time.monotonic() - started) * 1000, 3)
                    yield row
        finally:
            # Publish only after the lifecycle exits successfully, including an
            # explicit generator close when the caller stops at its budget.
            if repository is not None and not self.last_lifecycle.get('session_error'):
                for key, response in pending_cache.items():
                    repository.cache_put(key, response)
