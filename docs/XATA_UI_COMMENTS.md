# Интерфейс и общие комментарии семьи — ноябрь 2026

Изменения только app. Карты, геокодирование, внешние сервисы, анкетирование,
загрузки новых страниц ЦИАН и заметки до поездки в интерфейсе не добавлены.
Collector, matcher, ипотечная формула, Compose, порты и volumes не меняются.

## Две страницы

«Все объявления» показывает все уникальные property, в том числе выбранные.
Текущие отказы семьи/риелтора остаются в закрытом разделе «Не подходят»,
включая выбранные квартиры. Голубая метка «В нашем списке» остаётся при отказе;
красный фон имеет приоритет. «Наши квартиры» показывает только подборку.

Навигационные счётчики считаются по полному состоянию, независимо от поиска,
фильтров и закрытых групп. «Найдено» показывается отдельно.
Четыре группы, отзывы о поездках, документы, календарь, ближайшие встречи
и сравнение сохранены. Отказ не удаляет квартиру из подборки.

Риелтор — первый столбец/блок карточки. Отдельного «До метро» нет; исходные
время и расстояние доступны в характеристиках, сравнении, импорте и Excel.
На узких экранах те же строки и формы становятся карточками через CSS,
не создавая вторые активные редакторы. На телефоне есть сортировка.
Увеличение фото и календарь остаются прежними.

## Комментарии и версии

Оценка риелтора, семейный комментарий и впечатление конкретного просмотра
независимы. У риелтора и семьи один текущий текст и отдельная история.
Редактор заменяет обычный показ текста. «Отмена» не отправляет запрос.
Фоновое обновление не меняет значение, курсор и DOM открытого поля.

Семейный комментарий изменяют reviewer/admin только у выбранной квартиры.
Риелтор читает его. После удаления из подборки текст и история доступны
в характеристиках «Все объявления». Повторное добавление возвращает редактор.
Пустой текст допустим; его очистка создаёт запись истории, а не отказ.
Ограничение ввода — 20 000 символов; комментарий риелтора — прежние 2000.
Старые заметки и впечатления не копируются в новое поле.

PATCH /api/listings/{id}/family-comment:
property_id, family_comment_version, text.

Существующий POST /api/listings/{id}/realtor-review теперь обязательно принимает
property_id и realtor_version вместе со status/comment. Это намеренное
устранение возможности записи без проверки версии. Старый открытый браузер
нужно перезагрузить; новые asset hashes предотвращают старый кеш.
Запрос без версии — 422, устаревшая версия/property после merge — 409.
Ни один конфликт не перезаписывает чужой текст. UI сохраняет черновик,
показывает актуальный текст и требует явного выбора продолжения с черновиком
перед следующей попыткой сохранения. Поля истории выводятся только как текст.
Сессии, CSRF, origin и проверки ролей сохранены.

## SQLite

Новые поля properties (ALTER TABLE ADD COLUMN):
- family_comment TEXT NOT NULL DEFAULT '';
- family_comment_version INTEGER NOT NULL DEFAULT 0;
- family_comment_updated_at TEXT;
- family_comment_updated_by INTEGER REFERENCES users(id).

Новая property_comment_history содержит property_id, исходные origin_property_id
и origin_listing_id, kind (family/realtor), text, status, changed_at, changed_by,
version. Индекс property_comment_history_owner(property_id,id).
Маркер property_comments_history_v1 однократно сохраняет прежние оценки
риелтора в историю. Новые и очищенные семейные тексты добавляются в той же
транзакции, что версия и текущий текст. Повтор одинакового семейного текста
не создаёт дополнительную ревизию.

Миграция additive и идемпотентна. Не удаляет и не пересоздаёт таблицы.
Не меняет прежний маркер our_list_initial_selection_v1 и не повторяет
первоначальный выбор по истории просмотров. Неизвестные поля, индексы,
триггеры и сессии сохраняются.

При ручном подтверждении дублей существующий merge вызывает сохранение
комментариев: истории переносятся к итоговому property с исходными ID.
Если заполнены разные тексты обеих квартир, текущий текст включает оба
с явным происхождением и без обрезания. В исходных строках истории остаются
отдельные тексты и авторы. Версия повышается. Правила поиска дублей не меняются.

В конец листа Excel «Объявления» добавлен «Наш комментарий».
Прежние столбцы, лист «Просмотры», заметки и причины отказа сохранены.
Формулы в пользовательском тексте экранируются.

## Проверки и скриншоты

Полный XATA checks включает старые regression tests, новые API/SQLite tests,
Node tests, Chromium UI и WebKit UI. Новые UI-сценарии идут без сети к ЦИАН
и другим внешним сервисам через настоящий FastAPI TestClient:
360, 390, 430, 768, 1440 px; смена ориентации/ширины с открытым черновиком;
права, конфликты и 503, очистка/отмена, обе страницы и счётчики,
характеристики, календарь и сравнение.

Артефакт GitHub Actions xata-ui-screenshots содержит PNG:
{chromium,webkit}-{width}-{all,ours,comment-editor}.png.
Это эмуляция браузерных размеров/касания, не испытание на физическом телефоне.
Установка WebKit может зависеть от runner; не считать пропущенный прогон
успешным. Реальный OMV и живой ЦИАН не проверяются.

## Резервное копирование существующего OMV

Ниже команды выполняет владелец на OMV **из существующего каталога проекта**.
Сначала проверьте, что используется тот же Compose project xata и /data.
Не создавайте новый каталог установки и не меняйте project/volumes.
Не используйте down -v, volume rm/prune и не подменяйте рабочий .env.

Команды для bash; APP_PASSWORD и токены не выводятся:

~~~bash
set -eu
git status --short
docker compose -p xata ps
docker compose -p xata config --quiet

XATA_BACKUP_DIR="$(mktemp -d "$(dirname "$PWD")/xata-backup-$(date +%Y%m%d-%H%M%S)-XXXXXX")"
chmod 700 "$XATA_BACKUP_DIR"
git rev-parse HEAD > "$XATA_BACKUP_DIR/code-sha"
cp -p .env "$XATA_BACKUP_DIR/runtime.env"
chmod 600 "$XATA_BACKUP_DIR/runtime.env"
XATA_APP_ID="$(docker compose -p xata ps -q app)"
test -n "$XATA_APP_ID"
docker inspect --format '{{.Image}}' "$XATA_APP_ID" > "$XATA_BACKUP_DIR/app-image-id"
XATA_SAVED_IMAGE="$(cat "$XATA_BACKUP_DIR/app-image-id")"
docker image tag "$XATA_SAVED_IMAGE" "xata-app:rollback-$(date +%Y%m%d-%H%M%S)"

docker compose -p xata exec -T app python - <<'PY'
from pathlib import Path
import sqlite3
directory=Path('/data/backups')
directory.mkdir(exist_ok=True)
source=sqlite3.connect('/data/xata.sqlite3')
destination=sqlite3.connect('/data/backups/before-ui-comments.sqlite3')
try:
    source.backup(destination)
    assert destination.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
finally:
    destination.close()
    source.close()
Path('/data/backups/before-ui-comments.sqlite3').chmod(0o600)
PY
docker compose -p xata cp app:/data/backups/before-ui-comments.sqlite3 "$XATA_BACKUP_DIR/xata.sqlite3"
# Если уже есть локальные изображения:
if docker compose -p xata exec -T app python -c "from pathlib import Path; raise SystemExit(not Path('/data/images').exists())"; then
    docker compose -p xata cp app:/data/images "$XATA_BACKUP_DIR/images"
fi
printf 'Резервная копия: %s\n' "$XATA_BACKUP_DIR"
~~~

SQLite backup API делает согласованный snapshot работающей БД, включая WAL;
простое копирование только xata.sqlite3 во время записи не заменяет этот шаг.
Скопируйте приватный backup на отдельный носитель. Browser profile остаётся
в своём volume; эти команды его не изменяют.

## Обновление только app

Возьмите полный SHA из итогового отчёта и проверьте успешный CI этого SHA.
Не подставляйте SHA из текущего HEAD как доказательство review.

~~~bash
XATA_TARGET_SHA=ВСТАВЬТЕ_ПОЛНЫЙ_REVIEWED_SHA
test -z "$(git status --porcelain)"
git fetch origin xata/initial-app
git switch xata/initial-app
git merge --ff-only origin/xata/initial-app
test "$(git rev-parse HEAD)" = "$XATA_TARGET_SHA"
docker compose -p xata config --quiet
docker compose -p xata build app
docker compose -p xata up -d --no-deps --no-build app
docker compose -p xata ps app
docker compose -p xata logs --tail=80 app
~~~

Дождитесь healthy app, откройте / и /ours, перезагрузите старые вкладки.
Проверьте вход, счётчики, подборку и новый комментарий. Миграция выполняется
при старте app. Browser service, NPM, порт 3010 и named volume xata_xata_data
не пересоздаются. Сборка browser в CI — прежняя проверка совместимости,
а не требование обновлять его на OMV.

## Откат без потери новых пользовательских данных

Откатывается **образ app**, не БД. Сначала тем же SQLite backup API сохраните
ещё один snapshot текущей БД в отдельный файл after-ui-comments.sqlite3
и скопируйте его в backup-каталог. Сохраните также текущий код/image ID.
Не восстанавливайте before-ui-comments.sqlite3 поверх БД, в которую уже
поступили комментарии, оценки, просмотры, сессии или импорт.

~~~bash
test -d "$XATA_BACKUP_DIR"
XATA_SAVED_IMAGE="$(cat "$XATA_BACKUP_DIR/app-image-id")"
export XATA_SAVED_IMAGE
python3 - "$XATA_BACKUP_DIR/rollback.json" <<'PY'
import json,os,sys
from pathlib import Path
path=Path(sys.argv[1])
path.write_text(json.dumps({'services':{'app':{'image':os.environ['XATA_SAVED_IMAGE']}}}))
path.chmod(0o600)
PY
docker compose -p xata -f docker-compose.yml -f "$XATA_BACKUP_DIR/rollback.json" config --quiet
docker compose -p xata -f docker-compose.yml -f "$XATA_BACKUP_DIR/rollback.json" up -d --no-deps --no-build app
docker compose -p xata ps app
~~~

Новые столбцы и history остаются в текущем volume. Прежний app их не показывает,
но не должен удалять; возврат к новому образу снова откроет эти данные.
На старой версии временно не объединяйте property: прежний merge не знает
семейных комментариев и их происхождения. Для исправления повреждённой БД
нужен отдельный план восстановления/слияния текущих данных; автоматического
отката схемы или восстановления устаревшего dump здесь нет.
