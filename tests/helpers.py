"""Exercise real HTTPS session/CSRF login in API regression tests."""
from fastapi.testclient import TestClient


def login_attempt(client, username='owner', password='long-test-password'):
    challenge = client.get('/api/auth/csrf')
    assert challenge.status_code == 200
    response = client.post('/api/auth/login', json={'username': username, 'password': password},
                           headers={'X-CSRF-Token': challenge.json()['csrf_token']})
    if response.status_code == 200:
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
    return response


def sign_in(client, username='owner', password='long-test-password'):
    response = login_attempt(client, username, password)
    assert response.status_code == 200, response.text
    return response.json()


def unauthenticated(client, path, **kwargs):
    # A wrong Basic header must no longer grant or override session authentication.
    with TestClient(client.app, base_url='https://testserver') as stranger:
        return stranger.post(path, auth=('bad', 'bad'), **kwargs)


def post_realtor(client,path,*,json,**kwargs):
    """Legacy regression scenarios now send the same optimistic token as the UI."""
    data=dict(json)
    response=client.get('/api/state?full=1')
    key=path.split('/')[3]
    item=next((i for i in response.json().get('listings',[]) if key in i.get('listing_ids',[i['id']])),{}) if response.status_code==200 else {}
    data.setdefault('property_id',item.get('property_id',1))
    data.setdefault('realtor_version',item.get('realtor_version',0))
    return client.post(path,json=data,**kwargs)
