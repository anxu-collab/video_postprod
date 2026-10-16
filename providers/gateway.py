import copy,datetime,json,math,secrets,threading,urllib.request
from pathlib import Path
from providers.ark_text_agent import ROUTE as ARK_TEXT_AGENT_ROUTE,load_config as ark_text_config,tier_for as ark_text_tier_for
from providers.claude_cli import ClaudeCli,MODELS as CLAUDE_MODELS,OPUS,SONNET
from providers.ledger import Ledger
from scheduler.contracts import canonical_hash
from workers.common import ROOT,atomic_json

CLAUDE_TEXT_ROUTE='claude-cli'

class Gateway:
    model='doubao-seed-2-0-lite-260428'
    # Official 2026-10-11 regular highest tier is 1.8/10.8 CNY per million;
    # reserve using twice that (low-latency highest tier), no cache discounts.
    max_input_tokens=224000;input_rate=3.6;output_rate=21.6
    def __init__(self,store,transport=None,claude=None):
        self.store=store
        caps=ark_text_config().get('global_authorization',{})
        self.ledger=Ledger(store,global_limit_micros=int(caps.get('total_micros',2000000)),max_requests=int(caps.get('requests',40)))
        self.transport=transport or self._network
        self.claude=claude or ClaudeCli()
        self.contexts={};self.lock=threading.RLock()
    def route_model(self,job_id,node_key,revision=1):
        """Vision stays on Ark. With the claude-cli text route, script generation uses
        Opus and every *-review node (fact checks, Clef text review) uses Sonnet.
        With the ark-text-agent route, script-* uses the script tier and every
        *-review node uses the review tier (see config/ark-text-agent.json)."""
        kind=node_key.split(':',1)[0]
        if kind.startswith('vision-'):return self.model
        job=self.store.get_job(job_id,revision)
        text=((job or {}).get('payload') or {}).get('providers',{}).get('text')
        if text==CLAUDE_TEXT_ROUTE:
            if kind.endswith('-review'):return SONNET
            if kind.startswith('script-'):return OPUS
        if text==ARK_TEXT_AGENT_ROUTE:
            tiers=ark_text_config()
            # reviews win over script-*: script-review and clef-review are review nodes
            if kind.endswith('-review'):return tiers['review']['model']
            if kind.startswith('script-'):return tiers['script']['model']
        if kind=='clef-review':raise ValueError('CLEF_TEXT_REVIEW_ROUTE_UNAVAILABLE')
        return self.model
    def issue(self,job_id,node_key,revision=1,model=None):
        token=secrets.token_urlsafe(32)
        self.contexts[token]={'job_id':job_id,'revision':revision,'node_key':node_key,'model':model or self.model,'expires':datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=1)}
        return token
    def request(self,token,payload):
        with self.lock:
            context=self.contexts.get(token)
            if not context or context['expires']<datetime.datetime.now(datetime.timezone.utc):raise ValueError('OPERATION_TOKEN_INVALID')
            if payload.get('model')!=context.get('model',self.model):raise ValueError('UNAPPROVED_MODEL')
            allowed={'model','messages','max_tokens','temperature','top_p','response_format','stream','thinking'}
            if set(payload)-allowed or payload.get('stream'):raise ValueError('UNSUPPORTED_CLOUD_PARAMETERS')
            if payload['model'] in CLAUDE_MODELS:return self._claude(context,payload)
            tier=ark_text_tier_for(payload['model'])
            p=copy.deepcopy(payload)
            if tier is None:
                # legacy vision route keeps its exact behaviour
                p['max_tokens']=min(int(p.get('max_tokens',2048)),2048);p['thinking']={'type':'enabled'}
                max_in,reserve_in,in_rate,out_rate,headroom=self.max_input_tokens,self.max_input_tokens,self.input_rate,self.output_rate,0
            else:
                p['max_tokens']=min(int(p.get('max_tokens',tier['max_output_tokens'])),tier['max_output_tokens'])
                p['thinking']={'type':'enabled' if tier['thinking'] else 'disabled'}
                max_in=tier['max_input_tokens'];reserve_in=tier['reserve_input_tokens']
                in_rate=tier['input_rate'];out_rate=tier['output_rate']
                # Volcengine thinking models count reasoning tokens in completion_tokens and
                # may exceed the requested max_tokens; headroom covers that legitimate overrun.
                headroom=tier['reasoning_headroom'] if tier['thinking'] else 0
            if p['max_tokens']<=0:raise ValueError('INVALID_TOKEN_CAP')
            images=0
            for message in p.get('messages',[]):
                content=message.get('content','')
                if isinstance(content,list):
                    for part in content:
                        if part.get('type')=='image_url':
                            url=part.get('image_url',{}).get('url','')
                            if not url.startswith(('data:image/jpeg;base64,','data:image/png;base64,')):raise ValueError('REMOTE_IMAGE_URL_FORBIDDEN')
                            if len(url)>8*1024*1024:raise ValueError('IMAGE_PAYLOAD_CAP')
                            images+=1
                        elif part.get('type')!='text':raise ValueError('AUDIO_VIDEO_TOOL_REQUEST_FORBIDDEN')
                elif not isinstance(content,str):raise ValueError('INVALID_MESSAGES')
            if images>4 or not p.get('messages') or len(json.dumps(p))>34*1024*1024:raise ValueError('INPUT_SIZE_CAP')
            key=canonical_hash({'model':payload['model'],'node_key':context['node_key'],'payload':p,'account_ref':'approved-project-env','policy':'ark-budget-v1'})
            reserve=math.ceil(reserve_in*in_rate+(p['max_tokens']+headroom)*out_rate)
            job=self.store.get_job(context['job_id'],context['revision'])
            caps=job['payload']['cost'] if job else {'paid_enabled':context['job_id']=='deployment-qualification','job_limit_micros':2000000,'daily_limit_micros':2000000,'max_paid_requests':40}
            if not caps['paid_enabled']:raise ValueError('PAID_DISABLED')
            intent=self.ledger.reserve(key,context['job_id'],reserve,job_limit_micros=caps['job_limit_micros'],daily_limit_micros=caps['daily_limit_micros'],max_paid_requests=caps['max_paid_requests'])
            if intent['state']=='SUCCEEDED':return intent['result']
            try:
                result=self.transport(p)
                usage=result.get('usage',{});inp=usage.get('prompt_tokens');out=usage.get('completion_tokens')
                if not isinstance(inp,int) or not isinstance(out,int) or not 0<=inp<=max_in or not 0<=out<=p['max_tokens']+headroom:raise ValueError('USAGE_BOUND_UNVERIFIED')
                cost=math.ceil(inp*in_rate+out*out_rate)
                self.ledger.complete(intent['intent_id'],result,cost)
                return result
            except BaseException as e:
                self.ledger.unknown(intent['intent_id'],type(e).__name__)
                raise
    def _claude(self,context,payload):
        job=self.store.get_job(context['job_id'],context['revision'])
        if not (job and job['payload']['cost']['paid_enabled']):raise ValueError('PAID_DISABLED')
        return self.claude.complete(payload['model'],payload,job_id=context['job_id'],node_key=context['node_key'])
    def _network(self,payload):
        values={}
        for line in (ROOT.parent/'.env').read_text(encoding='utf-8-sig').splitlines():
            name,sep,value=line.partition('=')
            if sep and name.strip()=='ARK_API_KEY':values[name.strip()]=value.strip().strip('\"').strip("'")
        key=values.get('ARK_API_KEY') or values.get('VOLCENGINE_API_KEY')
        if not key:raise ValueError('ARK_SECRET_MISSING')
        req=urllib.request.Request('https://ark.cn-beijing.volces.com/api/v3/chat/completions',data=json.dumps(payload,ensure_ascii=False).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+key},method='POST')
        # Exactly one network request. No SDK retries, redirects, or task resubmission.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=120) as response:return json.load(response)
