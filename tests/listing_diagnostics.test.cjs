const test=require('node:test');
const assert=require('node:assert/strict');
const diag=require('../scripts/diagnose_ui.js');
const review=require('../app/static/review-state.js');
test('UI diagnosis distinguishes historical, collapsed, filtered and unloaded IDs without mutation',()=>{
  const items=[{id:'456',property_id:2,listing_ids:['123','456'],in_our_list:true,realtor_status:'rejected',review_status:'liked'}];
  const before=JSON.stringify(items);
  const row={isConnected:true,hidden:true,closest:()=>({open:false}),getClientRects:()=>[]};
  const result=diag.inspect(['123','999'],{ready:true,items,page:'all',searchActive:true,filter:'liked',
    row:()=>row,section:i=>review.section(i,'all'),matchesSearch:()=>false});
  const current=result.targets[0].loaded_matches[0];
  assert.equal(current.current_listing_id,'456');assert.equal(current.historical_id,true);
  assert.equal(current.section,'refused');assert.equal(current.ancestor_collapsed,true);
  assert.equal(current.matches_search,false);assert.equal(current.rendered,false);
  assert.match(result.targets[1].reason,/not_proof/);
  assert.equal(JSON.stringify(items),before);
  assert.equal(diag.inspect(['123'],null).ready,false);
  assert.throws(()=>diag.inspect(['https://cian.ru'],null));
});

test('current loaded rows remain inspectable in the older UI without listLoaded',()=>{
  const names=['records','rows','XataReview','matchesSearch','pageMode','selectedFilter','searchText'];
  const old=Object.fromEntries(names.map(name=>[name,Object.getOwnPropertyDescriptor(globalThis,name)]));
  try{
    const item={id:'123',property_id:1,listing_ids:['123'],review_status:'new'};
    globalThis.records=new Map([['property:1',item]]);
    globalThis.rows=new Map([['property:1',{row:{isConnected:true,hidden:false,closest:()=>null,getClientRects:()=>[{}]}}]]);
    globalThis.XataReview=review;globalThis.matchesSearch=()=>true;
    globalThis.pageMode='all';globalThis.selectedFilter='all';globalThis.searchText='';
    assert.equal(diag.run(['123']).targets[0].loaded_matches[0].rendered,true);
    globalThis.records.clear();
    assert.equal(diag.run(['123']).ready,false); // Empty legacy state cannot prove initial load finished.
  }finally{for(const name of names){if(old[name])Object.defineProperty(globalThis,name,old[name]);else delete globalThis[name];}}
});
