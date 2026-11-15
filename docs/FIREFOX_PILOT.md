# Сверка выдачи ЦИАН в видимом Firefox

Цель — собирать основную выдачу, которую видит пользователь, а не объяснять
несоответствия дополнительными фильтрами после широкого DOM-сбора.

## Что подготовлено

- `BROWSER_ENGINE=firefox` выбирает Firefox; по умолчанию остаётся Chromium.
  Профили разделены на `/profile/firefox` и `/profile/chromium`.
- Отдельный `Dockerfile.firefox` / `requirements-firefox.txt` используют
  согласованные Playwright 1.64.1 и образ `v1.64.1-noble`. Производственный
  Dockerfile.browser / requirements-browser остаются на 1.58.0.
- Это сборка Firefox, поддерживаемая Playwright, не точный Windows-бинарник
  пользователя. Версия выводится в `/status`. ОС, аккаунт, IP, cookies и
  сортировка могут менять выдачу; одна смена движка не доказывает равенство.
- Через noVNC видна ТА ЖЕ открытая страница, которую читает preview. Обычный
  локальный Firefox и серверный Firefox не считаются автоматически идентичными.
- Preview не листает, не прокручивает, не импортирует и не читает embedded state,
  cookies, tokens или сетевые API ЦИАН. Возвращает видимые ID, заголовки разделов
  и публичные data-name предков карточек, до 100 карточек.
- `/preview/screenshot` снимает текущий viewport, без full-page прокрутки.
  Он может содержать видимые данные вашего аккаунта; не публикуйте его без нужды.
- `/preview/open-search` открывает только настроенный HTTPS URL `/cat.php` ЦИАН,
  только по отдельному запросу в review-only режиме. Навигации при запуске нет.
- Все preview API требуют внутренний Bearer-токен; 8010 не публикуется.
- `BROWSER_REVIEW_ONLY=1` запрещает `/collect`, даже если ручной флаг выключен.
  В ручном режиме нет перехвата сетевых запросов браузера (context.route),
  который вмешивался в открытие вкладок. Это не исправление прежнего краша
  Chromium и не обход CAPTCHA; подтверждения выполняет пользователь.

## Подготовка на OMV

```bash
python3 /путь/к/исходникам/scripts/prepare_firefox_pilot.py \
  --output /root/xata-recovery/firefox-pilot --build
```

Скрипт только читает текущий `CIAN_SEARCH_URL` у xata-app-1 и создаёт отдельный
Compose. Не копирует пароль ХАТА/CIAN, API-токен production, профиль Chromium,
volume БД или сеть xata_default. API-токен создаётся новый. Конфигурация приватная.
Работающие xata-app-1 и xata-browser-1 не меняются.

Пилот: проект `xata-firefox-pilot`, контейнер `xata-firefox-pilot`, отдельный
volume `xata_firefox_pilot_profile`, 3 CPU / 4 GiB RAM / 512 MiB shm,
публично доступных портов нет; noVNC слушает только 127.0.0.1:16081 на OMV.

После сборки задайте отдельный VNC-пароль (8 печатных ASCII-символов):

```bash
docker compose -p xata-firefox-pilot -f /root/xata-recovery/firefox-pilot/compose.json \
  run --rm --no-deps --entrypoint python browser -m collector.vnc_password
docker compose -p xata-firefox-pilot -f /root/xata-recovery/firefox-pilot/compose.json \
  up -d --no-deps --no-build --wait --wait-timeout 120 browser
```

В существующем PuTTY добавьте Local tunnel: Source 16081 → Destination
127.0.0.1:16081, Add → Apply. Откройте http://127.0.0.1:16081/vnc.html на ПК.
Пилот имеет новый профиль: при необходимости пользователь вручную входит в
свой аккаунт ЦИАН и проходит подтверждение. Пароли в чат не присылаются.

Открыть ровно текущую ссылку ХАТА, не вставляя длинный URL вручную:

```bash
docker exec -i xata-firefox-pilot python - <<'PY'
import json, os, urllib.request
r = urllib.request.Request('http://127.0.0.1:8010/preview/open-search', data=b'{}',
    headers={'Authorization':'Bearer '+os.environ['BROWSER_API_TOKEN'],
             'Content-Type':'application/json'}, method='POST')
print(json.load(urllib.request.urlopen(r, timeout=30)))
PY
```

## Сверка перед импортом

1. Одинаковая ссылка и сортировка в локальном Firefox и серверном окне noVNC.
   Войдите в один аккаунт; учитывайте разницу IP. Сравните первые ID основной
   выдачи, не только количество или похожие фотографии.
2. `/preview/results` без selector показывает кандидатов и названия предков.
   По реальному DOM выберите точный контейнер основной выдачи; не угадывайте
   selector и не считайте body/main доказательством основного раздела.
3. Передайте selector в `/preview/results?selector=...`. Проверьте, что ID
   `in_selected_container=true` совпадают с видимой основной выдачей; карточки
   рекомендаций находятся снаружи. JSON и screenshot приватны.
4. `query_matches_configured_search` сравнивает текущие query-условия с
   настроенными, игнорируя только context/mlSearchSessionGuid/saved_search_id.
   Это не доказательство одинаковой серверной персонализации ЦИАН.
5. При `BROWSER_REQUIRE_RESULT_SCOPE=1` автоматический сбор допускает только
   единственный видимый контейнер по `BROWSER_RESULTS_SELECTOR`; отсутствие,
   неоднозначность или невалидный selector останавливают пакет. Совпадение
   контейнера — ещё не доказательство его семантики: сначала проверяется п.3.
6. В пилоте импорт остаётся заблокированным. Подключение Firefox к production,
   перенос app BROWSER_SERVICE_URL, включение сбора и пагинации — отдельный шаг
   после проверки реального DOM. Эта версия сама не переключает production.

Чтение текущих ID без навигации/сбора:

```bash
docker exec -i xata-firefox-pilot python - <<'PY'
import json, os, urllib.request
r = urllib.request.Request('http://127.0.0.1:8010/preview/results',
    headers={'Authorization':'Bearer '+os.environ['BROWSER_API_TOKEN']})
print(json.dumps(json.load(urllib.request.urlopen(r,timeout=15)), ensure_ascii=False, indent=2))
PY
```

Остановить только пилот, сохранив его профиль:

```bash
docker compose -p xata-firefox-pilot -f /root/xata-recovery/firefox-pilot/compose.json stop
```

Не используйте down -v и не заменяйте прежний production Compose на pilot.

## Подтверждённая разметка OMV, 15.11.2026

Диагностика живого Firefox показала 6 карточек в `[data-name="Offers"]` и
19 в `[data-name="SuggestionsLayout"]`, с заголовком «Дополнительные предложения
по вашему запросу». Объявление 336311683 находится во второй группе.
Именно для него захват дополнительных предложений подтверждён; принадлежность
старого 333386132 к этому блоку исторически не записана и не объявляется доказанной.

Оба контейнера используют одинаковый CSS-класс wrapper, поэтому выбирать только
по этому классу нельзя. Подтверждённый selector для дальнейшей сверки —
`[data-name="Offers"]`. Сборщик дополнительно распознаёт SuggestionsLayout.

`query_matches_configured_search=False` требует отдельного разбора. Поле
`query_difference` показывает изменившиеся публичные фильтры и только названия
неизвестных параметров, скрывая их значения. Полный URL, saved_search_id,
context, cookies и токены не выводятся. Сравнение не ослабляется автоматически:
Firefox остаётся review-only до проверки различий и совпадения видимых ID.

Следующий живой сбор подтвердил одинаковые 6 ID и совпадение URL. Проверка
времени обнаружила отдельную ошибку: без TimeLabel значение из описания продавца
могло заменить время из шапки карточки (78 вместо 9, 5 вместо 7 минут), а явные
10 минут рядом со станцией не распознавались. Нормализация browser-карточки
теперь использует явный TimeLabel либо отдельную строку длительности сразу
после названия станции в шапке до цены/описания. Время из свободного текста
продавца не заполняет это поле; исходный текст сохраняется. CSV-парсер не меняется.
Правило исключения района Северный остаётся прежним; его присутствие в Offers
само по себе не меняет подтверждённый список районов ХАТА.
