"""Operator-only independent script endpoints."""
from fastapi import Header, HTTPException, Request
from providers.ark_text_agent import load_config
from scheduler.expression_catalog import load_catalog
from scheduler.script_drafts import DraftStore
from scheduler.script_draft_generation import DraftGenerator


def install(app, main_store, gateway, operator):
    folder=main_store.path.parent
    app.state.script_drafts=DraftStore(folder/'script-drafts.sqlite',folder/'script-drafts')
    app.state.script_draft_generator=DraftGenerator(app.state.script_drafts,gateway)

    def call(fn,*args):
        try:return fn(*args)
        except ValueError as exc:
            message=str(exc)
            code=409 if message.startswith('409:') else 404 if message=='DRAFT_NOT_FOUND' else 422
            raise HTTPException(code,message) from exc

    @app.get('/v2/expression-entries')
    def expressions(req:Request):
        operator(req)
        catalog=call(load_catalog)
        return {'entries':[{k:e[k] for k in ('id','kind','category','text','context')} for e in catalog['entries']],
                'cost_limits':{'job_limit_micros':2000000,'daily_limit_micros':2000000,'max_paid_requests':load_config()['global_authorization']['requests']}}

    @app.get('/v2/script-drafts')
    def listing(req:Request):
        operator(req);return app.state.script_drafts.list()

    @app.post('/v2/script-drafts',status_code=201)
    def create(p:dict,req:Request,idempotency_key:str|None=Header(default=None)):
        operator(req)
        if not idempotency_key:raise HTTPException(400,'IDEMPOTENCY_KEY_REQUIRED')
        return call(app.state.script_drafts.create,p,idempotency_key)

    @app.get('/v2/script-drafts/{id}')
    def detail(id:str,req:Request):
        operator(req);return call(app.state.script_drafts.get,id)

    @app.post('/v2/script-drafts/{id}/generate')
    def generate(id:str,req:Request):
        operator(req);return call(app.state.script_draft_generator.generate,id)

    @app.post('/v2/script-drafts/{id}/confirm')
    def confirm(id:str,p:dict,req:Request):
        operator(req);return call(app.state.script_drafts.confirm,id,p.get('script_sha256'),p.get('operator_id'))

    @app.post('/v2/script-drafts/{id}/handoff')
    def handoff(id:str,req:Request):
        operator(req);return call(app.state.script_drafts.handoff,id)
