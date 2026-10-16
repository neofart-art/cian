/* Shared by the UI and offline regression tests. Never stores credentials. */
(function(root) {
  'use strict';
  const fields = ['review_status','review_updated_at','review_updated_by','review_updated_by_name','review_updated_by_username','review_version'];
  function merge(previous, incoming) {
    if (!previous || Number(incoming.review_version || 0) >= Number(previous.review_version || 0)) return {...incoming};
    const result = {...incoming};
    for (const key of fields) result[key] = previous[key];
    return result;
  }
  function ordered(items) {
    const rank = {new:0, viewed:1, rejected:2};
    return [...items].sort((a,b) => (rank[a.review_status] ?? 0) - (rank[b.review_status] ?? 0)
      || String(b.first_seen || '').localeCompare(String(a.first_seen || ''))
      || String(a.id).localeCompare(String(b.id)));
  }
  const api = {merge, ordered};
  root.XataReview = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
