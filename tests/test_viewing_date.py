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
