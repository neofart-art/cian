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
