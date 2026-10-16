"""Bounded one-operation model processes; controllers never import GPU libraries."""
import ctypes
import datetime
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]


def utc():return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _safe(text):
    return re.sub(r'(?i)(bearer\s+|(?:token|api[_-]?key|password)\s*[=:]\s*)[^\s,;"}]+',r'\1[REDACTED]',str(text))


def _write(path,value):
    path=Path(path);temp=path.with_suffix('.partial.json')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temp,path)


class ModelProcessFailure(RuntimeError):pass


def _execution_gate():
    """Child stdlib-only gate: no model import until controller verifies its Job."""
    nonce=os.environ.get('POSTPROD_EXECUTION_NONCE')
    if not nonce:raise ModelProcessFailure('MODEL_EXECUTION_GATE_REQUIRED')
    identity={'nonce':nonce,'process_id':os.getpid()}
    _write(os.environ['POSTPROD_EXECUTION_REQUEST'],identity)
    ack=Path(os.environ['POSTPROD_EXECUTION_ACK']);limit=time.monotonic()+15
    while time.monotonic()<limit:
        if ack.exists():
            if json.loads(ack.read_text(encoding='utf-8'))!=identity:
                raise ModelProcessFailure('MODEL_EXECUTION_ACK_MISMATCH')
            return identity
        time.sleep(.01)
    raise ModelProcessFailure('MODEL_EXECUTION_GATE_TIMEOUT')


class _WindowsProcess:
    """Small waitable process handle; ownership was assigned inside CreateProcess."""
    def __init__(self,k,info):
        self.k=k;self.handle=info.hProcess;self.thread=info.hThread;self.pid=info.dwProcessId

    def resume(self):
        if self.k.ResumeThread(self.thread)==0xffffffff:raise ctypes.WinError(ctypes.get_last_error())
        self.k.CloseHandle(self.thread);self.thread=None

    def poll(self):
        status=self.k.WaitForSingleObject(self.handle,0)
        if status==258:return None
        if status!=0:raise ctypes.WinError(ctypes.get_last_error())
        from ctypes import wintypes as w
        code=w.DWORD()
        if not self.k.GetExitCodeProcess(self.handle,ctypes.byref(code)):raise ctypes.WinError(ctypes.get_last_error())
        return code.value

    def wait(self,timeout):
        status=self.k.WaitForSingleObject(self.handle,max(1,int(timeout*1000)))
        if status==258:raise subprocess.TimeoutExpired('owned-model-child',timeout)
        if status!=0:raise ctypes.WinError(ctypes.get_last_error())
        return self.poll()

    def kill(self):
        if not self.k.TerminateProcess(self.handle,1):raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.thread:self.k.CloseHandle(self.thread);self.thread=None
        if self.handle:self.k.CloseHandle(self.handle);self.handle=None


class _WindowsTree:
    """CreateProcess JOB_LIST atomically binds a child before any code can run.

    The handle is not inherited. Controller crashes close it in the OS, killing
    models and descendants even when Python finally blocks cannot run.
    """
    def __init__(self):
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_=[('a',ctypes.c_int64),('b',ctypes.c_int64),('flags',w.DWORD),('min',ctypes.c_size_t),('max',ctypes.c_size_t),('active',w.DWORD),('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(ctypes.Structure):_fields_=[('io'+str(i),ctypes.c_uint64) for i in range(6)]
        class Extended(ctypes.Structure):
            _fields_=[('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        class Accounting(ctypes.Structure):
            _fields_=[('time'+str(i),ctypes.c_int64) for i in range(4)]+[('faults',w.DWORD),('total',w.DWORD),('active',w.DWORD),('terminated',w.DWORD)]
        self.Accounting=Accounting;self.k=ctypes.WinDLL('kernel32',use_last_error=True)
        self.k.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR];self.k.CreateJobObjectW.restype=w.HANDLE
        self.k.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        self.k.TerminateJobObject.argtypes=[w.HANDLE,w.UINT]
        self.k.QueryInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD,ctypes.c_void_p]
        self.k.CloseHandle.argtypes=[w.HANDLE]
        self.handle=self.k.CreateJobObjectW(None,None)
        if not self.handle:raise ctypes.WinError(ctypes.get_last_error())
        limits=Extended();limits.basic.flags=0x2000
        if not self.k.SetInformationJobObject(self.handle,9,ctypes.byref(limits),ctypes.sizeof(limits)):
            self.k.CloseHandle(self.handle);self.handle=None;raise ctypes.WinError(ctypes.get_last_error())

    def create(self,argv,env,diagnostics):
        """Win10+ STARTUPINFOEX; no create-then-assign race or unsafe fallback.

        Constants follow Microsoft WinBase.h ProcThreadAttributeValue definitions.
        HANDLE_LIST restricts inherited handles to stdin/log, excluding the Job.
        """
        from ctypes import wintypes as w
        import msvcrt
        class Startup(ctypes.Structure):
            _fields_=[('cb',w.DWORD),('reserved',w.LPWSTR),('desktop',w.LPWSTR),('title',w.LPWSTR),
                ('x',w.DWORD),('y',w.DWORD),('xs',w.DWORD),('ys',w.DWORD),('xc',w.DWORD),('yc',w.DWORD),
                ('fill',w.DWORD),('flags',w.DWORD),('show',w.WORD),('reserved_size',w.WORD),
                ('reserved_ptr',ctypes.c_void_p),('stdin',w.HANDLE),('stdout',w.HANDLE),('stderr',w.HANDLE)]
        class ExtendedStartup(ctypes.Structure):_fields_=[('startup',Startup),('attributes',ctypes.c_void_p)]
        class ProcessInfo(ctypes.Structure):
            _fields_=[('hProcess',w.HANDLE),('hThread',w.HANDLE),('dwProcessId',w.DWORD),('dwThreadId',w.DWORD)]
        k=self.k
        k.InitializeProcThreadAttributeList.argtypes=[ctypes.c_void_p,w.DWORD,w.DWORD,ctypes.POINTER(ctypes.c_size_t)]
        k.UpdateProcThreadAttribute.argtypes=[ctypes.c_void_p,w.DWORD,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_void_p]
        k.DeleteProcThreadAttributeList.argtypes=[ctypes.c_void_p]
        k.CreateProcessW.argtypes=[w.LPCWSTR,w.LPWSTR,ctypes.c_void_p,ctypes.c_void_p,w.BOOL,w.DWORD,ctypes.c_void_p,w.LPCWSTR,ctypes.c_void_p,ctypes.POINTER(ProcessInfo)]
        k.GetCurrentProcess.restype=w.HANDLE
        k.DuplicateHandle.argtypes=[w.HANDLE,w.HANDLE,w.HANDLE,ctypes.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD]
        k.ResumeThread.argtypes=[w.HANDLE];k.ResumeThread.restype=w.DWORD
        k.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];k.WaitForSingleObject.restype=w.DWORD
        k.GetExitCodeProcess.argtypes=[w.HANDLE,ctypes.POINTER(w.DWORD)]
        k.TerminateProcess.argtypes=[w.HANDLE,w.UINT]
        size=ctypes.c_size_t()
        k.InitializeProcThreadAttributeList(None,2,0,ctypes.byref(size))
        if not size.value:raise ctypes.WinError(ctypes.get_last_error())
        attributes=ctypes.create_string_buffer(size.value);initialized=False;inherited=[]
        try:
            if not k.InitializeProcThreadAttributeList(attributes,2,0,ctypes.byref(size)):raise ctypes.WinError(ctypes.get_last_error())
            initialized=True
            jobs=(w.HANDLE*1)(self.handle)
            if not k.UpdateProcThreadAttribute(attributes,0,0x2000d,jobs,ctypes.sizeof(jobs),None,None):raise ctypes.WinError(ctypes.get_last_error())
            with open(os.devnull,'rb') as null:
                for file in (null,diagnostics):
                    duplicate=w.HANDLE();current=k.GetCurrentProcess()
                    if not k.DuplicateHandle(current,msvcrt.get_osfhandle(file.fileno()),current,ctypes.byref(duplicate),0,True,2):raise ctypes.WinError(ctypes.get_last_error())
                    inherited.append(duplicate.value)
                handles=(w.HANDLE*2)(*inherited)
                if not k.UpdateProcThreadAttribute(attributes,0,0x20002,handles,ctypes.sizeof(handles),None,None):raise ctypes.WinError(ctypes.get_last_error())
                startup=ExtendedStartup();startup.startup.cb=ctypes.sizeof(startup);startup.attributes=ctypes.addressof(attributes)
                startup.startup.flags=0x100;startup.startup.stdin=inherited[0]
                startup.startup.stdout=inherited[1];startup.startup.stderr=inherited[1]
                command=ctypes.create_unicode_buffer(subprocess.list2cmdline([str(a) for a in argv]))
                environment=ctypes.create_unicode_buffer('\0'.join(k+'='+v for k,v in sorted(env.items(),key=lambda item:item[0].upper()))+'\0\0')
                info=ProcessInfo()
                # EXTENDED_STARTUPINFO_PRESENT | UNICODE_ENVIRONMENT | NO_WINDOW | SUSPENDED.
                # The suspended child already belongs to our kill-on-close Job.
                if not k.CreateProcessW(str(argv[0]),command,None,None,True,0x80000|0x400|0x8000000|0x4,
                    environment,str(ROOT),ctypes.byref(startup),ctypes.byref(info)):
                    raise ctypes.WinError(ctypes.get_last_error())
                return _WindowsProcess(k,info)
        finally:
            if initialized:k.DeleteProcThreadAttributeList(attributes)
            for handle in inherited:k.CloseHandle(handle)

    def close(self):
        if not self.handle:return
        try:
            if not self.k.TerminateJobObject(self.handle,1):raise ctypes.WinError(ctypes.get_last_error())
            limit=time.monotonic()+5
            while True:
                state=self.Accounting()
                if not self.k.QueryInformationJobObject(self.handle,1,ctypes.byref(state),ctypes.sizeof(state),None):
                    raise ctypes.WinError(ctypes.get_last_error())
                if state.active==0:break
                if time.monotonic()>=limit:raise RuntimeError('MODEL_PROCESS_TREE_EXIT_UNCONFIRMED')
                time.sleep(.02)
        finally:self.k.CloseHandle(self.handle);self.handle=None

    def verify_member(self,pid):
        """Verify a live inference PID belongs to this operation's exact Job."""
        from ctypes import wintypes as w
        self.k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];self.k.OpenProcess.restype=w.HANDLE
        self.k.IsProcessInJob.argtypes=[w.HANDLE,w.HANDLE,ctypes.POINTER(w.BOOL)]
        handle=self.k.OpenProcess(0x1000,False,pid)
        if not handle:raise ModelProcessFailure('MODEL_EXECUTION_PID_NOT_LIVE')
        try:
            member=w.BOOL()
            if not self.k.IsProcessInJob(handle,self.handle,ctypes.byref(member)) or not member.value:
                raise ModelProcessFailure('MODEL_EXECUTION_PID_OUTSIDE_JOB')
        finally:self.k.CloseHandle(handle)


def run_child(argv,work,output,*,timeout_sec,operation,execution_handshake=False):
    """Internal trusted argv only, no shell. Return only after process-tree exit."""
    if not 0<timeout_sec<=600:raise ValueError('MODEL_TIMEOUT_OUT_OF_RANGE')
    work=Path(work);work.mkdir(parents=True,exist_ok=True);output=Path(output)
    audit={'operation':operation,'request_started_utc':utc(),'request_finished_utc':None,
        'controller_process_id':os.getpid(),'launcher_process_id':None,'model_process_id':None,'execution_verified':False,'state':'STARTING',
        'process_exited':False,'process_tree_closed':False,'model_unloaded':False,
        'unload_method':'process_tree_exit','neural_device_policy':'cuda_only','new_model_downloads':0}
    process=None;tree=None;error=None;result=None
    env=dict(os.environ,PYTHONUTF8='1',PYTHONNOUSERSITE='1',HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',MODELSCOPE_OFFLINE='1')
    handshake=work/'model-execution-request.json';ack=work/'model-execution-ack.json'
    nonce=secrets.token_hex(32) if execution_handshake else None
    if execution_handshake:
        env.update(POSTPROD_EXECUTION_NONCE=nonce,POSTPROD_EXECUTION_REQUEST=str(handshake.resolve()),POSTPROD_EXECUTION_ACK=str(ack.resolve()))
    log=work/'model-child.log'
    try:
        if output.exists():raise ValueError('MODEL_OUTPUT_ALREADY_EXISTS')
        if execution_handshake and (handshake.exists() or ack.exists()):raise ModelProcessFailure('MODEL_EXECUTION_HANDSHAKE_ALREADY_EXISTS')
        tree=_WindowsTree() if os.name=='nt' else None
        with log.open('wb') as diagnostics:
            process=tree.create(argv,env,diagnostics) if tree else subprocess.Popen(argv,cwd=ROOT,env=env,
                stdin=subprocess.DEVNULL,stdout=diagnostics,stderr=subprocess.STDOUT,start_new_session=True)
            audit['launcher_process_id']=process.pid
            if not execution_handshake:audit['model_process_id']=process.pid
            audit['state']='RUNNING';_write(work/'model-lifecycle.json',audit)
            if tree:process.resume()
            deadline=time.monotonic()+timeout_sec
            try:
                if execution_handshake:
                    gate_deadline=min(deadline,time.monotonic()+15)
                    while not handshake.exists():
                        if process.poll() is not None:raise ModelProcessFailure('MODEL_EXECUTION_HANDSHAKE_MISSING')
                        if time.monotonic()>=gate_deadline:raise subprocess.TimeoutExpired('execution-gate',timeout_sec)
                        time.sleep(.01)
                    identity=json.loads(handshake.read_text(encoding='utf-8'));pid=identity.get('process_id')
                    if identity.get('nonce')!=nonce or not isinstance(pid,int) or pid<=0:
                        raise ModelProcessFailure('MODEL_EXECUTION_HANDSHAKE_INVALID')
                    if tree:tree.verify_member(pid)
                    elif os.getpgid(pid)!=process.pid:raise ModelProcessFailure('MODEL_EXECUTION_PID_OUTSIDE_GROUP')
                    audit.update(model_process_id=pid,execution_verified=True,execution_verified_utc=utc())
                    _write(work/'model-lifecycle.json',audit);_write(ack,identity)
                code=process.wait(timeout=max(.001,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                audit['state']='TIMED_OUT';raise ModelProcessFailure('MODEL_OPERATION_TIMEOUT')
            audit['exit_code']=code
            if code!=0:
                detail=log.read_text(encoding='utf-8',errors='replace')[-2000:]
                raise ModelProcessFailure('MODEL_OPERATION_FAILED:'+_safe(detail))
            if not output.is_file():raise ModelProcessFailure('MODEL_RESULT_MISSING')
            result=json.loads(output.read_text(encoding='utf-8'))
            if not isinstance(result,dict):raise ModelProcessFailure('MODEL_RESULT_INVALID')
            if execution_handshake:
                if result.pop('execution_nonce',None)!=nonce or result.pop('execution_process_id',None)!=audit['model_process_id']:
                    raise ModelProcessFailure('MODEL_EXECUTION_RESULT_MISMATCH')
                evidence=result.get('device_evidence') or {}
                if evidence.get('process_id') is not None and evidence['process_id']!=audit['model_process_id']:
                    raise ModelProcessFailure('MODEL_EXECUTION_EVIDENCE_MISMATCH')
            audit['state']='SUCCEEDED'
    except Exception as exc:
        error=exc
        if audit['state']!='TIMED_OUT':audit['state']='FAILED'
        audit['error']=_safe(type(exc).__name__+':'+str(exc))
    finally:
        try:
            if tree:tree.close()
            elif process:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            if process:
                # TerminateJobObject has already requested termination. Its
                # accounting can reach zero just before the process handle
                # signals; a second TerminateProcess then returns access denied.
                if not tree and process.poll() is None:process.kill()
                process.wait(timeout=5)
            audit.update(process_exited=process is None or process.poll() is not None,process_tree_closed=True)
            audit['model_unloaded']=audit['process_exited'] and audit['process_tree_closed']
        except Exception as exc:
            error=ModelProcessFailure('MODEL_UNLOAD_UNCONFIRMED:'+_safe(exc))
            audit.update(state='FAILED',cleanup_error=_safe(exc),model_unloaded=False)
        if isinstance(process,_WindowsProcess):process.close()
        audit['request_finished_utc']=utc();_write(work/'model-lifecycle.json',audit)
    if error:raise ModelProcessFailure(_safe(error)) from error
    result['model_lifecycle']=audit
    return result


def run_model_process(kind,payload,work,*,timeout_sec=570,python_executable=None):
    if kind not in ('cosy','whisper'):raise ValueError('MODEL_KIND_NOT_ALLOWED')
    work=Path(work);work.mkdir(parents=True,exist_ok=True)
    request=work/('model-'+kind+'-request.json');output=work/('model-'+kind+'-response.json')
    _write(request,payload)
    return run_child([str(python_executable or sys.executable),str(Path(__file__).resolve()),
        '--kind',kind,'--request',str(request),'--output',str(output)],work,output,timeout_sec=timeout_sec,operation=kind,execution_handshake=True)


def main():
    import argparse
    identity=_execution_gate()
    sys.path.insert(0,str(ROOT))
    parser=argparse.ArgumentParser();parser.add_argument('--kind',choices=['cosy','whisper'],required=True)
    parser.add_argument('--request',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
    payload=json.loads(Path(args.request).read_text(encoding='utf-8'))
    if args.kind=='cosy':
        from workers.mpt.cosy_cuda_launcher import run_cosy_once
        result=run_cosy_once(payload)
    else:
        upstream=ROOT/'upstream/MoneyPrinterTurbo';os.chdir(upstream);sys.path.insert(0,str(upstream))
        os.environ['PATH']=str(ROOT/'tools/ffmpeg')+os.pathsep+os.environ.get('PATH','')
        from workers.mpt.components import _transcribe_in_process
        result=_transcribe_in_process(Path(payload['source']),Path(payload['work']))
    result.update(execution_process_id=identity['process_id'],execution_nonce=identity['nonce'])
    _write(args.output,result)


if __name__=='__main__':main()
