"""Phase 3: real-property grouping, relisting detection and local branding."""
from app.property_match import listing_match
from tests.test_multiuser import multi_app


def decide(admin, listing_id, decision):
    row = next(item for item in admin.get('/api/state').json()['listings'] if str(listing_id) in item['listing_ids'])
    candidate = row['duplicate_candidate']
    return admin.post('/api/listings/'+str(listing_id)+'/duplicate-decision',json={
        'decision':decision,'candidate_id':candidate['id'],'source_property_id':candidate['source_property_id'],
        'candidate_property_id':candidate['candidate_property_id']})


def sample(item_id, address='Москва, Ленинградский проспект, д. 10, корп. 1',
           area=38.7, floor='7/16', price=12900000, description=None):
    detail = ' На лоджии установлен дубовый шкаф, ванная облицована мозаикой, потолки 2.85 метра.'
    return {
        'id': str(item_id),
        'url': 'https://www.cian.ru/sale/flat/' + str(item_id) + '/',
        'title': '1-комнатная квартира',
        'complex_name': 'ЖК Тестовый',
        'address': address,
        'price': price,
        'area': area,
        'floor': floor,
        'metro': 'Сокол',
        'metro_time': '8 мин. пешком',
        'description': (description or 'Светлая квартира, большая кухня, окна во двор, один собственник.') + detail,
        'card_text': (description or 'Светлая квартира, большая кухня, окна во двор, один собственник.') + detail,
    }


def test_matcher_is_conservative_but_recognizes_relisting():
    old = sample('1')
    new = sample('2', price=12500000,
                 description='Светлая квартира. Большая кухня, окна выходят во двор. Один собственник.')
    result = listing_match(new, old)
    assert result.score >= 85
    assert result.auto

    other_floor = sample('3', floor='11/16')
    different = listing_match(other_floor, old)
    assert not different.auto
    assert 'floor-different' in different.reasons

    other_house = sample('4', address='Москва, Ленинградский проспект, д. 12, корп. 1')
    assert listing_match(other_house, old).score == 0


def test_same_collection_does_not_silently_merge_identical_units(multi_app):
    main, _ = multi_app
    now = '2026-10-18T10:00:00+00:00'
    with main.database() as conn:
        result = main.save_items(conn, [sample('610'), sample('611')], now)
        ids = conn.execute("SELECT id,property_id FROM listings WHERE id IN ('610','611') ORDER BY id").fetchall()
    assert result['added'] == 2
    assert result['relisted'] == 0
    assert ids[0]['property_id'] != ids[1]['property_id']


def test_relisting_inherits_reviews_and_keeps_price_history(multi_app):
    main, admin = multi_app
    old = sample('700', price=12900000)
    with main.database() as conn:
        first = main.save_items(conn, [old], '2026-10-01T10:00:00+00:00')
    assert first['added'] == 1
    assert admin.post('/api/listings/700/review', json={'status':'liked'}).status_code == 200
    assert admin.post('/api/listings/700/realtor-review',
                      json={'status':'approved','comment':'Хороший вариант'}).status_code == 200

    new = sample('701', price=12450000,
                 description='Светлая квартира с большой кухней. Окна во двор. Один собственник.')
    with main.database() as conn:
        second = main.save_items(conn, [new], '2026-10-18T10:00:00+00:00')
        rows = conn.execute("SELECT id,property_id,review_status,realtor_status,realtor_comment FROM listings WHERE id IN ('700','701') ORDER BY id").fetchall()
        prices = [row['price'] for row in conn.execute(
            'SELECT price FROM price_history WHERE property_id=? ORDER BY id', (rows[0]['property_id'],)
        )]
    assert second == {'added':0,'updated':0,'relisted':1,'possible_duplicates':0}
    assert rows[0]['property_id'] == rows[1]['property_id']
    assert all(row['review_status'] == 'liked' for row in rows)
    assert all(row['realtor_status'] == 'approved' and row['realtor_comment'] == 'Хороший вариант' for row in rows)
    assert prices[-2:] == [12900000,12450000]

    state = admin.get('/api/state').json()['listings']
    item = next(entry for entry in state if entry['id'] == '701')
    assert item['listing_ids'] == ['700','701']
    assert item['relist_count'] == 1
    assert item['first_seen'] == '2026-10-01T10:00:00+00:00'
    assert item['price_min'] == 12450000 and item['price_max'] == 12900000
    assert item['review_status'] == 'liked'
    assert item['realtor_status'] == 'approved'


def test_ambiguous_duplicate_requires_manual_decision(multi_app):
    main, admin = multi_app
    old = sample('800', address='Москва, ул. Академика Королёва, д. 5', area=40.0, floor='5/12',
                 price=13000000, description='')
    # Remove text/complex/metro so address+area+floor is suggestive but not enough for automatic merge.
    for key in ('description','card_text','complex_name','metro'):
        old[key] = None
    with main.database() as conn:
        main.save_items(conn, [old], '2026-09-01T10:00:00+00:00')

    new = dict(old, id='801', url='https://www.cian.ru/sale/flat/801/', price=11500000)
    with main.database() as conn:
        result = main.save_items(conn, [new], '2026-10-18T10:00:00+00:00')
    assert result['added'] == 1
    assert result['possible_duplicates'] == 1

    item = next(entry for entry in admin.get('/api/state').json()['listings'] if entry['id'] == '801')
    assert 65 <= item['duplicate_candidate']['confidence'] < 85
    assert item['duplicate_candidate']['candidate_listing_id'] == '800'

    rejected = decide(admin,'801','different')
    assert rejected.status_code == 200
    item = next(entry for entry in admin.get('/api/state').json()['listings'] if entry['id'] == '801')
    assert item['duplicate_candidate'] is None

    old2 = sample('900', address='Москва, ул. Вавилова, д. 17', area=41.0, floor='6/14', price=13100000)
    for key in ('description','card_text','complex_name','metro'):
        old2[key] = None
    with main.database() as conn:
        main.save_items(conn, [old2], '2026-09-02T10:00:00+00:00')
        main.save_items(conn, [dict(old2, id='901', url='https://www.cian.ru/sale/flat/901/', price=11600000)],
                        '2026-10-18T11:00:00+00:00')
    assert decide(admin,'901','same').status_code == 200
    state = admin.get('/api/state').json()['listings']
    merged = next(entry for entry in state if entry['id'] == '901')
    assert merged['listing_ids'] == ['900','901']
    assert merged['relist_count'] == 1
    assert not any(entry['id'] == '900' for entry in state)


def test_logo_is_local_versioned_asset_not_inline(multi_app):
    main, admin = multi_app
    page = admin.get('/')
    assert page.status_code == 200
    assert 'data:image' not in page.text
    assert '/static/xata-logo.svg?v=' in page.text
    assert '/static/xata-mark.svg?v=' in page.text
    assert 'alt="ХАТА"' in page.text
    assert admin.get('/static/xata-logo.svg').status_code == 200
    assert admin.get('/static/xata-mark.svg').status_code == 200
