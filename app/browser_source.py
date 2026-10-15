"""Internal browser-service adapter. No Playwright/Chromium in the app image."""
import os

import httpx

from app.source_types import SourceBlocked, SourceError
from app.source import search_url


class BrowserAttention(SourceBlocked):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class CianBrowserSource:
    name = 'ЦИАН browser'

    def request(self, method, path, **kwargs):
        token = os.getenv('BROWSER_API_TOKEN') or os.getenv('APP_PASSWORD', '')
        try:
            with httpx.Client(timeout=httpx.Timeout(5 if path == '/status' else 110, connect=3), trust_env=False) as client:
                response = client.request(method, os.getenv('BROWSER_SERVICE_URL', 'http://browser:8010') + path,
                                          headers={'Authorization': f'Bearer {token}'}, **kwargs)
            if response.status_code == 409:
                raise SourceError('Браузер занят сбором или ручным входом. Завершите вход и отключите noVNC.')
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SourceError('Browser service недоступен или вернул неверный ответ. Проверьте его состояние и журнал.') from exc

    def status(self):
        return self.request('GET', '/status')

    def collect(self, filters):
        result = self.request('POST', '/collect', json={
            'url': os.getenv('CIAN_SEARCH_URL') or search_url(filters),
        })
        if result.get('status') in ('login_required', 'confirmation_required'):
            raise BrowserAttention(result['status'], result['message'])
        if result.get('status') != 'authorized' or not isinstance(result.get('items'), list):
            raise SourceError(result.get('error') or result.get('message') or 'Не удалось подтвердить обычную выдачу ЦИАН.')
        return [dict(item, source=self.name) for item in result['items']
                if item.get('price') is None or item['price'] <= filters['max_price']]
