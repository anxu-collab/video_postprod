"""One HTTP adapter to the existing Cosy service; no alternative TTS engine."""
import json,urllib.request
from workers.common import ROOT,atomic_json,output_ref
from scheduler.security import worker_token

def synthesize(params,work):
    # Health attests a lightweight controller, never a resident model. Fresh
    # inference placement and confirmed child exit are bound to THIS result.
    with urllib.request.urlopen('http://127.0.0.1:9235/postprod/device-evidence',timeout=5) as response:live=json.load(response)
    if live.get('model_lifecycle')!='on_demand' or live.get('backend')!='torch_cuda_aux_v1' or not live.get('controller_process_id'):
        raise ValueError('COSY_CONTROLLER_POLICY_MISMATCH')
    from scheduler.account_profile import voice_for_preset
    out=work/'voice.wav'
    payload={'text':params['text'],'voice_id':voice_for_preset(params['preset']),'preset':params['preset'],'output_path':str(out)}
    request=urllib.request.Request('http://127.0.0.1:9235/tts',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+worker_token()})
    with urllib.request.urlopen(request,timeout=600) as response:result=json.load(response)
    qualified=result.get('device_evidence') or {};lifecycle=result.get('model_lifecycle') or {}
    if not qualified.get('passed') or qualified.get('cpu_neural_nodes',1)!=0 or qualified.get('backend')!='torch_cuda_aux_v1':
        raise ValueError('COSY_CUDA_AUXILIARY_UNQUALIFIED')
    if not lifecycle.get('execution_verified') or not lifecycle.get('launcher_process_id') or qualified.get('process_id')!=lifecycle.get('model_process_id') or not qualified.get('process_id'):
        raise ValueError('COSY_INFERENCE_PROCESS_MISMATCH')
    if lifecycle.get('state')!='SUCCEEDED' or not lifecycle.get('model_unloaded') or not lifecycle.get('process_exited') or not lifecycle.get('process_tree_closed'):
        raise ValueError('COSY_MODEL_UNLOAD_UNCONFIRMED')
    if not result.get('success') or str(out.resolve())!=str(__import__('pathlib').Path(result['audio_path']).resolve()) or not out.is_file():raise ValueError('COSY_RESULT_INVALID')
    atomic_json(work/'tts-receipt.json',result)
    return {'output_refs':[output_ref(out,'audio'),output_ref(work/'tts-receipt.json')],'metrics':{'duration_sec':result['duration_sec'],'sample_rate':result['sample_rate']},'device_evidence':dict(qualified,model_lifecycle=lifecycle),'provider_request_ids':[]}
