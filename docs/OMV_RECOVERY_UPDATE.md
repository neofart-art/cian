# OMV: обновление при отсутствующем каталоге исходников

Эта инструкция предназначена для владельца сервера. В среде разработки deployment не выполняется. В этом обновлении меняется только app; browser с авторизованным профилем, Nginx Proxy Manager и другие проекты остаются работающими.

**Сначала проверяется фактическая конфигурация. До её восстановления и сверки контейнер не пересоздавать.** Docker labels с путём compose не доказывают существование каталога. Данные находятся в существующем named volume `xata_xata_data`; новый пустой volume создавать нельзя.

## 1. Только чтение: найти контейнер и сохранить факты

Команды выполняются на OMV, под пользователем с доступом к Docker. Не публикуйте полный inspect: в нём есть значения окружения и секреты.

~~~sh
docker ps --format 'table {{.ID}}\t{{.Names}}\t{{.Ports}}'
~~~

Найдите app по `172.31.1.1:3010->8000/tcp`. Запишите ID, не выбирайте первый попавшийся контейнер. Укажите **существующий диск данных OMV**; путь ниже нужно заменить на свой. Старый отсутствующий каталог не используем.

~~~sh
umask 077
XATA_APP_ID='ФАКТИЧЕСКИЙ_ID_APP'
XATA_RECOVERY_ROOT='/srv/dev-disk-by-uuid-ВАШ_UUID/appdata/xata-recovery'
XATA_REVIEWED_SHA='ИТОГОВЫЙ_SHA_ИЗ_ОТЧЁТА'
mkdir -p "$XATA_RECOVERY_ROOT"
chmod 700 "$XATA_RECOVERY_ROOT"
git clone --branch xata/initial-app https://github.com/neofart-art/cian.git "$XATA_RECOVERY_ROOT/source"
git -C "$XATA_RECOVERY_ROOT/source" checkout --detach "$XATA_REVIEWED_SHA"
git -C "$XATA_RECOVERY_ROOT/source" rev-parse HEAD
XATA_BACKUP="$XATA_RECOVERY_ROOT/backup-$(date -u +%Y%m%dT%H%M%SZ)"
python3 "$XATA_RECOVERY_ROOT/source/scripts/omv_snapshot.py" --container "$XATA_APP_ID" --output "$XATA_BACKUP"
~~~

Скрипт проверяет фактический bind и mount; при несовпадении прекращает работу. Он **не останавливает и не пересоздаёт контейнер**, не импортирует app.main и не выполняет миграции production. Сохраняет в приватном каталоге:

- Полные container/image/volume inspect, docker diff и безопасный report.json.
- Фактические файлы рабочего каталога контейнера (`runtime/app` при обычном WORKDIR=/app), включая локальные изменения UI.
- Найденные исходные compose/.env, если они существуют по labels. Отсутствующие пути перечисляются в report.json.
- Согласованную SQLite-копию через стандартный `sqlite3.backup()` с integrity_check/foreign_key_check и локальные изображения.
- Текущий образ в `app-image.tar` для отката.

SQLite-копия также остаётся под новым уникальным именем в `/data/backups`. Ничего из volume не удаляется. Нужны свободное место для DB/фото/образа и Docker-доступ; при ошибке частичная приватная копия сохраняется. Копирование фото не является транзакцией SQLite; во время обновления не запускайте сбор/импорт, основной rollback использует действующий volume. Browser profile этим скриптом не открывается; его резервируют отдельно при остановленном browser по прежней инструкции, если это нужно.

## 2. Сохранить локальные изменения кода

`runtime/app` — источник фактических локальных правок, а не предположение по ветке Git. Не копируйте этот каталог поверх нового app целиком: так можно вернуть старый JS/API и потерять оптимизацию.

Если исходный commit образа подтверждён (например OCI label `org.opencontainers.image.revision` или вашим журналом обновлений), создайте **отдельный** worktree в backup и патч. Значение BASE_SHA должно быть фактическим; нельзя автоматически считать им 21c0d740.

~~~sh
XATA_BASE_SHA='ПОДТВЕРЖДЁННЫЙ_ИСХОДНЫЙ_SHA'
git -C "$XATA_RECOVERY_ROOT/source" fetch origin "$XATA_BASE_SHA"
git -C "$XATA_RECOVERY_ROOT/source" worktree add --detach "$XATA_BACKUP/local-base" "$XATA_BASE_SHA"
python3 - "$XATA_BACKUP" <<'PY'
import shutil,sys
from pathlib import Path
backup=Path(sys.argv[1]).resolve()
target=backup/'local-base'/'app'
source=backup/'runtime'/'app'
assert source.is_dir() and target.is_dir()
shutil.rmtree(target)  # Only the new isolated worktree copy, never the runtime app.
shutil.copytree(source,target,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
PY
git -C "$XATA_BACKUP/local-base" diff --binary -- app > "$XATA_BACKUP/local-app.patch"
~~~

Просмотрите патч приватно. В новой версии уже включены «Риелтор», зелёные понравившиеся и отсутствие голубого оформления. Соответствующие старые CSS-hunks не нужно повторно применять. Остальные подтверждённые изменения перенесите по отдельности в новый checkout; при подходящей базе можно применить проверенный патч через `git apply --3way`, разрешив конфликты и просмотрев итоговый diff. Сохраните новый diff вне Git с секретами и запишите локальный commit без .env/DB/фото. Не используйте reset --hard для удаления правок.

Если BASE_SHA неизвестен, выполните приватное сравнение `runtime/app` с новым `source/app` и переносите только понятные изменения вручную. Не применяйте неизвестный патч автоматически. Сохранённый runtime позволяет вернуться к исходному состоянию. Секреты в Git не добавлять.

## 3. Восстановить и проверить конфигурацию — обязательный рубеж

Из приватных inspect и сохранённого compose сверяются:

- Compose project/service, имя app-контейнера, command/entrypoint, пользователь, рабочий каталог.
- **Точно `172.31.1.1:3010 -> 8000`**, mount существующего **`xata_xata_data:/data`**, остальные mounts и локальные bind правки.
- Все фактические environment (особенно PUBLIC_URL, DATA_DIR, BROWSER_SERVICE_URL/API_TOKEN, расписание и CIAN_SEARCH_URL); секреты не печатать.
- Существующие Docker networks/aliases и доступ к уже работающему browser; текущий browser не пересоздавать.
- read_only, tmpfs, memory/CPU/pids, capabilities/security_opt, restart, healthcheck и logging.
- Отсутствие незапланированных изменений портов, volumes, NPM, других services.

Если исходный compose и .env отсутствуют, **на этом месте обновление приостанавливается**. Их сначала восстанавливают по container/image inspect в приватном каталоге и проверяют, а не подменяют примером из репозитория. Docker inspect не хранит исходный compose целиком и не гарантирует точного восстановления всех его настроек. Можно передать для помощи только обезличенный структурный отчёт, без секретных Env. Не запускайте `up` из нового clone с настройками по умолчанию.

Для восстановленного compose явно закрепите существующий volume. Например, если логическое имя в нём `xata_data`:

~~~yaml
volumes:
  xata_data:
    external: true
    name: xata_xata_data
~~~

Аналогично закрепите фактическую существующую network как external с её реальным именем, если восстановленная конфигурация иначе создаёт новую. Не менять browser_profile и не переименовывать проект. Пути bind mounts и build context проверяются отдельно: перемещение source не должно менять существующие данные или заставлять старый bind-код перекрывать новый образ.

После восстановления укажите проверенные пути. Эти команды только проверяют конфигурацию и строят **app**, контейнеры не заменяют:

~~~sh
XATA_COMPOSE_PROJECT='ФАКТИЧЕСКИЙ_PROJECT_ИЗ_INSPECT'
XATA_COMPOSE_FILE='/АБСОЛЮТНЫЙ/ПУТЬ/К/ПРОВЕРЕННОМУ/compose.yml'
XATA_ENV_FILE='/АБСОЛЮТНЫЙ/ПУТЬ/К/ВОССТАНОВЛЕННОМУ/.env'
docker compose --env-file "$XATA_ENV_FILE" -p "$XATA_COMPOSE_PROJECT" -f "$XATA_COMPOSE_FILE" config --quiet
docker compose --env-file "$XATA_ENV_FILE" -p "$XATA_COMPOSE_PROJECT" -f "$XATA_COMPOSE_FILE" config --format json > "$XATA_BACKUP/candidate.compose.private.json"
chmod 600 "$XATA_BACKUP/candidate.compose.private.json"
docker compose --env-file "$XATA_ENV_FILE" -p "$XATA_COMPOSE_PROJECT" -f "$XATA_COMPOSE_FILE" build app
~~~

Полный config содержит секреты; его не публикуют. Сверьте candidate с фактическим inspect по пунктам выше. Проверьте собранный образ отдельно на **копии** DB, не подключая production volume: integrity_check/foreign_key_check, число property/подборки/просмотров/комментариев и повторный запуск миграций. В копии отключите расписание и не подключайте сеть ЦИАН. Не импортируйте main в production через docker exec только ради проверки: это запустит миграции старого контейнера.

## 4. Только после успешной сверки: применение владельцем

Этот раздел применим лишь после выполнения рубежа 3. Фактическая конфигурация OMV в среде Codex не проверялась, поэтому команды ниже нельзя считать разрешением на слепое пересоздание.

Объявите короткое окно без пользовательских изменений/сбора, повторите snapshot в новый backup-каталог непосредственно перед заменой app. Убедитесь, что старый образ сохранён и локальные правки перенесены. Затем владелец выполняет:

~~~sh
docker compose --env-file "$XATA_ENV_FILE" -p "$XATA_COMPOSE_PROJECT" -f "$XATA_COMPOSE_FILE" up -d --no-deps --no-build app
docker compose --env-file "$XATA_ENV_FILE" -p "$XATA_COMPOSE_PROJECT" -f "$XATA_COMPOSE_FILE" ps app
docker compose --env-file "$XATA_ENV_FILE" -p "$XATA_COMPOSE_PROJECT" -f "$XATA_COMPOSE_FILE" logs --tail=80 app
~~~

Нужна только пересборка app. Не запускать `down`, `down -v`, `volume rm`, `prune`, `up --remove-orphans` или обновление browser/NPM. Named volume остаётся тем же. Миграция additive; текущие sessions сохраняются.

В браузере откройте обе страницы. До первого ответа видно «Загрузка…». В DevTools проверьте:

- State all/ours: Content-Encoding: gzip, Vary: Accept-Encoding, Cache-Control: no-store, Server-Timing.
- Ours содержит только подборку и общие totals; повтор без правок — 304, тело 0 байт.
- Скрытая вкладка прекращает опрос, фоновые ошибки не очищают список.
- Комментарий риелтора сохраняется и виден после reload; отдельно проверьте дату, календарь, «ОК», результаты/группы, сравнение, фото и Excel.

Для повторения исходного замера смотрите TTFB и загрузку тела по отдельности. `sql` с большой задержкой укажет на чтение/ожидание SQLite, `assemble/serialize` — на Python, разница между `app` и фактическим TTFB — на дополнительную обработку/сеть/proxy. NPM здесь не перенастраивается.

## 5. Откат без потери новых пользовательских данных

Возвращается **код/образ**, а не старая SQLite. Предварительно сохраните свежую копию текущей DB и текущий runtime через тот же snapshot. Существующий volume не менять, старую backup DB поверх него не копировать: иначе потеряются новые комментарии, просмотры и история.

~~~sh
docker image load --input "$XATA_BACKUP/app-image.tar"
~~~

Image ID возьмите из соответствующего `image.inspect.json`; задайте ему локальный rollback tag, создайте приватный override только `services.app.image` с этим tag и выполните после проверки config ту же команду `up -d --no-deps --no-build app` с override. Пример после подстановки подтверждённого image ID:

~~~sh
XATA_ROLLBACK_IMAGE_ID='sha256:ФАКТИЧЕСКИЙ_ID_СОХРАНЁННОГО_ОБРАЗА'
docker image tag "$XATA_ROLLBACK_IMAGE_ID" xata-app:rollback-local
python3 - "$XATA_BACKUP/rollback.compose.json" <<'PY'
import json,sys
from pathlib import Path
path=Path(sys.argv[1])
path.write_text(json.dumps({'services':{'app':{'image':'xata-app:rollback-local'}}}))
path.chmod(0o600)
PY
docker compose --env-file "$XATA_ENV_FILE" -p "$XATA_COMPOSE_PROJECT" -f "$XATA_COMPOSE_FILE" -f "$XATA_BACKUP/rollback.compose.json" config --quiet
docker compose --env-file "$XATA_ENV_FILE" -p "$XATA_COMPOSE_PROJECT" -f "$XATA_COMPOSE_FILE" -f "$XATA_BACKUP/rollback.compose.json" up -d --no-deps --no-build app
~~~

Если приложение имело bind mounts с локальным кодом, верните и их подтверждённое прежнее содержимое из runtime backup; один rollback image не перекроет bind. Новые таблица/индекс/triggers совместимы с предыдущим кодом; проверка предыдущего приложения на копии выполняется CI. Произвольно старые версии без такой проверки не используйте. Восстановление DB из backup — отдельная аварийная процедура с остановкой записи и оценкой потери изменений, не штатный rollback этого патча.
