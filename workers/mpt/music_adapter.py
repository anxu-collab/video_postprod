import shutil
from pathlib import Path
from workers.common import input_path,digest,atomic_json,output_ref
def select(req,work):
    from app.services import bgm,video
    src=input_path(req['input_refs'][0]);root=Path(bgm.uploaded_bgm_dir());root.mkdir(parents=True,exist_ok=True)
    dst=root/(digest(src)+src.suffix.lower())
    if not dst.exists():shutil.copyfile(src,dst)
    if digest(dst)!=digest(src):raise ValueError('BGM_STAGING_HASH_MISMATCH')
    selected=video.get_bgm_file('specified',str(dst))
    if not selected or digest(selected)!=digest(src):raise ValueError('BGM_NATIVE_PATH_REJECTED')
    out=work/'music.json';atomic_json(out,{'path':selected,'sha256':digest(selected),'staging':'native uploaded BGM allowlist'})
    return {'output_refs':[output_ref(out)],'metrics':{},'provider_request_ids':[],'device_evidence':{'neural_inference':0}}
