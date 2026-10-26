"""Date-only permissions, optimistic revisions and reuse of real viewing records."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.helpers import sign_in
from tests.test_multiuser import multi_app,reviewer_client
from tests.test_viewings import current,create,payload


def date_payload(item,date,**changes):
    planned=[v for v in item['visits'] if v['status']=='planned']
    completed=[v for v in item['visits'] if v['status']=='completed']
    shown=(min(planned,key=lambda v:(v['scheduled_at'],v['id'])) if planned else
           max(completed,key=lambda v:(v['completed_at'],v['id'])) if completed else None)
    return {'property_id':item['property_id'],'visits_version':item['visits_version'],
            'visit_id':shown['id'] if shown else None,'date':date,
            'request_id':None if shown else str(uuid4()),**changes}


def patch(client,data,key='123'):
    return client.patch('/api/listings/'+key+'/visits/date',json=data)


def decisions(item):
    return {k:v for k,v in item.items() if k.startswith(('review_','realtor_'))}


def test_date_only_roles_csrf_and_original_form_permissions(multi_app):
    main,admin=multi_app
    _,buyer=reviewer_client(main,admin)
    before=current(admin)
    data=date_payload(before,'2026-11-01T18:30:00+03:00')
    assert patch(buyer,data).status_code==403
    assert current(admin)['visits']==[]
    assert admin.post('/api/users',json={'username':'expert','display_name':'Риелтор','password':'password-123','role':'realtor'}).status_code==201
    with TestClient(main.app,base_url='https://testserver') as realtor:
        sign_in(realtor,'expert','password-123')
        result=patch(realtor,data)
        assert result.status_code==200,result.text
        assert len(result.json()['visits'])==1
        assert realtor.post('/api/listings/123/visits',json=payload(result.json(),status='completed',outcome='liked')).status_code==403
    # The user explicitly kept the original result form unchanged: buyers
    # may still submit a viewing/result there, but cannot use the inline API.
    visit=current(admin)['visits'][0]
    result=buyer.patch('/api/listings/123/visits/'+str(visit['id']),json=payload(current(admin),
        version=visit['version'],status='completed',outcome='liked',completed_at='2026-11-02T19:00:00+03:00'))
    assert result.status_code==200 and result.json()['review_status']=='liked'
    with TestClient(main.app,base_url='https://testserver') as stranger:
        assert patch(stranger,data).status_code in (401,403)
    with TestClient(main.app,base_url='https://testserver') as no_csrf:
        sign_in(no_csrf);no_csrf.headers.pop('X-CSRF-Token')
        assert patch(no_csrf,data).status_code==403


def test_first_date_creates_one_meeting_and_retries_do_not_reapply_decisions(multi_app):
    main,admin=multi_app
    admin.post('/api/listings/123/review',json={'status':'liked'})
    before=current(admin)
    data=date_payload(before,'2026-11-01T18:30:00+03:00')
    result=patch(admin,data)
    assert result.status_code==200,result.text
    saved=result.json()
    assert len(saved['visits'])==1 and saved['visits_version']==1
    assert saved['visits'][0]['scheduled_at']=='2026-11-01T15:30:00+00:00'
    assert saved['visits'][0]['status']=='planned'
    assert decisions(saved)==decisions(before)
    for _ in range(3):
        response=patch(admin,data)
        assert response.status_code==200 and response.json()['visits']==saved['visits']
        assert response.json()['visits_version']==1
    assert patch(admin,{**data,'date':'2026-11-02T18:30:00+03:00'}).status_code==409
    assert len(current(admin)['visits'])==1


def test_existing_planned_and_completed_dates_keep_notes_results_and_statuses(multi_app):
    main,admin=multi_app
    item=create(admin,preparation='До поездки',comment='Заметка',scheduled_at='2026-11-01T18:00:00+03:00')
    old=item['visits'][0];reviews=decisions(item)
    data=date_payload(item,'2026-11-02T19:45:00+03:00')
    result=patch(admin,data)
    assert result.status_code==200
    saved=result.json();visit=saved['visits'][0]
    assert visit['id']==old['id'] and len(saved['visits'])==1
    assert visit['scheduled_at']=='2026-11-02T16:45:00+00:00'
    for field in ('preparation','comment','status','outcome','completed_at','created_at'):
        assert visit[field]==old[field]
    assert decisions(saved)==reviews
    assert patch(admin,data).json()['visits_version']==saved['visits_version']
    finished=admin.patch('/api/listings/123/visits/'+str(visit['id']),json=payload(saved,
        version=visit['version'],status='completed',outcome='rejected',scheduled_at=visit['scheduled_at'],
        completed_at='2026-11-03T18:00:00+03:00',preparation='Проверить',comment='Шумно',rejection_reasons=['noise'])).json()
    admin.post('/api/listings/123/review',json={'status':'liked'})
    before=current(admin);old=before['visits'][0]
    result=patch(admin,date_payload(before,'2026-11-04T19:00:00+03:00'))
    assert result.status_code==200,result.text
    saved=result.json();visit=saved['visits'][0]
    assert visit['completed_at']=='2026-11-04T16:00:00+00:00'
    assert decisions(saved)==decisions(before)
    for field in ('id','status','outcome','preparation','comment','rejection_reasons','scheduled_at','created_at'):
        assert visit[field]==old[field]
    assert patch(admin,date_payload(saved,None)).status_code==422
    assert current(admin)['visits']==saved['visits']


def test_clear_planned_date_cancels_without_removing_history(multi_app):
    main,admin=multi_app
    item=create(admin)
    data=date_payload(item,None);old=item['visits'][0]
    result=patch(admin,data)
    assert result.status_code==200
    saved=result.json();visit=saved['visits'][0]
    assert visit['status']=='cancelled' and visit['id']==old['id']
    assert visit['scheduled_at']==old['scheduled_at']
    assert decisions(saved)==decisions(item)
    assert patch(admin,data).json()['visits_version']==saved['visits_version']
    result=patch(admin,date_payload(saved,'2026-12-01T18:00:00+03:00'))
    assert result.status_code==200 and len(result.json()['visits'])==2
    assert sum(v['status']=='planned' for v in result.json()['visits'])==1
    assert sum(v['status']=='cancelled' for v in result.json()['visits'])==1


@pytest.mark.parametrize('changes',[
    {'date':'2026-11-01T18:30:00'},{'date':'2101-01-01T00:00:00+03:00'},
    {'date':'not-a-date'},{'outcome':'liked'},{'comment':'Cannot write notes here'},
    {'review_status':'liked'},{'visits_version':-1},{'request_id':'bad'},
])
def test_date_input_rejects_invalid_times_and_unrelated_fields(multi_app,changes):
    main,admin=multi_app
    data=date_payload(current(admin),'2026-11-01T18:30:00+03:00')
    data.update(changes)
    assert patch(admin,data).status_code==422
    assert current(admin)['visits']==[]


def test_concurrent_updates_and_wrong_targets_do_not_overwrite(multi_app):
    main,admin=multi_app
    item=create(admin)
    data=date_payload(item,'2026-11-02T18:00:00+03:00')
    assert patch(admin,data).status_code==200
    assert patch(admin,{**data,'date':'2026-11-03T18:00:00+03:00'}).status_code==409
    saved=current(admin)
    assert patch(admin,{**date_payload(saved,'2026-11-04T18:00:00+03:00'),'property_id':99999}).status_code==409
    assert patch(admin,{**date_payload(saved,'2026-11-04T18:00:00+03:00'),'visit_id':99999}).status_code==409
    assert current(admin)['visits']==saved['visits']


def test_nearest_switch_and_retry_reuse_the_same_viewing(multi_app):
    main,admin=multi_app
    item=create(admin,scheduled_at='2026-11-01T18:00:00+03:00')
    first=item['visits'][0]
    item=create(admin,scheduled_at='2026-12-01T18:00:00+03:00')
    second=item['visits'][0]
    wrong=date_payload(item,'2027-01-01T18:00:00+03:00',visit_id=second['id'])
    assert patch(admin,wrong).status_code==409
    data=date_payload(item,'2027-01-01T18:00:00+03:00')
    assert data['visit_id']==first['id']
    saved=patch(admin,data).json()
    assert len(saved['visits'])==2
    assert patch(admin,data).json()['visits']==saved['visits']
    assert current(admin)['visits_version']==saved['visits_version']


@pytest.mark.parametrize('same_key',[False,True])
def test_simultaneous_first_date_cannot_create_duplicates(multi_app,same_key):
    main,admin=multi_app
    data=date_payload(current(admin),'2026-11-01T18:00:00+03:00')
    other={**data,'request_id':data['request_id'] if same_key else str(uuid4())}
    barrier=Barrier(2)
    def save(body):
        barrier.wait(timeout=5)
        return patch(admin,body)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(save,[data,other]))
    assert sorted(r.status_code for r in responses)==([200,200] if same_key else [200,409])
    assert len(current(admin)['visits'])==1 and current(admin)['visits_version']==1


@pytest.mark.parametrize('day,stored',[
    ('2026-11-01','2026-10-31T21:00:00+00:00'),
    ('2026-01-01','2025-12-31T21:00:00+00:00'),
    ('2000-01-01','1999-12-31T21:00:00+00:00'),
    ('2010-06-01','2010-05-31T20:00:00+00:00'),
    ('2100-12-31','2100-12-30T21:00:00+00:00'),
])
def test_calendar_day_uses_moscow_not_browser_or_utc_date(multi_app,day,stored):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    main,admin=multi_app
    before=current(admin)
    data=date_payload(before,day)
    response=patch(admin,data)
    assert response.status_code==200,response.text
    item=response.json();visit=item['visits'][0]
    assert visit['scheduled_at']==stored
    assert datetime.fromisoformat(stored).astimezone(ZoneInfo('Europe/Moscow')).date().isoformat()==day
    assert decisions(item)==decisions(before)
    assert patch(admin,data).json()['visits']==item['visits']
    assert current(admin)['visits_version']==item['visits_version']
    assert len(current(admin)['visits'])==1


def test_calendar_day_preserves_existing_wall_time_precision_and_result(multi_app):
    main,admin=multi_app
    item=create(admin,scheduled_at='2026-10-31T23:30:15.123456+00:00',
                preparation='До',comment='После')
    old=item['visits'][0]
    # Its UTC day is October 31, but the displayed Moscow day is November 1.
    unchanged=patch(admin,date_payload(item,'2026-11-01'))
    assert unchanged.status_code==200
    assert unchanged.json()['visits']==item['visits']
    assert unchanged.json()['visits_version']==item['visits_version']
    changed=patch(admin,date_payload(item,'2026-11-02')).json()
    assert changed['visits'][0]['id']==old['id']
    assert changed['visits'][0]['scheduled_at']=='2026-11-01T23:30:15.123456+00:00'
    assert changed['visits'][0]['preparation']=='До' and changed['visits'][0]['comment']=='После'
    assert decisions(changed)==decisions(item)
    visit=changed['visits'][0]
    complete=admin.patch('/api/listings/123/visits/'+str(visit['id']),json=payload(changed,
        version=visit['version'],status='completed',outcome='rejected',scheduled_at=visit['scheduled_at'],
        completed_at='2026-11-02T20:15:43.654321+03:00',preparation='До',comment='После',rejection_reasons=['noise'])).json()
    after=patch(admin,date_payload(complete,'2026-11-03')).json()
    assert after['visits'][0]['completed_at']=='2026-11-03T17:15:43.654321+00:00'
    assert after['visits'][0]['outcome']=='rejected'
    assert after['visits'][0]['rejection_reasons']==['noise']
    assert decisions(after)==decisions(complete)
    assert len(after['visits'])==1


@pytest.mark.parametrize('day',['2026-02-30','2026-13-01','1999-12-31','2101-01-01','01.11.2026'])
def test_invalid_calendar_days_do_not_write(multi_app,day):
    main,admin=multi_app
    assert patch(admin,date_payload(current(admin),day)).status_code==422
    assert current(admin)['visits']==[]


def test_calendar_day_reuses_date_only_authorization(multi_app):
    main,admin=multi_app
    _,buyer=reviewer_client(main,admin)
    assert patch(buyer,date_payload(current(admin),'2026-11-01')).status_code==403
    assert current(admin)['visits']==[]
