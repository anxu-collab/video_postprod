"""Current-user single-instance Windows supervisor, bounded retries, Job Object."""
import ctypes,datetime,json,os,socket,subprocess,sys,time,urllib.request
from ctypes import wintypes
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scheduler.security import worker_token
from workers.common import atomic_json
k=ctypes.WinDLL('kernel32',use_last_error=True)
class Basic(ctypes.Structure):_fields_=[('PerProcessUserTimeLimit',ctypes.c_int64),('PerJobUserTimeLimit',ctypes.c_int64),('LimitFlags',wintypes.DWORD),('MinimumWorkingSetSize',ctypes.c_size_t),('MaximumWorkingSetSize',ctypes.c_size_t),('ActiveProcessLimit',wintypes.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',wintypes.DWORD),('SchedulingClass',wintypes.DWORD)]
class IO(ctypes.Structure):_fields_=[(name,ctypes.c_uint64) for name in ['ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount']]
class Extended(ctypes.Structure):_fields_=[('BasicLimitInformation',Basic),('IoInfo',IO),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
for fn in ['CreateMutexW','CreateJobObjectW','OpenProcess']:getattr(k,fn).restype=wintypes.HANDLE
k.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR];k.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]
k.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD];k.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
k.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD];k.CloseHandle.argtypes=[wintypes.HANDLE]
k.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)]

def process_at(port):
    code=f'$c=Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if($c){{Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)" | Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress}}'
    p=subprocess.run(['powershell','-NoProfile','-Command',code],capture_output=True,text=True,timeout=15)
    return json.loads(p.stdout) if p.stdout.strip() else None
def alive(port):
    with socket.socket() as s:s.settimeout(.2);return s.connect_ex(('127.0.0.1',port))==0
def main():
    mutex=k.CreateMutexW(None,False,'Local\\laox.video_postprod.supervisor.v1')
    if ctypes.get_last_error()==183:return
    jobs={};all_jobs=[]
    def new_job(name):
        job=k.CreateJobObjectW(None,None);limits=Extended();limits.BasicLimitInformation.LimitFlags=0x2000
        if not k.SetInformationJobObject(job,9,ctypes.byref(limits),ctypes.sizeof(limits)):raise ctypes.WinError(ctypes.get_last_error())
        jobs[name]=job;all_jobs.append(job)
    def attach(pid,name):
        if name not in jobs:new_job(name)
        job=jobs[name]
        handle=k.OpenProcess(0x0100|0x0001|0x0400,False,pid)
        if not handle:raise ctypes.WinError(ctypes.get_last_error())
        try:
            inside=wintypes.BOOL()
            if not k.IsProcessInJob(handle,job,ctypes.byref(inside)):raise ctypes.WinError(ctypes.get_last_error())
            if not inside.value and not k.AssignProcessToJobObject(job,handle):raise ctypes.WinError(ctypes.get_last_error())
        finally:k.CloseHandle(handle)
    definitions={'mpt':(8761,'mpt','workers/mpt/server.py'),'narrato':(8762,'narrato','workers/narrato/server.py'),'scheduler':(8760,'scheduler','scheduler/server.py'),'cosy':(9235,'cosy_cuda_overlay','workers/mpt/cosy_cuda_launcher.py')}
    env=dict(os.environ,PYTHONUTF8='1',PYTHONNOUSERSITE='1',POSTPROD_WORKER_TOKEN=worker_token());children={};restarts={name:[] for name in definitions};state={};attached=set()
    atomic_json(ROOT/'state/processes/supervisor.json',{'pid':os.getpid(),'started_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'job_object':'kill-on-close','single_instance':True})
    try:
        while not (ROOT/'state/stop.request').exists():
            for name,(port,venv,script) in definitions.items():
                child=children.get(name)
                if alive(port):
                    if name not in state or not state[name].get('adopted'):
                        owner=process_at(port)
                        if not owner or str(ROOT/script).lower() not in owner['CommandLine'].replace('/','\\').lower():raise RuntimeError('PORT_OWNER_UNVERIFIED:'+name)
                        if owner['ProcessId'] not in attached:attach(owner['ProcessId'],name);attached.add(owner['ProcessId'])
                        state[name]={'pid':owner['ProcessId'],'port':port,'adopted':True,'status':'listening'}
                    continue
                if child and child.poll() is None:
                    if time.time()-state[name]['started']<360:continue
                    child.terminate();state[name]['status']='startup_timeout';continue
                history=[t for t in restarts[name] if time.time()-t<600];restarts[name]=history
                if len(history)>=3:state[name]={'status':'failed','reason':'RESTART_LIMIT_3_PER_10_MINUTES'};continue
                if history and time.time()-history[-1]<[5,30,60][min(len(history)-1,2)]:continue
                stdout=(ROOT/'logs'/(name+'-supervisor.stdout.log')).open('ab');stderr=(ROOT/'logs'/(name+'-supervisor.stderr.log')).open('ab')
                new_job(name)
                child=subprocess.Popen([str(ROOT/'envs'/venv/'Scripts/python.exe'),str(ROOT/script)],cwd=ROOT,env=env,stdout=stdout,stderr=stderr,creationflags=subprocess.CREATE_NO_WINDOW)
                stdout.close();stderr.close();attach(child.pid,name);attached.add(child.pid);children[name]=child;restarts[name].append(time.time());state[name]={'pid':child.pid,'port':port,'started':time.time(),'status':'starting'}
            atomic_json(ROOT/'state/service-status.json',{'updated_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'components':state,'developer_agents':False});time.sleep(3)
    finally:
        for handle in all_jobs:k.CloseHandle(handle)
        k.CloseHandle(mutex)
        stop=ROOT/'state/stop.request'
        if stop.exists():stop.unlink()
if __name__=='__main__':main()
