/* Shared pure UI calculations. Never stores credentials. */
(function(root) {
  'use strict';
  const reviewFields = ['review_status','review_updated_at','review_updated_by','review_updated_by_name','review_updated_by_username','review_version'];
  const realtorFields = ['realtor_status','realtor_comment','realtor_updated_at','realtor_updated_by','realtor_updated_by_name','realtor_updated_by_username','realtor_version'];
  const rank = {new:0, liked:1, viewed:2, rejected:3}, realtorRank = {pending:0, approved:1, rejected:2};
  const collator = new Intl.Collator('ru', {sensitivity:'base'});
  function key(item) {
    return item.property_id == null ? 'listing:' + item.id : 'property:' + item.property_id;
  }
  function reconcile(previous, incoming, previousRevision, incomingRevision) {
    if (Number(incomingRevision || 0) < Number(previousRevision || 0)) return new Map(previous);
    return new Map(incoming.map(item => [key(item), merge(previous.get(key(item)),item)]));
  }
  function merge(previous, incoming) {
    const result = {...incoming};
    if (previous) for (const [version,fields] of [['review_version',reviewFields],['realtor_version',realtorFields],['visits_version',['visits','visits_version','viewing_documents_ready','viewed_group']]]) {
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
      case 'visit_date': return visitDate(item);
      case 'object': return [item.complex_name || item.title || '',item.address || ''].join(' ').trim() || null;
      case 'price': case 'area': return numeric(item[key]);
      case 'mortgage': return mortgage(item.price);
      case 'floor': { const match = String(item.floor || '').match(/^-?\d+/); return match ? Number(match[0]) : null; }
      case 'metro': return item.metro || null;
      case 'metro_time': return minutes(item);
      case 'first_seen': return numeric(item.first_seen ? Date.parse(item.first_seen) : null);
      case 'review_status': return rank[effectiveStatus(item)] ?? null;
      case 'realtor_status': return realtorRank[item.realtor_status || 'pending'] ?? null;
      default: return null;
    }
  }
  function compare(a,b) {
    return typeof a === 'number' && typeof b === 'number' ? a-b : collator.compare(String(a),String(b));
  }
  function effectiveStatus(item) {
    return item.realtor_status === 'rejected' ? 'rejected' : (item.review_status || 'new');
  }
  function nextVisit(item) {
    return (item.visits || []).filter(v=>v.status==='planned').sort((a,b)=>Date.parse(a.scheduled_at)-Date.parse(b.scheduled_at)||a.id-b.id)[0] || null;
  }
  function latestCompleted(item) {
    return (item.visits || []).filter(v=>v.status==='completed')
      .sort((a,b)=>(Date.parse(b.completed_at)||0)-(Date.parse(a.completed_at)||0)||b.id-a.id)[0] || null;
  }
  function viewedGroup(item) {
    const visit=latestCompleted(item);
    if(!visit||visit.outcome==='thinking')return null;
    return visit.outcome==='liked'?(item.viewing_documents_ready?'documents':'liked'):'rejected';
  }
  function section(item) { return viewedGroup(item)|| (effectiveStatus(item)==='rejected'?'refused':'main'); }
  function comparisonEligible(item) { return effectiveStatus(item)!=='rejected'&&viewedGroup(item)!=='rejected'; }
  function displayVisit(item) { return nextVisit(item) || latestCompleted(item); }
  const calendar=new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'});
  function moscowDay(value) {
    if(value==null||value==='')return null;
    const instant=new Date(value);
    if(!Number.isFinite(instant.getTime()))return null;
    const parts=Object.fromEntries(calendar.formatToParts(instant).map(part=>[part.type,part.value]));
    return parts.year+'-'+parts.month+'-'+parts.day;
  }
  function displayDay(day) {
    return /^\d{4}-\d{2}-\d{2}$/.test(day||'') ? day.split('-').reverse().join('.') : '—';
  }
  function parseDay(text) {
    const match=/^(\d{2})\.(\d{2})\.(\d{4})$/.exec(text||'');
    if(!match)return null;
    const year=Number(match[3]),month=Number(match[2]),day=Number(match[1]);
    const instant=new Date(Date.UTC(year,month-1,day));
    return year>=2000&&year<=2100&&instant.getUTCFullYear()===year&&instant.getUTCMonth()===month-1&&instant.getUTCDate()===day
      ? match[3]+'-'+match[2]+'-'+match[1] : null;
  }
  function visitDate(item) {
    const v=displayVisit(item),day=v?moscowDay(v.status==='planned'?v.scheduled_at:v.completed_at):null;
    return day ? Date.parse(day+'T00:00:00Z') : null;
  }
  function overdue(visit,now=Date.now()) {
    const day=visit?.status==='planned'?moscowDay(visit.scheduled_at):null,today=moscowDay(now);
    return !!day&&!!today&&day<today;
  }
  function agenda(items) {
    return [...items].flatMap(item=>(item.visits||[]).filter(v=>v.status==='planned').map(visit=>({item,visit})))
      .sort((a,b)=>Date.parse(a.visit.scheduled_at)-Date.parse(b.visit.scheduled_at)||a.visit.id-b.visit.id);
  }
  function pricePerMeter(item) {
    const price=numeric(item.price),area=numeric(item.area);
    return price!=null && area!=null && area>0 ? price/area : null;
  }
  function defaultOrder(a,b) {
    return (rank[effectiveStatus(a)] ?? 0) - (rank[effectiveStatus(b)] ?? 0)
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
    for (const item of items) { result.all++; result[effectiveStatus(item)]++; }
    return result;
  }
  const api = {key,reconcile,merge,ordered,cycleSort,counts,mortgage,minutes,effectiveStatus,nextVisit,latestCompleted,displayVisit,visitDate,overdue,agenda,pricePerMeter,moscowDay,displayDay,parseDay,viewedGroup,section,comparisonEligible};
  root.XataReview = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
