/* Run only as a DevTools snippet on XATA, never on Cian. No requests or writes. */
(function(root){
'use strict';
function inspect(ids,state){
  if(!Array.isArray(ids)||!ids.length||ids.length>20||ids.some(id=>typeof id!=='string'||!/^\d{1,32}$/.test(id)))
    throw new Error('Specify 1 to 20 numeric Cian IDs');
  const targets=[...new Set(ids.map(id=>id.replace(/^0+(?=\d)/,'')))];
  if(!state||!state.ready)return {mode:'xata_ui_read_only',ready:false,reason:'page_not_loaded_or_unsupported',targets:[]};
  const items=[...state.items];
  return {mode:'xata_ui_read_only',ready:true,page:state.page,search_active:!!state.searchActive,
    status_filter:['all','new','liked','viewed','rejected','planned'].includes(state.filter)?state.filter:'unknown',
    targets:targets.map(id=>{
      const matches=items.filter(item=>[item.id,...(item.listing_ids||[])].some(value=>String(value)===id));
      return {id,loaded_matches:matches.map(item=>{
        const row=state.row(item),closed=row?.closest('details:not([open])');
        return {property_id:item.property_id??null,current_listing_id:item.id,
          historical_id:String(item.id)!==id,selected:!!item.in_our_list,section:state.section(item),
          matches_search:!!state.matchesSearch(item),row_connected:!!row?.isConnected,
          row_hidden:!!row?.hidden,ancestor_collapsed:!!closed,
          rendered:!!row?.isConnected&&!!row.getClientRects().length};
      }),reason:matches.length?'present_in_loaded_dataset':'not_in_loaded_page_dataset_not_proof_of_absent_import'};
    })};
}
function run(ids){
  const available=typeof records!=='undefined'&&typeof rows!=='undefined'&&typeof XataReview!=='undefined'
      &&typeof XataReview.section==='function'&&typeof XataReview.key==='function'&&typeof matchesSearch==='function';
  const page=typeof pageMode!=='undefined'?pageMode:root.document?.body?.dataset.page||'all';
  return inspect(ids,available?{
    ready:typeof listLoaded!=='undefined'?listLoaded:records.size>0,
    items:records.values(),page,
    searchActive:typeof searchText!=='undefined'?!!searchText:!!root.document?.querySelector('#listing-search')?.value,
    filter:typeof selectedFilter!=='undefined'?selectedFilter:'unknown',
    row:item=>rows.get(XataReview.key(item))?.row||rows.get(String(item.id))?.row,
    section:item=>XataReview.section(item,page),matchesSearch}:null);
}
if(typeof module!=='undefined'&&module.exports)module.exports={inspect,run};
else root.xataInspectListings=run;
})(globalThis);
