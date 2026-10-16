"""Local Claude Code CLI bridge for text-only routes (script, fact check, shot text review).

Every call is a fresh, tool-less, non-persistent `claude -p` session. Results are
cached by request key so a job re-run after each operator gate replays the exact
same text instead of regenerating it. The process runs in an empty directory
outside this repo: a cwd inside the repo makes the CLI load parent project
instructions. `--bare` is not used because it drops subscription login."""
import json,re,shutil,subprocess,tempfile,threading,time,uuid
from pathlib import Path
from scheduler.contracts import canonical_hash
from workers.common import ROOT,atomic_json

OPUS='claude-opus-5-5'
SONNET='claude-sonnet-5-5'
MODELS=(OPUS,SONNET)
OPUS_EFFORT='medium'
STATE=ROOT/'state/claude-bridge'
MAX_REQUESTS_PER_JOB=40


class ClaudeCliError(RuntimeError):pass


def _messages(payload):
    system,user=[],[]
    for message in payload.get('messages') or []:
        content=message.get('content','')
        if isinstance(content,list):
            if any(part.get('type')!='text' for part in content):raise ValueError('CLAUDE_CLI_TEXT_ONLY')
            content='\n'.join(part.get('text','') for part in content)
        if not isinstance(content,str):raise ValueError('INVALID_MESSAGES')
        role=message.get('role')
        if role=='system':system.append(content)
        elif role in ('user','assistant'):user.append(content)
        else:raise ValueError('INVALID_MESSAGES')
    if not user:raise ValueError('INVALID_MESSAGES')
    return '\n\n'.join(system),'\n\n'.join(user)


def cli_args(exe,model,system):
    """Text-only session: no tools, no project instructions, Opus at medium effort."""
    args=[exe,'-p','--model',model,'--output-format','json','--tools','','--safe-mode','--no-session-persistence','--strict-mcp-config']
    if model==OPUS:args+=['--effort',OPUS_EFFORT]
    if system:args+=['--system-prompt',system]
    return args


def _run_cli(model,system,prompt,timeout,state=STATE):
    exe=shutil.which('claude')
    if not exe:raise ClaudeCliError('CLAUDE_CLI_NOT_FOUND')
    # A .cmd/.ps1 shim would re-parse the system prompt through a shell.
    if Path(exe).suffix.lower()!='.exe':raise ClaudeCliError('CLAUDE_CLI_NOT_NATIVE_EXE:'+exe)
    args=cli_args(exe,model,system)
    # state is the on-repo cache. The CLI cwd must stay outside it and outside the repo.
    with tempfile.TemporaryDirectory(prefix='claude-bridge-') as cwd:
        isolated=Path(cwd).resolve()
        repo,cache=ROOT.resolve(),Path(state).resolve()
        if repo==isolated or repo in isolated.parents or cache==isolated or cache in isolated.parents:
            raise ClaudeCliError('CLAUDE_CWD_NOT_ISOLATED')
        done=subprocess.run(args,input=prompt,cwd=cwd,capture_output=True,text=True,encoding='utf-8',errors='replace',
                            timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    out=done.stdout.strip()
    try:data=json.loads(out.splitlines()[-1]) if out else None
    except ValueError:data=None
    if not isinstance(data,dict):raise ClaudeCliError('CLAUDE_CLI_NO_JSON:exit=%s:%s'%(done.returncode,done.stderr.strip()[-200:]))
    return data


class ClaudeCli:
    def __init__(self,runner=None,timeout=600,state=None):
        self.state=Path(state) if state else STATE
        self.runner=runner or (lambda model,system,prompt,timeout:_run_cli(model,system,prompt,timeout,self.state))
        self.timeout=timeout;self.lock=threading.RLock()

    def _cache_path(self,key):return self.state/'cache'/key[:2]/(key+'.json')

    def _job_file(self,job_id):return self.state/'jobs'/(re.sub(r'[^A-Za-z0-9_.-]','_',job_id)+'.json')

    def _job_count(self,job_id):
        p=self._job_file(job_id)
        return len(json.loads(p.read_text(encoding='utf-8'))) if p.is_file() else 0

    def _record(self,job_id,receipt):
        p=self._job_file(job_id);rows=json.loads(p.read_text(encoding='utf-8')) if p.is_file() else []
        rows.append(receipt);atomic_json(p,rows)

    def complete(self,model,payload,*,job_id,node_key):
        if model not in MODELS:raise ValueError('UNAPPROVED_MODEL')
        system,prompt=_messages(payload)
        key=canonical_hash({'model':model,'node_key':node_key,'system':system,'prompt':prompt,'policy':'claude-cli-v1'})
        with self.lock:
            cached=self._cache_path(key)
            if cached.is_file():return json.loads(cached.read_text(encoding='utf-8'))
            if self._job_count(job_id)>=MAX_REQUESTS_PER_JOB:raise ValueError('CLAUDE_REQUEST_CAP')
            started=time.monotonic()
            data=self.runner(model,system,prompt,self.timeout)
            if data.get('is_error') or data.get('subtype')!='success':
                raise ClaudeCliError('CLAUDE_CLI_FAILED:'+str(data.get('api_error_status') or data.get('subtype')))
            used=list((data.get('modelUsage') or {}).keys())
            if used!=[model]:raise ClaudeCliError('CLAUDE_MODEL_MISMATCH:'+','.join(used))
            text=data.get('result')
            if not isinstance(text,str) or not text.strip():raise ClaudeCliError('CLAUDE_CLI_EMPTY_RESULT')
            usage=data.get('usage') or {}
            prompt_tokens=sum(int(usage.get(k) or 0) for k in ('input_tokens','cache_creation_input_tokens','cache_read_input_tokens'))
            completion_tokens=int(usage.get('output_tokens') or 0)
            result={'id':data.get('session_id') or uuid.uuid4().hex,'object':'chat.completion','created':int(time.time()),'model':model,
                    'choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':prompt_tokens,'completion_tokens':completion_tokens,'total_tokens':prompt_tokens+completion_tokens}}
            atomic_json(cached,result)
            self._record(job_id,{'request_key':key,'node_key':node_key,'model':model,'session_id':data.get('session_id'),
                                 'cost_usd':data.get('total_cost_usd'),'duration_s':round(time.monotonic()-started,3),
                                 'usage':result['usage'],'utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())})
            return result
