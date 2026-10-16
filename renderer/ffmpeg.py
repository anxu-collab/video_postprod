import hashlib,json,os,subprocess,time
from fractions import Fraction
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
FFMPEG=ROOT/'tools/ffmpeg/ffmpeg.exe';FFPROBE=ROOT/'tools/ffmpeg/ffprobe.exe'

def execute(args,cwd,timeout=300,verbose=False):
    # This FFmpeg build can exit zero after refusing an existing output with -n.
    output=str(args[-1]) if args else '-'
    if output!='-':
        candidate=Path(output)
        if not candidate.is_absolute():candidate=Path(cwd)/candidate
        if candidate.exists():raise RuntimeError('OUTPUT_ALREADY_EXISTS:'+str(candidate))
    command=[str(FFMPEG),'-nostdin','-n','-v','info' if verbose else 'error',*map(str,args)]
    p=subprocess.run(command,cwd=cwd,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout,shell=False)
    if p.returncode:raise RuntimeError('RENDER_FAILED:'+p.stderr[-3000:])
    return p

def probe(path):
    p=subprocess.run([str(FFPROBE),'-v','error','-show_streams','-show_format','-of','json',str(path)],capture_output=True,text=True,check=True,timeout=30,shell=False)
    q=json.loads(p.stdout)
    if not q.get('streams'):raise ValueError('MEDIA_PROBE_INVALID')
    duration=Fraction(q.get('format',{}).get('duration','0'))
    if duration<=0:raise ValueError('MEDIA_DURATION_INVALID')
    q['duration_ticks']=round(duration*48000)
    starts=[Fraction(s.get('start_time','0')) for s in q['streams'] if s['codec_type'] in ['audio','video']]
    q['epoch_ticks']=round(min(starts or [Fraction(0)])*48000)
    q['stream_indices']={k:next((s['index'] for s in q['streams'] if s['codec_type']==k),None) for k in ['video','audio']}
    return q

def decode(path,work,timeout=300):
    return execute(['-i',path,'-map','0:v?','-map','0:a?','-f','null','-'],work,timeout)

def content_hash(data):
    return hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def render_cached(kind,inputs,params,args_factory,extension,work):
    from workers.common import digest
    manifest=json.loads((ROOT/'tools/ffmpeg/manifest.json').read_text(encoding='utf-8'))
    key=content_hash({'kind':kind,'inputs':[digest(p) for p in inputs],'params':params,'ffmpeg':manifest['files']['ffmpeg.exe'],'compiler':'v1.0.0'})
    folder=ROOT/'artifacts/sha256'/key[:2];folder.mkdir(parents=True,exist_ok=True)
    final=folder/(key+extension);record=folder/(key+'.manifest.json')
    if final.exists() and record.exists():
        m=json.loads(record.read_text(encoding='utf-8'))
        if m['sha256']==digest(final):return final,True
        raise RuntimeError('IMMUTABLE_CACHE_CORRUPTED')
    partial=Path(work)/(kind.replace('.','_')+'_'+key[:12]+'.partial'+extension)
    if partial.exists():partial.unlink() # Only this new attempt's unpublished output.
    result=execute(args_factory(partial),work,timeout=max(120,params.get('duration_ticks',48000)//2400))
    decode(partial,work)
    sha=digest(partial);os.replace(partial,final)
    from workers.common import atomic_json
    atomic_json(record,{'node_key':key,'kind':kind,'sha256':sha,'path':str(final),'params':params})
    return final,False
