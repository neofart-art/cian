/* Shared pure UI calculations. Never stores credentials. */
(function(root) {
  'use strict';
  const reviewFields = ['review_status','review_updated_at','review_updated_by','review_updated_by_name','review_updated_by_username','review_version'];
  const realtorFields = ['realtor_status','realtor_comment','realtor_updated_at','realtor_updated_by','realtor_updated_by_name','realtor_updated_by_username','realtor_version'];
  const rank = {new:0, liked:1, viewed:2, rejected:3}, realtorRank = {pending:0, approved:1, rejected:2};
  const collator = new Intl.Collator('ru', {sensitivity:'base'});
  function merge(previous, incoming) {
    const result = {...incoming};
    if (previous) for (const [version,fields] of [['review_version',reviewFields],['realtor_version',realtorFields]]) {
      if (Number(previous[version] || 0) > Number(incoming[version] || 0))
        for (const key of fields) result[key] = previous[key];
    }
    return result;
  }
  function numeric(value) {
    if (value == null || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }
  function mortgage(price) {
    const amount = numeric(price);
    if (amount == null) return null;
    const rate = .18 / 12, factor = (1 + rate) ** 180;
    return Math.max(amount - 6000000, 0) * rate * factor / (factor - 1);
  }
  function minutes(item) {
    if (!item.metro_time || /сегодня|вчера|назад/i.test(item.metro_time)) return null;
    const match = item.metro_time.match(/(\d+)\s*мин(?:ут[а-я]*)?\.?\s*(?:пешком|транспортом|на транспорте)/i);
    return match ? Number(match[1]) : null;
  }
  function value(item, key) {
    switch(key) {
      case 'id': return numeric(item.id);
      case 'object': return [item.complex_name || item.title || '',item.address || ''].join(' ').trim() || null;
      case 'price': case 'area': return numeric(item[key]);
      case 'mortgage': return mortgage(item.price);
      case 'floor': { const match = String(item.floor || '').match(/^-?\d+/); return match ? Number(match[0]) : null; }
      case 'metro': return item.metro || null;
      case 'metro_time': return minutes(item);
      case 'first_seen': return numeric(item.first_seen ? Date.parse(item.first_seen) : null);
      case 'review_status': return rank[item.review_status || 'new'] ?? null;
      case 'realtor_status': return realtorRank[item.realtor_status || 'pending'] ?? null;
      default: return null;
    }
  }
  function compare(a,b) {
    return typeof a === 'number' && typeof b === 'number' ? a-b : collator.compare(String(a),String(b));
  }
  function defaultOrder(a,b) {
    return (rank[a.review_status] ?? 0) - (rank[b.review_status] ?? 0)
      || (value(b,'first_seen') ?? 0) - (value(a,'first_seen') ?? 0)
      || compare(value(a,'id') ?? a.id, value(b,'id') ?? b.id);
  }
  function ordered(items, sort = null) {
    return [...items].sort((a,b) => {
      if (sort) {
        const left = value(a,sort.key), right = value(b,sort.key);
        // Unknown values go last in both directions.
        if (left == null && right != null) return 1;
        if (right == null && left != null) return -1;
        if (left != null && right != null) {
          const result = compare(left,right) * (sort.direction === 'asc' ? 1 : -1);
          if (result) return result;
        }
      }
      return defaultOrder(a,b);
    });
  }
  function cycleSort(sort, key) {
    if (!sort || sort.key !== key) return {key,direction:'asc'};
    return sort.direction === 'asc' ? {key,direction:'desc'} : null;
  }
  function counts(items) {
    const result = {all:0,new:0,liked:0,viewed:0,rejected:0};
    for (const item of items) { result.all++; result[item.review_status || 'new']++; }
    return result;
  }
  const api = {merge,ordered,cycleSort,counts,mortgage,minutes};
  root.XataReview = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
