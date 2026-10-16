"""Real isolated Python children, no neural models or production services."""
import datetime
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest


def test_control_import_does_not_import_upstream_or_gpu_models():
    from workers.mpt import model_runner
    from workers.mpt import cosy_cuda_launcher
    app=cosy_cuda_launcher.create_app()
    assert not any(k in sys.modules for k in ('torch','engine','ctranslate2','app.services.subtitle'))
    endpoint=next(r.endpoint for r in app.routes if r.path=='/postprod/device-evidence')
    evidence=endpoint()
    assert evidence['model_loaded'] is False and evidence['model_lifecycle']=='on_demand'
    assert 'process_id' not in evidence and 'passed' not in evidence


@pytest.mark.parametrize('behavior',['success','error','timeout'])
def test_model_child_and_descendants_exit_on_every_outcome(tmp_path,behavior):
    from workers.mpt.model_runner import run_child,ModelProcessFailure
    child=tmp_path/'fake.py';output=tmp_path/'child-result.json'
    child.write_text('''import json,os,subprocess,sys,time
from pathlib import Path
sub=subprocess.Popen([sys.executable,"-c","import time;time.sleep(120)"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
Path(sys.argv[2]).write_text(str(sub.pid))
if sys.argv[1]=="timeout":time.sleep(120)
if sys.argv[1]=="error":raise RuntimeError("injected model failure")
Path(sys.argv[3]).write_text(json.dumps({"success":True,"voice_id":"laox","preset":"laox_warming_compact"}))
''',encoding='utf-8')
    argv=[sys.executable,str(child),behavior,str(tmp_path/'descendant.pid'),str(output)]
    if behavior=='success':
        result=run_child(argv,tmp_path,output,timeout_sec=5,operation='TEST_MODEL')
        assert result['voice_id']=='laox' and result['preset']=='laox_warming_compact'
    else:
        with pytest.raises(ModelProcessFailure):run_child(argv,tmp_path,output,timeout_sec=.8 if behavior=='timeout' else 5,operation='TEST_MODEL')
    audit=json.loads((tmp_path/'model-lifecycle.json').read_text())
    assert audit['model_unloaded'] and audit['process_exited'] and audit['process_tree_closed']
    assert audit['state']=={'success':'SUCCEEDED','error':'FAILED','timeout':'TIMED_OUT'}[behavior]
    begin=datetime.datetime.fromisoformat(audit['request_started_utc']);end=datetime.datetime.fromisoformat(audit['request_finished_utc'])
    assert begin.tzinfo and end.tzinfo and begin<=end
    pid=int((tmp_path/'descendant.pid').read_text())
    if os.name=='nt':
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.restype=ctypes.c_void_p
        handle=kernel.OpenProcess(0x00100000,False,pid)
        if handle:
            try:assert kernel.WaitForSingleObject(ctypes.c_void_p(handle),1000)==0
            finally:kernel.CloseHandle(ctypes.c_void_p(handle))
    else:
        # A killed child may remain briefly as a zombie until its owner reaps it.
        stat=Path('/proc')/str(pid)/'stat'
        assert not stat.exists() or stat.read_text().split()[2]=='Z'


def test_asr_controller_only_dispatches_model_child(monkeypatch,tmp_path):
    from workers.mpt import components
    from workers.mpt import model_runner
    calls=[]
    def runner(kind,payload,work,**kwargs):
        calls.append((kind,payload,work,kwargs));return {'output_refs':[], 'device_evidence':{},'metrics':{},'model_lifecycle':{'model_unloaded':True}}
    monkeypatch.setattr(model_runner,'run_model_process',runner)
    components.transcribe(tmp_path/'source.wav',tmp_path)
    assert len(calls)==1 and calls[0][0]=='whisper'
    assert 'ctranslate2' not in sys.modules and 'app.services.subtitle' not in sys.modules


def test_cosy_missing_weights_refuses_before_gpu_import(monkeypatch,tmp_path):
    from workers.mpt import cosy_cuda_launcher
    monkeypatch.setattr(cosy_cuda_launcher,'EXISTING',tmp_path)
    with pytest.raises(ValueError,match='COSY_LOCAL_MODEL_MISSING'):
        cosy_cuda_launcher.run_cosy_once({'text':'test','voice_id':'laox','preset':'laox_warming_compact','output_path':str(tmp_path/'voice.wav')})
    assert 'torch' not in sys.modules and 'engine' not in sys.modules


def test_provider_binds_fresh_cuda_evidence_to_exited_child(monkeypatch,tmp_path):
    from workers.mpt import cosy_provider
    import io
    responses=[]
    live={'model_lifecycle':'on_demand','backend':'torch_cuda_aux_v1','controller_process_id':91,'model_loaded':False}
    result={'success':True,'audio_path':str(tmp_path/'voice.wav'),'duration_sec':.1,'sample_rate':24000,
        'device_evidence':{'passed':True,'cpu_neural_nodes':0,'backend':'torch_cuda_aux_v1','process_id':92},
        'model_lifecycle':{'state':'SUCCEEDED','model_process_id':92,'launcher_process_id':90,'execution_verified':True,'process_exited':True,'process_tree_closed':True,'model_unloaded':True}}
    (tmp_path/'voice.wav').write_bytes(b'only-audio-binding-test')
    def response(request,timeout):
        responses.append(request)
        return io.BytesIO(json.dumps(live if len(responses)%2 else result).encode())
    monkeypatch.setattr(cosy_provider.urllib.request,'urlopen',response)
    monkeypatch.setattr(cosy_provider,'worker_token',lambda:'test-token')
    qualified=cosy_provider.synthesize({'text':'test','preset':'laox_warming_compact'},tmp_path)
    assert qualified['device_evidence']['model_lifecycle']['model_unloaded']
    result['model_lifecycle']['execution_verified']=False
    with pytest.raises(ValueError,match='INFERENCE_PROCESS_MISMATCH'):
        cosy_provider.synthesize({'text':'test','preset':'laox_warming_compact'},tmp_path)
    result['model_lifecycle']['execution_verified']=True
    result['model_lifecycle']['model_unloaded']=False
    with pytest.raises(ValueError,match='MODEL_UNLOAD_UNCONFIRMED'):
        cosy_provider.synthesize({'text':'test','preset':'laox_warming_compact'},tmp_path)
    result['model_lifecycle']['model_unloaded']=True;result['device_evidence']['process_id']=999
    with pytest.raises(ValueError,match='INFERENCE_PROCESS_MISMATCH'):
        cosy_provider.synthesize({'text':'test','preset':'laox_warming_compact'},tmp_path)


@pytest.mark.skipif(os.name!='nt',reason='Windows kill-on-close Job Object guarantee')
def test_controller_crash_reclaims_owned_model_child(tmp_path):
    import time
    import ctypes
    root=Path(__file__).resolve().parents[1]
    controller=tmp_path/'fake_controller.py'
    controller.write_text('''import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from workers.mpt.model_runner import run_child
work=Path(sys.argv[2])
run_child([sys.executable,"-c","import os,time;from pathlib import Path;Path(r'"+str(work/'model.pid')+"').write_text(str(os.getpid()));time.sleep(120)"],work,work/'no-output.json',timeout_sec=120,operation='TEST_CONTROLLER_CRASH')
''',encoding='utf-8')
    process=subprocess.Popen([sys.executable,str(controller),str(root),str(tmp_path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        limit=time.monotonic()+5
        while not (tmp_path/'model.pid').is_file() and time.monotonic()<limit:time.sleep(.02)
        assert (tmp_path/'model.pid').is_file()
        pid=int((tmp_path/'model.pid').read_text())
        kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.OpenProcess.restype=ctypes.c_void_p
        handle=kernel.OpenProcess(0x00100000,False,pid)
        assert handle
        try:
            process.kill();process.wait(timeout=5)
            assert kernel.WaitForSingleObject(ctypes.c_void_p(handle),5000)==0
        finally:kernel.CloseHandle(ctypes.c_void_p(handle))
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=5)


@pytest.mark.skipif(os.name!='nt',reason='Windows atomic Job process creation')
def test_controller_crash_at_first_post_create_journal_cannot_orphan_suspended_child(tmp_path):
    """Kill at the former Popen/Assign gap, before any child Python executes."""
    import time
    import ctypes
    root=Path(__file__).resolve().parents[1]
    controller=tmp_path/'creation_gap_controller.py'
    controller.write_text('''import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from workers.mpt import model_runner as runner
work=Path(sys.argv[2])
original=runner._write
def paused_journal(path,value):
    if value.get('state')=='RUNNING':
        (work/'created.pid').write_text(str(value['model_process_id']))
        time.sleep(120)
    return original(path,value)
runner._write=paused_journal
runner.run_child([sys.executable,'-c',"from pathlib import Path;import time;Path(r'"+str(work/'model-code-ran')+"').write_text('would-load-GPU');time.sleep(120)"],work,work/'none.json',timeout_sec=120,operation='TEST_ATOMIC_CREATE')
''',encoding='utf-8')
    process=subprocess.Popen([sys.executable,str(controller),str(root),str(tmp_path)],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.restype=ctypes.c_void_p
    handle=None
    try:
        limit=time.monotonic()+5
        while not (tmp_path/'created.pid').exists() and time.monotonic()<limit:time.sleep(.02)
        assert (tmp_path/'created.pid').exists()
        pid=int((tmp_path/'created.pid').read_text())
        handle=kernel.OpenProcess(0x00100000|0x1,False,pid)
        assert handle
        assert not (tmp_path/'model-code-ran').exists()
        process.kill();process.wait(timeout=5)
        assert kernel.WaitForSingleObject(ctypes.c_void_p(handle),1000)==0, 'orphaned suspended child at creation/Job gap'
        assert not (tmp_path/'model-code-ran').exists()
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=5)
        if handle:
            # Clean up the known test PID even when reproducing the old leak.
            kernel.TerminateProcess(ctypes.c_void_p(handle),1)
            kernel.WaitForSingleObject(ctypes.c_void_p(handle),1000)
            kernel.CloseHandle(ctypes.c_void_p(handle))


@pytest.mark.skipif(os.name!='nt',reason='Installed Windows venv launcher delegation')
@pytest.mark.parametrize('forged',['none','result_pid','handshake_pid'])
def test_real_cosy_venv_launcher_binds_only_this_job_inference_pid(tmp_path,forged):
    from workers.mpt.model_runner import run_child,ModelProcessFailure
    root=Path(__file__).resolve().parents[1]
    executable=root/'envs/cosy_cuda_overlay/Scripts/python.exe'
    assert executable.is_file(), 'real existing overlay required; no synthetic wrapper'
    child=tmp_path/'wrapper_probe.py';output=tmp_path/'result.json'
    child.write_text('''import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from workers.mpt import model_runner as runner
if sys.argv[3]=='handshake_pid':
    runner._write(os.environ['POSTPROD_EXECUTION_REQUEST'],{'nonce':os.environ['POSTPROD_EXECUTION_NONCE'],'process_id':int(sys.argv[4])})
    import time;time.sleep(120)
identity=runner._execution_gate()
Path(sys.argv[2]).with_suffix('.ran').write_text('no-GPU-test-code')
pid=os.getpid() if sys.argv[3]!='result_pid' else int(sys.argv[4])
runner._write(sys.argv[2],{'success':True,'execution_process_id':pid,'execution_nonce':identity['nonce'],'device_evidence':{'process_id':pid}})
''',encoding='utf-8')
    argv=[str(executable),str(child),str(root),str(output),forged,str(os.getpid())]
    if forged=='none':
        result=run_child(argv,tmp_path,output,timeout_sec=5,operation='TEST_REAL_VENV',execution_handshake=True)
        audit=result['model_lifecycle']
        assert audit['launcher_process_id']!=audit['model_process_id']
        assert audit['model_process_id']==result['device_evidence']['process_id']
        assert audit['execution_verified'] and audit['model_unloaded'] and audit['process_tree_closed']
        assert 'execution_nonce' not in result
    else:
        with pytest.raises(ModelProcessFailure,match='EXECUTION'):
            run_child(argv,tmp_path,output,timeout_sec=5,operation='TEST_REAL_VENV',execution_handshake=True)
        audit=json.loads((tmp_path/'model-lifecycle.json').read_text(encoding='utf-8'))
        assert audit['model_unloaded'] and audit['state']=='FAILED'
        if forged=='handshake_pid':assert not output.with_suffix('.ran').exists()
