param([switch]$Json)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root 'envs/scheduler/Scripts/python.exe'
$source = @'
import hashlib,json,os,pathlib,subprocess,sys,socket
root=pathlib.Path(sys.argv[1])
def run(args,cwd=None,timeout=60):
    p=subprocess.run([str(x) for x in args],cwd=cwd,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
    return {'exit_code':p.returncode,'stdout':p.stdout.strip(),'stderr':p.stderr[-2000:]}
def sha(p):
    h=hashlib.sha256()
    with open(p,'rb') as f:
        for chunk in iter(lambda:f.read(1048576),b''):h.update(chunk)
    return h.hexdigest()
r={'schema_version':'postprod-doctor/v1','environment_ready':True,'environments':{},'upstreams':{},'models':{},'locks':{}}
for name,minor in [('scheduler','3.12.'),('mpt','3.11.'),('narrato','3.12.')]:
    py=root/'envs'/name/'Scripts/python.exe'
    q=run([py,'-c','import sys;print(sys.version.split()[0])'])
    check=run(['uv','pip','check','--python',py])
    r['environments'][name]={'path':str(py),'version':q['stdout'],'pip_check':check,'qualified':q['exit_code']==0 and q['stdout'].startswith(minor) and check['exit_code']==0}
    r['environment_ready'] &= r['environments'][name]['qualified']
for name,folder,commit in [('mpt','MoneyPrinterTurbo','4204d0f39afa8ada6f3edc31604141f959fe254e'),('narrato','NarratoAI','9fa69e022d4add41205ee385207561df8796b3f1')]:
    path=root/'upstream'/folder
    head=run(['git','rev-parse','HEAD'],path)
    dirty=run(['git','status','--porcelain','--untracked-files=no'],path)
    # This smoke imports each app in its own interpreter and cwd, without model modules.
    smoke=run([root/'envs'/name/'Scripts/python.exe','-c',"import app,toml,importlib.metadata as m,json;print(json.dumps({'app':app.__file__,'moviepy':m.version('moviepy'),'toml':m.version('toml'),'torch_installed':bool(m.packages_distributions().get('torch'))}))"],path)
    qualified=head['stdout']==commit and dirty['exit_code']==0 and not dirty['stdout'] and smoke['exit_code']==0
    r['upstreams'][name]={'commit':head['stdout'],'clean':not dirty['stdout'],'lock_sha256':sha(path/'uv.lock'),'smoke':smoke,'qualified':qualified}
    r['environment_ready'] &= qualified
for p in (root/'locks').glob('*.lock'):r['locks'][p.name]=sha(p)
ff=root/'tools/ffmpeg/ffmpeg.exe';fp=root/'tools/ffmpeg/ffprobe.exe'
version=run([ff,'-version']);filters=run([ff,'-filters']);encoders=run([ff,'-encoders'])
requirements=['sidechaincompress','loudnorm','amix','subtitles','colorspace']
ok='version 6.1.1' in version['stdout'] and all(x in filters['stdout'] for x in requirements) and 'libx264' in encoders['stdout']
r['ffmpeg']={'version':version['stdout'].splitlines()[0],'ffmpeg_sha256':sha(ff),'ffprobe_sha256':sha(fp),'required_filters':{x:x in filters['stdout'] for x in requirements},'libx264':'libx264' in encoders['stdout'],'qualified':ok}
r['environment_ready'] &= ok
r['gpu']=run(['nvidia-smi','--query-gpu=name,memory.total,memory.used,driver_version','--format=csv,noheader'])
ports={}
for port in [8760,8761,8762,8763,9235]:
    with socket.socket() as s:s.settimeout(1);ports[str(port)]=s.connect_ex(('127.0.0.1',port))==0
r['ports_listening']=ports
# No /health request: the existing Cosy endpoint loads a model on health.
r['models']={'cosy':{'listening':ports['9235'],'status':'NOT_QUALIFIED','reason':'existing ONNX Runtime 1.18.0 exposes CPU/Azure only; no CUDA EP; not invoked'},'asr':{'model_present':(root/'upstream/MoneyPrinterTurbo/models/whisper-small/model.bin').is_file(),'status':'NOT_TESTED'},'cloud':{'status':'WAITING_CONFIGURATION_AND_BUDGET'},'lipsync':{'enabled':False,'code':'CAPABILITY_UNAVAILABLE_LIPSYNC'}}
print(json.dumps(r,ensure_ascii=False))
sys.exit(0 if r['environment_ready'] else 1)
'@
$source | & $python - $root
exit $LASTEXITCODE
