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
