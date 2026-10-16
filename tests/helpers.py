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
