const assert = require('node:assert/strict');
const {merge, ordered} = require('../app/static/review-state.js');
const saved = {id:'123', price:10, first_seen:'2020', review_status:'rejected', review_version:4, review_updated_by:7, review_updated_at:'2026', review_updated_by_name:'Риелтор'};
const stale = {id:'123', price:9, first_seen:'2020', review_status:'new', review_version:3, review_updated_by:null};
const merged = merge(saved, stale);
assert.equal(merged.review_status, 'rejected');
assert.equal(merged.review_updated_by, 7);
assert.equal(merged.review_updated_at, '2026');
assert.equal(merged.price, 9);
assert.equal(merge(saved, {...stale, review_version:5, review_status:'viewed'}).review_status, 'viewed');
assert.equal(merge(saved, {...saved, review_updated_by_name:'Переименован'}).review_updated_by_name, 'Переименован');
assert.deepEqual(ordered([
  saved, {id:'2',review_status:'viewed',first_seen:'2030'},
  {id:'3',review_status:'new',first_seen:'2020'},
  {id:'4',review_status:'new',first_seen:'2025'}
]).map(item=>item.id), ['4','3','2','123']);
console.log('Review state ordering and stale-response regression checks passed');

const test = require('node:test');
const state = require('../app/static/review-state.js');
test('independent realtor and common revisions survive stale polls', () => {
  const previous = {...saved,review_status:'liked',realtor_version:5,realtor_status:'approved',realtor_comment:'Сохранённый текст',realtor_updated_by:8};
  let result = state.merge(previous,{...stale,realtor_version:4,realtor_status:'pending',realtor_comment:''});
  assert.equal(result.review_status,'liked');
  assert.equal(result.realtor_status,'approved');
  assert.equal(result.realtor_comment,'Сохранённый текст');
  assert.equal(result.realtor_updated_by,8);
  assert.equal(result.price,9);
  result = state.merge(previous,{...stale,review_version:6,review_status:'new',realtor_version:4});
  assert.equal(result.review_status,'new'); assert.equal(result.realtor_status,'approved');
  result = state.merge(previous,{...stale,review_version:3,realtor_version:6,realtor_status:'rejected'});
  assert.equal(result.review_status,'liked'); assert.equal(result.realtor_status,'rejected');
});
test('liked order and filter counts', () => {
  const items=[{id:'1',review_status:'rejected'},{id:'2',review_status:'liked',first_seen:'2020-01-01'},
    {id:'3',review_status:'viewed'},{id:'4',review_status:'new'},{id:'5',review_status:'liked',first_seen:'2025-01-01'}];
  assert.deepEqual(state.ordered(items).map(x=>x.id),['4','5','2','3','1']);
  assert.deepEqual(state.counts(items),{all:5,new:1,liked:2,viewed:1,rejected:1});
  assert.deepEqual(state.ordered(items.filter(x=>x.review_status==='liked')).map(x=>x.id),['5','2']);
});
test('either rejection controls presentation without overwriting independent decisions', () => {
  for (const review_status of ['new','liked','viewed','rejected']) {
    for (const realtor_status of ['pending','approved','rejected']) {
      const item = Object.freeze({id:'1',review_status,realtor_status});
      assert.equal(state.effectiveStatus(item),realtor_status === 'rejected' ? 'rejected' : review_status);
      assert.equal(item.review_status,review_status);
      assert.equal(item.realtor_status,realtor_status);
    }
  }
  const items = [
    {id:'1',review_status:'liked',realtor_status:'rejected',first_seen:'2030-01-01'},
    {id:'2',review_status:'new',realtor_status:'approved'},
    {id:'3',review_status:'viewed',realtor_status:'pending'},
    {id:'4',review_status:'rejected',realtor_status:'approved'}
  ];
  assert.deepEqual(state.counts(items),{all:4,new:1,liked:0,viewed:1,rejected:2});
  assert.deepEqual(state.ordered(items).map(x=>x.id),['2','3','1','4']);
  assert.deepEqual(state.ordered(items,{key:'review_status',direction:'asc'}).map(x=>x.id),['2','3','1','4']);
  assert.equal(state.effectiveStatus({...items[0],realtor_status:'approved'}),'liked');
  assert.equal(state.effectiveStatus({...items[0],realtor_status:'pending'}),'liked');
  assert.equal(state.effectiveStatus({}),'new');
});
test('numeric sorting, nulls last both directions, mortgage exact sort', () => {
  const items=[{id:'10',price:null,area:null},{id:'2',price:14000000,area:40},{id:'3',price:9500000,area:9.5}];
  for (const key of ['id','price','area','mortgage']) {
    const expected=key==='id'?['2','3','10']:['3','2','10'];
    assert.deepEqual(state.ordered(items,{key,direction:'asc'}).map(x=>x.id),expected);
    const descending=key==='id'?['10','3','2']:['2','3','10'];
    assert.deepEqual(state.ordered(items,{key,direction:'desc'}).map(x=>x.id),descending);
  }
  const close=[{id:'1',price:10000100},{id:'2',price:10000001}];
  assert.deepEqual(state.ordered(close,{key:'mortgage',direction:'asc'}).map(x=>x.id),['2','1']);
});
test('sort dates by instant, floors, metro durations, text and both statuses', () => {
  const items=[
    {id:'1',first_seen:'2026-01-01T10:00:00+03:00',floor:'10/12',metro_time:'12 мин. транспортом',review_status:'liked',realtor_status:'approved',metro:'Ясенево',title:'Ясный'},
    {id:'2',first_seen:'2026-01-01T08:00:00Z',floor:'2/9',metro_time:'7 мин. пешком',review_status:'new',realtor_status:'pending',metro:'Аэропорт',title:'Астра'},
    {id:'3',first_seen:null,floor:null,metro_time:'4 часа назад сегодня',review_status:'rejected',realtor_status:'rejected'}
  ];
  assert.deepEqual(state.ordered(items,{key:'first_seen',direction:'asc'}).map(x=>x.id),['1','2','3']);
  for (const key of ['floor','metro_time','metro','object','review_status','realtor_status'])
    assert.deepEqual(state.ordered(items,{key,direction:'asc'}).map(x=>x.id),['2','1','3']);
  for (const key of ['review_status','realtor_status'])
    assert.deepEqual(state.ordered(items,{key,direction:'desc'}).map(x=>x.id),['3','1','2']);
});
test('third click restores default ordering; switching columns starts ascending', () => {
  let sort=state.cycleSort(null,'price');
  assert.deepEqual(sort,{key:'price',direction:'asc'});
  sort=state.cycleSort(sort,'price'); assert.equal(sort.direction,'desc');
  sort=state.cycleSort(sort,'price'); assert.equal(sort,null);
  assert.deepEqual(state.cycleSort({key:'price',direction:'desc'},'area'),{key:'area',direction:'asc'});
  const items=[{id:'1',review_status:'rejected',price:1},{id:'2',review_status:'new',price:2}];
  assert.deepEqual(state.ordered(items,sort).map(x=>x.id),['2','1']);
});
test('annuity uses 6m down, 18 percent and 180 months', () => {
  assert.equal(state.mortgage(null),null);
  assert.equal(state.mortgage(5000000),0); assert.equal(state.mortgage(6000000),0);
  assert.ok(Math.abs(state.mortgage(12000000)-96625.26236476943)<0.00001);
  assert.ok(state.mortgage(13000000)>state.mortgage(12000000));
});

test('property key survives a new Cian ID, with independent review revisions', () => {
  const old={id:'100',property_id:37,review_status:'liked',review_version:9,realtor_status:'approved',realtor_comment:'Сохранено',realtor_version:5};
  const incoming={id:'200',property_id:37,price:12500000,review_status:'new',review_version:8,realtor_status:'pending',realtor_version:4};
  const next=state.reconcile(new Map([[state.key(old),old]]),[incoming],1,2);
  assert.equal(next.size,1);
  const item=next.get(state.key(old));
  assert.equal(item.id,'200'); assert.equal(item.price,12500000);
  assert.equal(item.review_status,'liked'); assert.equal(item.realtor_comment,'Сохранено');
});
test('a delayed state cannot resurrect properties after manual merge', () => {
  const a={id:'1',property_id:1},b={id:'2',property_id:2};
  const merged={...b,property_id:1,review_status:'liked',review_version:100};
  const before=new Map([[state.key(a),a],[state.key(b),b]]);
  const after=state.reconcile(before,[merged],10,11);
  assert.equal(after.size,1);
  const stale=state.reconcile(after,[a,b],11,10);
  assert.deepEqual([...stale],[...after]);
});

test('viewing versions survive delayed polls and nearest appointment ignores completed and cancelled', () => {
  const visits=[{id:1,status:'completed',scheduled_at:'2026-10-01T10:00:00Z'},
    {id:2,status:'planned',scheduled_at:'2026-11-02T10:00:00Z'},
    {id:3,status:'planned',scheduled_at:'2026-11-01T18:30:00+03:00'},
    {id:4,status:'cancelled',scheduled_at:'2026-10-01T10:00:00Z'}];
  const previous={id:'1',visits_version:5,visits};
  const merged=state.merge(previous,{id:'1',visits_version:4,visits:[],price:123});
  assert.deepEqual(merged.visits,visits);assert.equal(merged.price,123);
  assert.equal(state.nextVisit(merged).id,3);
  assert.equal(state.nextVisit({}),null);
  assert.deepEqual(state.merge(previous,{id:'1',visits_version:6,visits:[]}).visits,[]);
});

test('viewing date sorts exactly the displayed appointment or latest completion, nulls always last',()=>{
  const items=[
    {id:'1',visits:[{id:1,status:'completed',scheduled_at:'2030-01-01T10:00:00Z',completed_at:'2026-10-01T10:00:00Z'},{id:2,status:'planned',scheduled_at:'2026-11-01T10:00:00Z'}]},
    {id:'2',visits:[{id:3,status:'completed',completed_at:'2026-10-02T18:00:00+03:00'},{id:4,status:'cancelled',scheduled_at:'2031-01-01T10:00:00Z'}]},
    {id:'3',visits:[{id:5,status:'cancelled',scheduled_at:'2025-01-01T10:00:00Z'}]},
  ];
  assert.deepEqual(state.ordered(items,{key:'visit_date',direction:'asc'}).map(i=>i.id),['2','1','3']);
  assert.deepEqual(state.ordered(items,{key:'visit_date',direction:'desc'}).map(i=>i.id),['1','2','3']);
  assert.equal(state.displayVisit(items[0]).id,2);
  assert.equal(state.displayVisit(items[1]).id,3);
  assert.equal(state.visitDate(items[2]),null);
  assert.equal(state.latestCompleted({visits:[{id:10,status:'completed',completed_at:'2020-01-01T10:00:00Z'},{id:2,status:'completed',completed_at:'2026-01-01T10:00:00Z',outcome:'thinking'}]}).id,2);
});
test('agenda counts meetings, includes overdue and refused properties, ignores completed and cancelled',()=>{
  const past=Object.freeze({id:1,status:'planned',scheduled_at:'2026-10-01T10:00:00Z'});
  const future=Object.freeze({id:2,status:'planned',scheduled_at:'2026-11-01T10:00:00Z'});
  const refused=Object.freeze({id:'10',review_status:'rejected',visits:Object.freeze([future,past,{id:4,status:'completed',completed_at:'2020-01-01'}])});
  const other=Object.freeze({id:'20',visits:Object.freeze([{id:3,status:'planned',scheduled_at:'2026-10-20T18:00:00+03:00'},{id:5,status:'cancelled',scheduled_at:'2020-01-01'}])});
  const entries=state.agenda([refused,other]);
  assert.deepEqual(entries.map(e=>e.visit.id),[1,3,2]);
  assert.equal(entries.length,3);
  assert.equal(state.overdue(past,Date.parse('2026-10-26T00:00:00Z')),true);
  assert.equal(state.overdue(future,Date.parse('2026-10-26T00:00:00Z')),false);
  assert.equal(past.status,'planned');assert.equal(refused.review_status,'rejected');
});
test('comparison price per square metre requires known price and positive area',()=>{
  assert.equal(state.pricePerMeter({price:12000000,area:40}),300000);
  assert.equal(state.pricePerMeter({price:0,area:40}),0);
  for(const item of [{price:null,area:40},{price:12000000,area:0},{price:12000000,area:-1},{price:12000000,area:null},{}])
    assert.equal(state.pricePerMeter(item),null);
});

test('overdue is a Moscow calendar day comparison, never an expired hour today',()=>{
  const now=Date.parse('2026-10-25T21:30:00Z'); // October 26, 00:30 in Moscow.
  for(const stamp of ['2026-10-25T21:00:00Z','2026-10-26T06:00:00Z','2026-10-26T20:59:59Z']) {
    const v=Object.freeze({status:'planned',scheduled_at:stamp});
    assert.equal(state.overdue(v,now),false);
    assert.equal(v.status,'planned');
  }
  const today={status:'planned',scheduled_at:'2026-10-25T21:00:00Z'};
  assert.equal(state.overdue({status:'planned',scheduled_at:'2026-10-25T20:59:59Z'},now),true);
  assert.equal(state.overdue(today,Date.parse('2026-10-26T20:59:59Z')),false);
  assert.equal(state.overdue(today,Date.parse('2026-10-26T21:00:00Z')),true);
  assert.equal(state.overdue({status:'completed',scheduled_at:'2020-01-01T00:00:00Z'},now),false);
  assert.equal(state.overdue({status:'planned',scheduled_at:'invalid'},now),false);
});
test('calendar display and parsing do not depend on browser date locale',()=>{
  assert.equal(state.moscowDay('2026-10-31T23:30:00Z'),'2026-11-01');
  assert.equal(state.moscowDay('2026-10-31T20:59:59Z'),'2026-10-31');
  assert.equal(state.displayDay('2026-11-01'),'01.11.2026');
  assert.equal(state.displayDay(null),'—');
  assert.equal(state.parseDay('01.11.2026'),'2026-11-01');
  assert.equal(state.parseDay('29.02.2028'),'2028-02-29');
  for(const text of ['29.02.2026','31.04.2026','00.12.2026','01.13.2026','1.11.2026','01.01.1999','01.01.2101'])
    assert.equal(state.parseDay(text),null);
});
test('viewing date sort uses the shown Moscow day, with missing days last',()=>{
  const a={id:'1',visits:[{id:1,status:'planned',scheduled_at:'2026-10-31T23:30:00Z'}]};
  const b={id:'2',visits:[{id:2,status:'completed',completed_at:'2026-11-01T18:00:00+03:00'}]};
  const c={id:'3',visits:[{id:3,status:'planned',scheduled_at:'2026-10-31T20:59:00Z'}]};
  assert.equal(state.visitDate(a),state.visitDate(b));
  assert.deepEqual(state.ordered([a,b,c,{id:'4'}],{key:'visit_date',direction:'asc'}).map(i=>i.id),['3','1','2','4']);
  assert.deepEqual(state.ordered([a,b,c,{id:'4'}],{key:'visit_date',direction:'desc'}).map(i=>i.id),['1','2','3','4']);
});

test('post-visit sections are exclusive, based on latest actual result rather than ad opinions',()=>{
  const visit=(id,outcome,day)=>({id,status:'completed',outcome,completed_at:day+'T10:00:00Z'});
  for(const review_status of ['new','liked','rejected'])for(const realtor_status of ['pending','rejected']){
    const item={id:'1',review_status,realtor_status};
    assert.equal(state.viewedGroup(item),null);
    assert.equal(state.section(item),state.effectiveStatus(item)==='rejected'?'refused':'main');
    const liked={...item,in_our_list:true,visits:[visit(2,'liked','2026-10-23'),visit(5,'rejected','2026-10-20')]};
    assert.equal(state.section(liked,'ours'),'liked');
    assert.equal(state.section({...liked,viewing_documents_ready:true},'ours'),'documents');
    assert.equal(state.section({...liked,visits:[...liked.visits,visit(6,'rejected','2026-10-24')]},'ours'),'rejected');
  }
  const thinking={review_status:'viewed',visits:[visit(3,'thinking','2026-10-24')]};
  assert.equal(state.viewedGroup(thinking),null);assert.equal(state.section(thinking),'main');
  const tied={in_our_list:true,visits:[visit(7,'liked','2026-10-24'),visit(8,'rejected','2026-10-24')]};
  assert.equal(state.section(tied,'ours'),'rejected');
});
test('document classification and viewing revision survive stale polls and Cian republication',()=>{
  const previous={id:'1',property_id:3,in_our_list:true,selection_version:2,visits_version:7,viewing_documents_ready:true,viewed_group:'documents',
    visits:[{id:1,status:'completed',outcome:'liked',completed_at:'2026-10-24T10:00:00Z'}]};
  const result=state.merge(previous,{id:'2',property_id:3,price:100,in_our_list:true,selection_version:2,visits_version:6,viewing_documents_ready:false,visits:[]});
  assert.equal(state.section(result,'ours'),'documents');assert.equal(result.viewed_group,'documents');
  assert.equal(result.id,'2');assert.equal(result.price,100);
  const next=state.merge(result,{...result,visits_version:8,viewing_documents_ready:false});
  assert.equal(state.section(next,'ours'),'liked');
});
test('comparison remains read-only and rejects negative completed results even after manual family reset',()=>{
  const item={review_status:'liked',realtor_status:'pending',visits:[{id:1,status:'completed',outcome:'rejected',completed_at:'2026-10-24T10:00:00Z'}]};
  assert.equal(state.comparisonEligible(item),false);
  const liked={...item,visits:[{...item.visits[0],outcome:'liked'}]};
  assert.equal(state.comparisonEligible(liked),true);
  assert.equal(state.comparisonEligible({...liked,realtor_status:'rejected'}),false);
  assert.equal(item.review_status,'liked');
});

test('membership independently protects stale polls and partitions both pages',()=>{
  const previous={id:'1',property_id:9,in_our_list:true,selection_version:5,visits_version:4,visits:[]};
  const merged=state.merge(previous,{id:'2',property_id:9,in_our_list:false,selection_version:4,visits_version:3,visits:[],price:12});
  assert.equal(merged.in_our_list,true);assert.equal(merged.price,12);
  assert.equal(state.section(merged,'all'),null);assert.equal(state.section(merged,'ours'),'planned');
  const removed=state.merge(merged,{...merged,in_our_list:false,selection_version:6,realtor_status:'rejected'});
  assert.equal(state.section(removed,'all'),'refused');assert.equal(state.section(removed,'ours'),null);
  const thinking={...previous,visits:[{id:1,status:'completed',outcome:'thinking',completed_at:'2026-10-24T10:00:00Z'}]};
  assert.equal(state.section(thinking,'ours'),'planned');
});
test('family opinions and realtor rejection never remove a selected apartment',()=>{
  for(const review_status of ['new','liked','viewed','rejected'])for(const realtor_status of ['pending','approved','rejected']){
    for(const [outcome,group] of [['liked','liked'],['rejected','rejected'],['thinking','planned']]){
      const item={in_our_list:true,review_status,realtor_status,visits:[{id:1,status:'completed',outcome,completed_at:'2026-10-24T10:00:00Z'}]};
      assert.equal(state.section(item,'all'),null);assert.equal(state.section(item,'ours'),group);
    }
  }
});
