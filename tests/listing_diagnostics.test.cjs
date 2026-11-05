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
