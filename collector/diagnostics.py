"""Read only the already open rendered DOM. No navigation, scroll, API or import."""
import asyncio
import re
from urllib.parse import parse_qs,urlsplit

from fastapi import HTTPException
from playwright.async_api import Error as PlaywrightError
from app.csv_source import listing_id
from app.source_types import SourceError
from collector.page import CARD_SELECTOR,CARD_SNAPSHOT,normalize_card

SAFE_PARAMS={'deal_type','engine_version','offer_type','region','room1','object_type[0]','maxprice','foot_min','only_foot','p'}
ENUMS={'sale','rent','flat','house','room','commercial','suburban','newbuilding'}


def public_query(url):
    try:
        parts=urlsplit(url);query=parse_qs(parts.query,max_num_fields=200)
        safe={k:v for k,v in query.items() if k in SAFE_PARAMS and all(
            value in ENUMS if k in ('deal_type','offer_type') else bool(re.fullmatch(r'[0-9]{1,12}',value)) for value in v)}
        return {'public_parameters':safe,'has_unreported_parameters':any(k not in safe for k in query),
                'is_search_path':parts.path in ('/cat.php','/cat.php/')}
    except (ValueError,TypeError):
        return {'public_parameters':{},'has_unreported_parameters':True,'is_search_path':False}


DOM_PROBE=r"""ids => {
  const snapshot = (""" + CARD_SNAPSHOT + r""")();
  const selector = """+repr(CARD_SELECTOR)+r""";
  const visible = node => node.isConnected
      && !!(node.offsetWidth || node.offsetHeight || node.getClientRects().length)
      && getComputedStyle(node).visibility !== 'hidden';
  const offerID = a => {
    try {
      const url = new URL(a.href);
      if (!/(^|\.)cian\.ru$/.test(url.hostname)) return null;
      const match = /^\/sale\/flat\/(\d+)\/?$/.exec(url.pathname);
      return match ? match[1].replace(/^0+(?=\d)/,'') : null;
    } catch (_) { return null; }
  };
  const nodes = [...document.querySelectorAll(selector)];
  const links = [...document.querySelectorAll('a[href*="/sale/flat/"]')].filter(visible);
  return {snapshot,targets:ids.map(id=>({
      id,rendered_link_present:links.some(a=>offerID(a)===id),
      card_indices:nodes.map((node,index)=>({node,index})).filter(({node})=>visible(node)&&
        [...node.querySelectorAll('a[href*="/sale/flat/"]')].some(a=>visible(a)&&offerID(a)===id))
        .map(({index})=>index).slice(0,32)
    })),viewport:{scroll_y:Math.round(scrollY),height:innerHeight,document_height:document.documentElement.scrollHeight},
    next_page_control_visible:[...document.querySelectorAll('a[rel="next"],[aria-label="Следующая страница"],[data-name="PaginationNext"]')].some(visible)};
}"""


async def diagnose_current_page(collector,ids,max_price=None):
    if collector.lock.locked():raise HTTPException(409,'Browser busy; diagnostics did not start collection')
    async with collector.lock:
        stage='classification_before_diagnostic'
        try:
            async with asyncio.timeout(12):
                status=await collector.inspect()
                if status!='authorized':
                    return {'mode':'current_dom_read_only','status':status,'targets':[],'reason':'authorization_or_confirmation_required'}
                stage='diagnostic_snapshot'
                raw=await asyncio.wait_for(collector.page.evaluate(DOM_PROBE,ids),timeout=3)
                normalized={};failures=0;failed_ids={};normalized_count=0;unique_ids=set()
                for card in raw['snapshot']['cards']:
                    try:
                        item=normalize_card(card)
                        normalized_count+=1;unique_ids.add(item['id'])
                        if item['id'] in ids:normalized[item['id']]=item
                    except (SourceError,ValueError,TypeError,KeyError,AttributeError) as exc:
                        failures+=1
                        try:
                            key=listing_id(card.get('url',''))
                            if key in ids:failed_ids[key]=type(exc).__name__
                        except (SourceError,ValueError,TypeError,AttributeError):pass
                stage='classification_after_diagnostic'
                status=await collector.inspect()
                if status!='authorized':
                    return {'mode':'current_dom_read_only','status':status,'targets':[],'reason':'page_changed_to_login_or_challenge'}
                for target in raw['targets']:
                    item=normalized.get(target['id'])
                    inside=any(i<100 for i in target['card_indices'])
                    target['within_snapshot_limit']=inside
                    target['normalization']='normalized' if item else 'normalization_error' if target['id'] in failed_ids else (
                        'outside_100_card_limit' if target['card_indices'] and not inside else
                        'card_not_extracted_or_changed' if inside else
                        'visible_link_without_card_selector' if target['rendered_link_present'] else 'not_observed_in_current_dom')
                    if target['id'] in failed_ids:target['normalization_error_type']=failed_ids[target['id']]
                    if item:
                        price=item.get('price')
                        target.update(parsed_price=price,missing_fields=[k for k in
                            ('title','price','area','floor','address','metro','metro_walk_minutes') if item.get(k) in (None,'')],
                            price_filter='admitted_missing_price' if price is None else
                            'budget_unconfirmed' if max_price is None else 'passed' if price<=max_price else 'rejected_price')
                return {'mode':'current_dom_read_only','status':status,'targets':raw['targets'],
                        'matching_card_nodes':raw['snapshot']['matching'],'snapshot_visible_cards':raw['snapshot']['visible'],
                        'normalization_failures':failures,'normalized_cards':normalized_count,'unique_normalized_ids':len(unique_ids),'snapshot_skipped':raw['snapshot']['skipped'],'snapshot_limit':100,
                        'page_query':public_query(collector.page.url),'budget_used_for_check':max_price,
                        'viewport':raw['viewport'],'next_page_control_visible':raw['next_page_control_visible'],
                        'caveat':'Current DOM only. No scrolling/navigation/import. Pagination controls do not prove completeness.'}
        except (PlaywrightError,asyncio.TimeoutError,ValueError,SourceError) as exc:
            return {'mode':'current_dom_read_only','status':'error','targets':[],'stage':stage,'error_type':type(exc).__name__}
