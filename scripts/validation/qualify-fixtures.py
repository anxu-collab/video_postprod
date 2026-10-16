import json,sys,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from renderer.ffmpeg import probe,execute
from scheduler.db import Store
from scheduler.worker_client import operation,request
from workers.common import digest,atomic_json,output_ref
store=Store(ROOT/'state/jobs.sqlite');target=ROOT/'jobs/deployment-fixtures';target.mkdir(parents=True,exist_ok=True)
files={'a1':'x_dragonfruit_broll_20261010/g01/6b7b4fad791fc3ed/video.mp4','a2':'x_dragonfruit_broll_20261010/g02/0c3e940a5b998bec/video.mp4','b1':'x_dragonfruit_host_20261010/host_v06a/cb1b5e3188ea0eda/video.mp4'}
inventory={}
for key,name in files.items():
    src=ROOT.parent/'runs/h3/h3_runs'/name;sha=digest(src);dst=ROOT/'artifacts/imports'/sha[:2]/(sha+'.mp4');dst.parent.mkdir(parents=True,exist_ok=True)
    if not dst.exists():shutil.copyfile(src,dst)
    assert digest(dst)==sha
    q=probe(dst);atomic_json(target/(key+'-probe.json'),q);inventory[key]={'path':str(dst),'sha256':sha,'probe':q}
    print(key,round(q['duration_ticks']/48000,3),[(s['index'],s['codec_type'],s.get('start_time'),s.get('color_space')) for s in q['streams']],flush=True)
atomic_json(target/'inventory.json',inventory)
b=inventory['b1'];wav=target/'b-source.wav'
if not wav.exists():execute(['-i',b['path'],'-map','0:a:0','-c:a','pcm_s16le','-ar','48000','-ac','2',wav],target)
result,cached=operation(store,'mpt','speech.transcribe',[output_ref(wav,'audio')],{})
atomic_json(target/'b-asr-operation.json',dict(result,cached=cached));print(json.dumps(result,ensure_ascii=False),flush=True)
