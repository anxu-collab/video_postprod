/* Independent script UX: source composition, cloud polish and checks are separate. */
window.initScriptDrafts=async function(){
  let current=null,catalog=[],limits=null,busy=false;
  const stage={created:'已保存，尚未生成',generating:'生成处理中',selecting:'正在判断表达适用性',writing:'正在写稿',reviewing:'正在审稿',ready_for_user_review:'待你确认',confirmed:'已确认',needs_review:'需要修改',needs_evidence:'需要补充证据',service_error:'服务未完成，请查看问题'};
  const reason={product_context_fit:'符合产品资料语境',expression_pattern_fit:'适合本次表达方式',product_context_conflict:'与本产品语境不符',information_missing:'产品信息不足',missing_evidence_selection:'缺少资料定位',request_budget_exhausted:'本轮判断额度已用完',context_budget_exceeded:'完整资料超过本次判断容量'};
  const status={applicable:'适用',not_applicable:'不适用',insufficient_information:'信息不足',not_evaluated:'未判定'};
  function message(text){$('dsMessage').textContent=text;}
  stage.polishing='程序已组装，云端正在受限润色';
  function isControlled(skill){return skill.id==='health-supplement-selling'&&['1.2.0','1.3.0','1.4.0'].includes(skill.version);}
  function failureReason(item){
    // Inspect audit codes only to select a user-facing explanation. Never render raw logs.
    const raw=[...(item.issues||[]),item.error_type||'',item.applicability&&item.applicability.error||''].join(' ');
    if(/CLEF_GPU_MEMORY_INSUFFICIENT/i.test(raw))return '本次表达判断未完成：可用显存不足。请待其他任务结束，确认资源可用后新建稿件；本稿不会自动重试。';
    if(item.applicability&&item.applicability.status==='service_error')return '本次表达判断未完成，尚不能写稿或确认。请先检查判断服务和失败记录，确认原因后再新建稿件。';
    if(/DRAFT_INPUT_BUDGET_EXCEEDED|SCRIPT_INPUT_BUDGET_EXCEEDED/.test(raw))return '完整资料与规则超过本次输入容量，内容没有被截断。请缩小主题或整理资料后保存新稿。';
    if(/SUBMISSION_UNKNOWN|SubmissionUnknown|USAGE_BOUND_UNVERIFIED|PROVIDER_EXCEEDED_RESERVED_BOUND|TimeoutError|ConnectionError|URLError|HTTPError|ARK_SECRET_MISSING/.test(raw))return '本次调用未能确认完成，付费结果尚未确认。请先核对请求和费用记录，不要重复提交；本稿不会自动重试。';
    if(/JOB_COST_CAP|DAILY_COST_CAP|DEPLOYMENT_COST_CAP|REQUEST_CAP|BudgetHold/.test(raw))return '本次生成未完成：可用额度不足，或已达到请求次数上限。请核对现有额度与费用记录后再处理，本稿不会自动重试。';
    if(/PAID_DISABLED/.test(raw))return '这份稿件尚未授权付费。请明确勾选生成额度后保存一份新稿。';
    if(/WRITER_|REVIEW_|MODEL_OUTPUT_EMPTY|DUPLICATE_MODEL_KEY|NONFINITE_MODEL_JSON|JSONDecodeError|EXPRESSION_REF_INVALID|FACT_REF_INVALID|FACT_EXCERPT_INVALID/.test(raw))return '模型返回格式或引用信息不完整，写稿与审稿尚未通过。请查看审计记录确认原因，处理后再新建稿件；本稿不会自动重试。';
    return '本次操作未完成，稿件尚未通过审核。请先检查服务和失败记录，确认原因后再处理；不会自动重试。';
  }
  function fail(e){
    const detail=String(e.message||e);
    if(/ASSEMBLY_PRODUCT_MISMATCH|ASSEMBLY_ACCOUNT_MISMATCH/.test(detail)){message('当前组装方案对应老周账号的天益清，产品编号为 tianyiqing。请核对所选账号和产品。');return;}
    if(/MANUAL_CONTROLLED_TEXT_OUTSIDE_ALLOWED_SCOPE/.test(detail)){message('人工修改超出了本次固定句或允许衔接范围，请先保留当前组成中的受保护文字。');return;}
    const local=/^(请选择账号|请选择本次口播 Skill|请填写产品编号、名称、完整资料和本期主题)$/.test(detail);
    message('需要处理：'+(local?detail:failureReason({issues:[detail]})));
  }
  function docRow(id='',text=''){
    const box=el('div',{},el('label',{},'资料编号',el('input',{value:id,placeholder:'例如 doc1'})),el('label',{},'完整产品资料',el('textarea',{value:text,rows:5})));
    $('dsDocuments').append(box);
  }
  function picked(){return [...$('dsCandidates').querySelectorAll('input:checked')].map(x=>x.value);}
  function freshInput(){
    if(!$('dsAccount').value)throw Error('请选择账号');
    if(!$('dsSkill').value)throw Error('请选择本次口播 Skill');
    const product={product_id:$('dsProductId').value.trim(),name:$('dsName').value.trim(),documents:[...$('dsDocuments').children].map(n=>({document_id:n.querySelector('input').value.trim(),text:n.querySelector('textarea').value}))};
    if(!product.product_id||!product.name||!$('dsBrief').value.trim()||product.documents.some(d=>!d.document_id||!d.text.trim()))throw Error('请填写产品编号、名称、完整资料和本期主题');
    const skill=JSON.parse($('dsSkill').value),ids=picked(),controlled=isControlled(skill);
    return {draft_id:crypto.randomUUID(),account:$('dsAccount').value,product,skill,brief:$('dsBrief').value,...(controlled?{assembly_recipe:({'1.2.0':'tianyiqing-r07-v1','1.3.0':'tianyiqing-r07-v2','1.4.0':'tianyiqing-r07-v3'})[skill.version]}:ids.length?{entry_ids:ids}:{}),cost:{...limits,paid_enabled:$('dsPaid').checked}};
  }
  function sourceInput(){
    const p=current.input;return {draft_id:crypto.randomUUID(),parent_id:current.draft_id,account:p.account,product:p.product,skill:p.skill,brief:p.brief,...(p.assembly_recipe?{assembly_recipe:p.assembly_recipe}:{}),...(p.entry_ids?{entry_ids:p.entry_ids}:{}),cost:{...p.cost,paid_enabled:$('dsPaid').checked}};
  }
  function buttons(){
    const dirty=current&&$('dsText').value!==(current.script_text||'');
    $('dsGenerate').disabled=busy||!current||current.status!=='created'||!current.input.cost.paid_enabled;
    $('dsConfirm').disabled=busy||!current||dirty||current.status!=='ready_for_user_review';
    $('dsHandoff').disabled=busy||!current||dirty||current.status!=='confirmed';
    $('dsEdit').disabled=busy||!current||!$('dsText').value.trim();
    $('dsRewrite').disabled=busy||!current;
    $('dsCreate').disabled=busy;
  }
  function render(item){
    current=item;$('dsDetail').hidden=false;$('dsTitle').textContent=item.input.product.name+' · '+item.input.account+' · '+item.input.skill.version;
    $('dsStage').textContent=(stage[item.status]||'待处理')+(item.status==='created'&&!item.input.cost.paid_enabled?'；该稿未授权付费，勾选额度后保存新稿再生成。':'');$('dsText').value=item.script_text||item.input.script_text||'';
    const issues=item.status==='service_error'?[failureReason(item)]:(item.issues||[]);
    $('dsIssues').replaceChildren(...issues.map(x=>el('p',{},x)));
    const a=item.applicability;$('dsExpressions').replaceChildren();
    const assembly=item.assembly||item.input.assembly_snapshot;
    if(assembly){
      const box=el('details',{open:true},el('summary',{},'话术库来源 → 程序组装 → 云端润色 → 检查'));
      box.append(el('p',{},assembly.narrative_type));
      for(const s of assembly.segments){
        box.append(el('p',{},s.label+' · '+s.sources.map(r=>r.id).join(' + ')));
        box.append(el('p',{},'库中原句：'+s.sources.map(r=>r.original).join('')));
        box.append(el('p',{},'程序组成：'+s.text));
        const rows=item.raw_polish&&item.raw_polish.segments;
        const polished=(Array.isArray(rows)?rows:[]).find(r=>r&&r.id===s.id);
        if(polished)box.append(el('p',{},'云端返回：'+polished.text));
      }
      if(assembly.polish_mode==='open_middle'&&item.raw_polish&&Array.isArray(item.raw_polish.segments)){
        box.append(el('p',{},'云端重组后的完整口播（中段可合句、拆句和调序）：'));
        for(const row of item.raw_polish.segments){if(row&&typeof row.text==='string')box.append(el('p',{},row.text));}
      }
      box.append(el('p',{},item.checks?(item.checks.passed?'文字控制检查通过；事实与衔接另审。':'文字控制检查未通过，云端返回未采用。'):'已组装，尚未调用云端。'));
      $('dsExpressions').append(box);
      if(assembly.repair_policy&&item.rounds){
        const history=el('details',{},el('summary',{},'查看审稿返修过程（'+item.rounds.length+'轮）'));
        for(const round of item.rounds){
          history.append(el('p',{},'第'+(round.round+1)+'轮'));
          const attemptRows=round.output&&round.output.segments;
          for(const row of Array.isArray(attemptRows)?attemptRows:[])if(row&&typeof row.text==='string')history.append(el('p',{},row.text));
          for(const target of round.repair_targets||[])history.append(el('p',{},'返修意见：'+target.issue+'；'+target.instruction));
        }
        if(item.repair_stop_reason==='repair_limit_reached')history.append(el('p',{},'已达到本次返修上限，剩余问题保留，未自动确认。'));
        $('dsExpressions').append(history);
      }
      if(item.status==='created')$('dsText').value=item.input.script_text||assembly.script_text;
    }
    if(a){
      const gap=a.coverage&&a.coverage.not_evaluated_ids||[];
      $('dsExpressions').append(el('p',{},'适用表达 '+(a.selected_ids||[]).length+' 条；未判定 '+gap.length+' 条。未判定不等于不适用。写稿仅使用少量适用词卡，未用不等于不适用。'));
      const list=el('details',{},el('summary',{},'查看表达与判定说明'));
      for(const d of a.decisions||[]){const e=catalog.find(x=>x.id===d.entry_id);list.append(el('p',{},(e?e.text:d.entry_id)+' — '+(status[d.status]||d.status)+'；'+(reason[d.reason_code]||'请结合完整资料复核')));}
      $('dsExpressions').append(list);
    }
    buttons();
  }
  async function listing(){const rows=await api('/v2/script-drafts');$('dsList').replaceChildren(...rows.map(d=>el('button',{className:'secondary',onclick:()=>{render(d);message('已载入保存的稿件。');}},d.input.product.name+' · '+d.input.account+' · '+(stage[d.status]||d.status)+' · '+(d.created_utc?new Date(d.created_utc).toLocaleString():''))));}
  async function create(p){const d=await api('/v2/script-drafts',p,{'Idempotency-Key':p.draft_id});render(d);await listing();message('已保存，尚未使用生成额度。');}
  async function action(fn){try{busy=true;buttons();await fn();}catch(e){fail(e);}finally{busy=false;buttons();}}
  $('dsCreate').onclick=()=>action(()=>create(freshInput()));
  $('dsAddDocument').onclick=()=>docRow('doc'+($('dsDocuments').children.length+1));
  $('dsRefresh').onclick=()=>action(listing);
  $('dsText').oninput=buttons;
  $('dsEdit').onclick=()=>action(()=>{const p=sourceInput();p.script_text=$('dsText').value;return create(p);});
  $('dsRewrite').onclick=()=>action(()=>create(sourceInput()));
  $('dsGenerate').onclick=()=>action(async()=>{
    const id=current.draft_id;message(isControlled(current.input.skill)?'正在对程序组装稿做受限润色和同源审稿，使用已保存额度。':'正在判断表达、写稿与同源审稿；本次按钮允许使用已保存的额度。');
    const timer=setInterval(async()=>{try{const d=await api('/v2/script-drafts/'+id);if(current.draft_id===id)$('dsStage').textContent=stage[d.status]||'处理中';}catch{}},2000);
    try{render(await api('/v2/script-drafts/'+id+'/generate',{}));await listing();message('处理结束，请查看正文和问题。');}finally{clearInterval(timer);}
  });
  $('dsConfirm').onclick=()=>action(async()=>{render(await api('/v2/script-drafts/'+current.draft_id+'/confirm',{script_sha256:current.script_sha256,operator_id:'local-operator'}));await listing();message('这版正文已确认。');});
  $('dsHandoff').onclick=()=>action(async()=>{const h=await api('/v2/script-drafts/'+current.draft_id+'/handoff',{});window.acceptDraftHandoff(h);message('已填写新建视频表单；请继续选择本期素材。');});
  try{
    const [accounts,skills,expressions]=await Promise.all([api('/v2/accounts'),api('/v2/script-skills'),api('/v2/expression-entries')]);
    for(const a of accounts.filter(a=>!a.error))$('dsAccount').append(el('option',{value:a.account},a.display_name+'（'+a.account+'）'));
    for(const s of skills.slice().sort((a,b)=>b.version.localeCompare(a.version)))$('dsSkill').append(el('option',{value:JSON.stringify({id:s.id,version:s.version})},s.name+' · '+s.version));
    $('dsSkill').onchange=()=>{const controlled=$('dsSkill').value&&isControlled(JSON.parse($('dsSkill').value));$('dsCandidates').closest('details').hidden=!!controlled;$('dsRecipeNote').hidden=!controlled;};
    catalog=expressions.entries;limits=expressions.cost_limits;
    $('dsCandidates').replaceChildren(...catalog.map(e=>el('label',{},el('input',{type:'checkbox',value:e.id}),e.text)));
    docRow('doc1');await listing();buttons();
  }catch(e){fail(e);$('dsCreate').disabled=true;}
};
