import os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
UPSTREAM=ROOT/'upstream/MoneyPrinterTurbo'
os.chdir(UPSTREAM);sys.path[:0]=[str(UPSTREAM),str(ROOT)]
os.environ['PATH']=str(ROOT/'tools/ffmpeg')+os.pathsep+os.environ.get('PATH','')
from workers.common import OPS,operation_app
from workers.mpt.components import handle as component_handle
def handle(req,work):
    if req['op']=='music.select':
        from workers.mpt.music_adapter import select
        return select(req,work)
    return component_handle(req,work)
def capabilities():
    return {'component':'mpt','ready':True,'operations':sorted(OPS['mpt']),'native_components':{'script':'generate_script','voice':'tts','material':'is_material_resolution_acceptable','music':'get_bgm_file'},'asr_model_present':(UPSTREAM/'models/whisper-small/model.bin').is_file(),'tts_qualified':(ROOT/'state/deployment/cosy-cuda-qualification.json').is_file(),'production_scheduler':False,'model_lifecycle':'on_demand','idle_models_loaded':False}
app=operation_app('mpt',handle,capabilities)
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=8761,log_level='warning')
