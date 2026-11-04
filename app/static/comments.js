/* One comment editor per property: refresh updates data, never live drafts. */
(function(root){
'use strict';
function create({kind,getItem,getUser,request,jsonOptions,accept,refresh,render,ensureDetails}) {
  const family=kind==='family', prefix=family?'family':'realtor';
  const cells=new Map();
  function text(node,value){const s=String(value??'');if(node.textContent!==s)node.textContent=s;}
  function stamp(author,date){return (author||'Система')+(date?' · '+new Date(date).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}):'');}
  function button(label,cls){const b=document.createElement('button');b.type='button';b.className=cls;b.textContent=label;return b;}
  function make(key) {
    const host=document.createElement('div');host.className='property-comment '+prefix+'-block';
    const display=document.createElement('div');display.className='comment-display';
    const badge=document.createElement('span');badge.className=family?'family-unused':'realtor-label';badge.hidden=family;
    const body=document.createElement('p');body.className='comment-body '+(family?'family-comment-text':'comment-preview');
    const more=button('Читать полностью','comment-more secondary');
    const meta=document.createElement('small');meta.className=prefix+'-meta';
    const edit=button('Добавить комментарий',prefix+'-edit secondary');
    const loadError=document.createElement('p');loadError.className='error comment-load-error';loadError.setAttribute('role','status');
    display.append(badge,body,more,meta,edit,loadError);
    const form=document.createElement('form');form.className=prefix+'-editor comment-editor';form.hidden=true;
    const status=document.createElement('select');status.className=family?'family-unused':'realtor-select';status.hidden=family;
    for(const [value,label] of [['pending','Не оценено'],['approved','Одобрено'],['rejected','Отбраковано']]){
      const option=document.createElement('option');option.value=value;option.textContent=label;status.append(option);
    }
    const input=document.createElement('textarea');input.className=prefix+'-comment';input.rows=4;input.maxLength=family?20000:2000;
    const actions=document.createElement('div');actions.className='comment-actions';
    const save=button('Сохранить','comment-save');save.type='submit';
    const cancel=button('Отмена','comment-cancel secondary');
    const error=document.createElement('p');error.className='error comment-error';error.setAttribute('role','status');
    const conflict=document.createElement('div');conflict.className='comment-conflict';conflict.hidden=true;
    const remote=document.createElement('p');remote.className='comment-remote';
    const rebase=button('Продолжить с моим черновиком','comment-rebase secondary');
    conflict.append(remote,rebase);actions.append(save,cancel);form.append(...(family?[]:[status]),input,actions,error,conflict);
    const history=document.createElement('details');history.className='comment-history';
    const summary=document.createElement('summary');summary.textContent='История';
    const entries=document.createElement('div');history.append(summary,entries);
    host.append(display,form,history);
    const state={host,display,badge,body,more,meta,edit,form,status,input,save,cancel,error,conflict,remote,rebase,history,entries,editing:false,pending:false,dirty:false,expanded:false,baseVersion:0,baseProperty:null,historySignature:null,loading:false};
    cells.set(key,state);
    async function load(){text(loadError,'Загрузка…');state.loading=true;update(key);try{const result=await ensureDetails(key,true);text(loadError,'');return result;}catch(e){text(loadError,e.message);return null;}finally{state.loading=false;update(key);}}
    more.addEventListener('click',async()=>{if(state.loading)return;if(!state.expanded&&!getItem(key)?.details_loaded&&!await load())return;state.expanded=!state.expanded;update(key);});
    history.addEventListener('toggle',async()=>{if(history.open&&!state.loading&&!getItem(key)?.details_loaded)await load();});
    edit.addEventListener('click',async()=>{
      if(state.loading)return;
      const item=await load();if(!item||!item.details_loaded)return;
      state.editing=true;state.dirty=false;state.baseProperty=item.property_id;
      state.baseVersion=item[family?'family_comment_version':'realtor_version']||0;
      input.value=item[family?'family_comment':'realtor_comment']||'';
      status.value=item.realtor_status||'pending';text(error,'');update(key);input.focus();
    });
    input.addEventListener('input',()=>{state.dirty=true;});
    status.addEventListener('change',()=>{state.dirty=true;});
    cancel.addEventListener('click',()=>{
      if(state.pending)return;state.editing=false;state.dirty=false;text(error,'');update(key);render();
    });
    rebase.addEventListener('click',()=>{
      const item=getItem(key);if(!item)return;
      state.baseVersion=item[family?'family_comment_version':'realtor_version']||0;
      state.baseProperty=item.property_id;text(error,'Актуальная версия выбрана. Проверьте черновик и нажмите «Сохранить».');update(key);
    });
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(state.pending)return;
      const item=getItem(key);if(!item)return;
      const payload=family?{property_id:state.baseProperty,family_comment_version:state.baseVersion,text:input.value}
        :{property_id:state.baseProperty,realtor_version:state.baseVersion,status:status.value,comment:input.value};
      state.pending=true;text(error,'Сохраняем…');update(key);
      try{
        const saved=await request('/api/listings/'+encodeURIComponent(item.id)+(family?'/family-comment':'/realtor-review'),jsonOptions(family?'PATCH':'POST',payload));
        accept(saved);state.editing=false;state.dirty=false;text(error,'');
      }catch(exc){
        text(error,exc.message);
        try{await refresh();}catch(_){/* Keep draft and original error. */}
      }finally{state.pending=false;update(key);render();}
    });
    return state;
  }
  function update(key) {
    const item=getItem(key);if(!item)return null;
    const s=cells.get(key)||make(key),version=item[family?'family_comment_version':'realtor_version']||0;
    const value=item[family?'family_comment':'realtor_comment']||'';
    const canEdit=family?['admin','reviewer'].includes(getUser()?.role):['admin','realtor'].includes(getUser()?.role);
    s.display.hidden=s.editing;s.form.hidden=!s.editing;
    text(s.body,value||'—');s.body.classList.toggle('comment-collapsed',!s.expanded);
    const measure=()=>{s.more.hidden=!value||(!item[family?'family_comment_truncated':'realtor_comment_truncated']&&!s.expanded&&s.body.scrollHeight<=s.body.clientHeight+1);};
    text(s.more,s.expanded?'Свернуть':'Читать полностью');
    requestAnimationFrame(measure);
    if(!s.resize&&typeof ResizeObserver!=='undefined'){s.resize=new ResizeObserver(()=>{if(!s.editing){const v=getItem(key)?.[family?'family_comment':'realtor_comment']||'';s.more.hidden=!v||(!getItem(key)?.[family?'family_comment_truncated':'realtor_comment_truncated']&&!s.expanded&&s.body.scrollHeight<=s.body.clientHeight+1);}});s.resize.observe(s.body);}
    const date=item[family?'family_comment_updated_at':'realtor_updated_at'];
    const author=family?item.family_comment_updated_by_name:(item.realtor_updated_by_name||item.realtor_updated_by_username);
    text(s.meta,date?stamp(author,date):'');s.meta.hidden=!date;
    const status=item.realtor_status||'pending';
    text(s.badge,status==='rejected'&&item.in_our_list?'Отбраковано риелтором':({pending:'Не оценено',approved:'Одобрено',rejected:'Отбраковано'}[status]));
    if(!family)s.badge.className='realtor-label realtor-'+status;
    s.edit.disabled=s.loading;s.more.disabled=s.loading;
    s.edit.hidden=!canEdit||(family&&(!document.body.matches('[data-page=ours]')||!item.in_our_list));
    text(s.edit,value?'Изменить':'Добавить комментарий');
    s.input.setAttribute('aria-label',(family?'Наш комментарий ':'Комментарий риелтора ')+item.id);
    s.status.setAttribute('aria-label','Оценка риелтора '+item.id);
    // Do not set .value or replace any editor node while editing.
    s.input.disabled=s.status.disabled=s.save.disabled=!canEdit||s.pending;
    s.cancel.disabled=s.pending;
    const changed=s.editing&&(version!==s.baseVersion||item.property_id!==s.baseProperty);
    s.conflict.hidden=!changed;
    if(changed)text(s.remote,'Актуально: '+(value||'Комментарий очищен')+(!family?' · '+({pending:'Не оценено',approved:'Одобрено',rejected:'Отбраковано'}[status]):''));
    const history=(item.comment_history||[]).filter(h=>h.kind===kind);
    const signature=JSON.stringify(history);
    if(signature!==s.historySignature){
      s.entries.replaceChildren();
      for(const h of history){
        const entry=document.createElement('div');entry.className='comment-history-entry';
        const meta=document.createElement('small');meta.textContent=stamp(h.changed_by_name,h.changed_at)+' · ЦИАН '+(h.origin_listing_id||'—')+' · property '+h.origin_property_id;
        const p=document.createElement('p');p.textContent=h.text||'Комментарий очищен';
        if(h.status){const statusText=document.createElement('strong');statusText.textContent=({pending:'Не оценено',approved:'Одобрено',rejected:'Отбраковано'}[h.status]||h.status);entry.append(statusText);}
        entry.append(meta,p);s.entries.append(entry);
      }
      s.historySignature=signature;
    }
    s.history.hidden=!(history.length||item.comment_history_count?.[kind])||s.editing;
    return s;
  }
  return {update,element:key=>update(key).host,editing:key=>!!cells.get(key)?.editing,dirty:key=>!!cells.get(key)?.dirty,needsDetails:key=>{const s=cells.get(key);return !!s&&!s.editing&&(s.history.open||s.expanded);}};
}
root.XataComments={create};
})(globalThis);
