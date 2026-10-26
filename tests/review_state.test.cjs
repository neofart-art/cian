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
