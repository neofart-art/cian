# XATA — UI, review statuses, and multi-user access

## Repository / branch

Work in repository `neofart-art/cian`, branch `xata/initial-app`.

XATA is already working on OMV in production. Do not break or redesign unrelated working functionality.

Do **not** deploy to OMV. After changes:
1. run the full CI;
2. commit to `xata/initial-app`;
3. report the commit SHA;
4. list changed files;
5. summarize migrations;
6. report test counts / Compose / browser / noVNC checks;
7. provide a safe step-by-step OMV upgrade procedure including a SQLite backup.

## Existing production functionality that must keep working

Preserve:
- browser collector through persistent Chromium;
- the authenticated Cian browser profile;
- scheduled browser searches;
- `CIAN_SEARCH_URL`;
- CSV import;
- XLSX export;
- the existing SQLite database and all existing listings;
- existing `first_seen` values;
- Docker Compose project name `xata`;
- existing volumes, especially `xata_xata_data` and `xata_browser_profile`;
- noVNC/manual Cian login flow;
- current browser-container security restrictions;
- current browser snapshot/CAPTCHA/login detection behavior unless a change is strictly required for this task.

Do not execute destructive Docker commands such as:
- `docker compose down -v`
- `docker volume prune`

---

# 1. Goal

Turn XATA into a practical shared apartment-review tool for:
- the owner/admin;
- the owner's wife;
- a realtor.

Users must be able to:
- view listings found by XATA;
- open the original listing on Cian quickly;
- mark listings as reviewed;
- mark listings as unsuitable;
- immediately identify unreviewed/new listings;
- see who last changed a listing status and when.

The admin additionally manages search, import, export, and users.

---

# 2. Use the full desktop width

The current UI wastes space because of constraints such as:

```css
main { max-width:1200px; }
.table-wrap table { min-width:1450px; }
```

Requirements:
- use almost the full available desktop width;
- keep sensible left/right page padding;
- remove the hard `max-width:1200px` restriction;
- remove the forced `min-width:1450px` table rule;
- on a normal desktop monitor, the main listings table should fit without a horizontal scrollbar;
- allow the ЖК/address cell to wrap;
- keep the mobile layout usable;
- horizontal scrolling is acceptable only on genuinely narrow/mobile widths;
- make the top panels use the wider layout without looking unnecessarily stretched.

Preserve the existing light / green / minimal visual style.

---

# 3. Listings table redesign

Final column order:

| № ЦИАН | ЖК / адрес | Цена | Площадь | Этаж | Метро | До метро | Впервые найдено | Статус |
|---|---|---:|---:|---|---|---|---|---|

## 3.1 № ЦИАН

The first column must show the existing `listing.id`.

The listing ID itself must be the link to `listing.url`.

Requirements:
- opens in a new tab;
- `target="_blank"`;
- `rel="noopener noreferrer"`;
- retain the current safe Cian URL validation or an equivalent server/client-safe validation.

There must be **no separate "Ссылка" column**.

## 3.2 ЖК / адрес

Keep the existing concept:
- main line = ЖК or listing title;
- smaller line = address;
- the current expandable "Текст карточки / описание" section may remain.

## 3.3 Remove these columns from the main UI

Remove from the screen table:
- `Последнее обновление`;
- `Последний импорт`;
- `Источник`.

Do **not** delete these fields from SQLite. They may remain in API data and XLSX for diagnostics/history.

---

# 4. Per-listing review status

Add a separate user-managed review state for each listing.

Allowed states:

```text
new
viewed
rejected
```

UI labels:

```text
new       -> НОВОЕ
viewed    -> Просмотрено
rejected  -> Не подходит
```

For existing listings after migration, default to:

```text
new
```

This is a **user-owned field**, not a Cian-derived field.

Browser collection, CSV import, and later listing refreshes must never overwrite this state.

Persist at least:
- `review_status`
- `review_updated_at`
- `review_updated_by`

Prefer `review_updated_by` to reference a stable user ID so renaming a user does not destroy history.

Migration must be additive and idempotent.

Do not:
- recreate `listings` from scratch;
- delete listings;
- change listing IDs;
- lose `first_seen`.

---

# 5. Review controls in the table

In the `Статус` column provide compact controls to:
- mark `Просмотрено`;
- mark `Не подходит`;
- reset back to `НОВОЕ`.

Do not render oversized buttons in every row. Compact buttons / segmented control / dropdown are all acceptable.

After a status change:
- do not reload the full page;
- call the API;
- update the row state;
- display the actual saved status.

Also show, unobtrusively, who last changed it and when, for example:

```text
Максим · 16.10 15:42
```

---

# 6. Visual states

Status must not be represented by color alone.

## new
- normal readable row;
- visible compact label `НОВОЕ`.

## viewed
- slightly muted row;
- explicit text label `Просмотрено`;
- still easily readable.

## rejected
- clearly more muted;
- explicit text label `Не подходит`;
- do not remove it from the table;
- Cian link must still work.

---

# 7. Auto-refresh compatibility

The page currently refreshes `/api/state` periodically.

Keep automatic refresh.

It must not:
- reset review statuses;
- overwrite a just-changed status with stale state;
- interfere with status buttons;
- cause unnecessary visual jumping.

`/api/state` must include all review fields required by the UI.

---

# 8. Multi-user authentication

Replace the current single shared HTTP Basic model based on `APP_USER/APP_PASSWORD` with a proper local multi-user XATA authentication system.

Needed users:
- owner/admin;
- wife;
- realtor.

Do **not** hardcode wife/realtor credentials. The admin will create those accounts after deployment.

Roles:

```text
admin
reviewer
```

## admin permissions

May:
- view listings;
- open Cian links;
- change review status;
- manually start browser search;
- view browser-collector state;
- import CSV;
- export XLSX;
- manage users.

## reviewer permissions

May:
- view listings;
- open Cian links;
- expand listing/card text;
- change review status between `new`, `viewed`, `rejected`;
- see who changed status and when.

Must **not** be allowed to:
- manually run browser collection;
- change monitoring/search settings;
- import CSV;
- manage users;
- enable/access manual browser/noVNC controls;
- call administrative APIs.

Keep XLSX export admin-only unless there is a strong reason otherwise.

Authorization must be enforced server-side. Hiding buttons in JavaScript is not sufficient.

---

# 9. Login page and session model

Remove dependence on the browser HTTP Basic popup.

Implement a normal XATA login page.

Example:

```text
ХАТА

Логин
[             ]

Пароль
[             ]

[ Войти ]
```

Use server-side authenticated sessions.

Session cookie requirements:
- `HttpOnly`
- `Secure`
- `SameSite=Lax` or stricter where appropriate

The session must:
- have a reasonable lifetime;
- be invalidated on logout;
- never store the plaintext password;
- never store password/hash in localStorage or sessionStorage.

After login, show current identity in the upper-right area, e.g.:

```text
Maksim · Администратор · Выйти
```

or for reviewer:

```text
Anna · Просмотр · Выйти
```

---

# 10. User storage

Add a SQLite `users` table through an additive migration.

Suggested fields:
- `id`
- `username`
- `display_name`
- `password_hash`
- `role`
- `enabled`
- `created_at`
- `updated_at`

An equivalent schema is acceptable if justified.

Requirements:
- unique username;
- store passwords only as secure password hashes;
- prefer Argon2id;
- bcrypt is acceptable if better aligned with dependencies;
- never store plaintext passwords;
- never expose password hashes through API;
- disabled users cannot log in;
- disabled users must no longer be able to continue authenticated use.

---

# 11. Safe bootstrap migration from current APP_USER / APP_PASSWORD

Production OMV already has:

```text
APP_USER
APP_PASSWORD
```

The owner must not lose access after upgrade.

On the first run of the new version:
- if the users table is empty;
- create the first `admin` user from existing `APP_USER` / `APP_PASSWORD`;
- store only a password hash;
- never log the password;
- make the bootstrap idempotent.

After successful bootstrap, normal application authentication must use SQLite users + sessions.

`APP_USER/APP_PASSWORD` may remain in `.env` for bootstrap/backward-safe migration, but must not remain the shared runtime auth mechanism.

Do not automatically delete or rewrite the user's `.env`.

---

# 12. User management

Add an admin-only user-management page or compact panel.

Capabilities:
- create user;
- set username;
- set display name;
- set role (`admin` / `reviewer`);
- set password;
- change/reset password;
- enable/disable user.

Prefer disabling over physical deletion so `review_updated_by` history remains valid.

Protect against accidentally disabling/removing the last active admin.

Never show:
- existing plaintext passwords;
- password hashes.

---

# 13. Review API

Add a dedicated endpoint, for example:

```text
POST /api/listings/{id}/review
```

Request body example:

```json
{
  "status": "viewed"
}
```

Only allow:

```text
new
viewed
rejected
```

Validate:
- authenticated user;
- valid status;
- listing exists.

Persist:
- review status;
- current user ID;
- UTC timestamp.

Both `admin` and `reviewer` may call this endpoint.

---

# 14. CSRF / request security

Keep the existing Origin / `Sec-Fetch-Site` protections.

Because auth becomes session-cookie based, add proper CSRF protection for state-changing requests.

Do not weaken the current CSP.

Preserve security headers including at least:
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy`
- `Content-Security-Policy`

Do not add external JavaScript/CDNs unless necessary.

---

# 15. Do not disturb the working browser collector

The real Cian browser search is working now.

Do not change, unless strictly required:
- `collector/service.py`;
- browser snapshot algorithm;
- CAPTCHA/login detection;
- persistent Chromium profile;
- noVNC/manual login mechanism;
- `BROWSER_SEARCH_INTERVAL_SECONDS`;
- scheduler;
- `CIAN_SEARCH_URL`;
- Cian-only navigation restrictions;
- browser API token mechanism;
- Docker network isolation.

Do not perform opportunistic refactors of working browser logic as part of this task.

---

# 16. Listing refresh behavior

`save_items()` may continue to refresh ordinary listing fields, but must never modify:

```text
review_status
review_updated_at
review_updated_by
```

Example:

```text
listing 123
-> realtor marks "Не подходит"
-> 30 minutes later browser collector finds listing 123 again
-> price/last_seen/etc. may update
-> review state remains "Не подходит"
```

The same rule applies to CSV import.

---

# 17. Newly discovered listings

When browser collector finds a previously unknown listing ID:
- create it as usual;
- keep current `first_seen` behavior;
- set `review_status = new`.

The UI must show a visible:

```text
НОВОЕ
```

This allows all reviewers to spot fresh, unprocessed listings after scheduled monitoring.

---

# 18. Display ordering

For the saved-listings table, prefer:

1. `new`
2. `viewed`
3. `rejected`

Inside each group:
- newer `first_seen` first.

This sorting applies only to XATA's saved-listings display, not to the Cian browser collector itself.

---

# 19. Quick status filters

Add a compact client-side filter above the table:

```text
Все | Новые | Просмотренные | Не подходят
```

Default:
- `Все`

Show counts if practical, e.g.:

```text
Новые 7
Просмотренные 15
Не подходят 23
```

No new network request is needed for every filter click; filter already-loaded rows/client state.

---

# 20. XLSX export

Keep XLSX export working.

Add these fields if practical:
- `Статус`
- `Статус изменил`
- `Дата изменения статуса`

Keep diagnostic/history fields such as:
- last update;
- last import;
- source

in XLSX if useful, even though they are removed from the main on-screen table.

Do not break spreadsheet formula-injection protection.

---

# 21. Existing production database compatibility

Upgrade must work directly on the current `xata_xata_data` volume without manual SQL.

Preserve:
- all listings;
- IDs;
- `first_seen`;
- `last_seen`;
- CSV data;
- browser state;
- runs;
- import data/history.

All migrations must be additive and idempotent.

---

# 22. Tests

Add tests covering at least:
- migration from an old SQLite database without the new review/user fields;
- preservation of existing listing data;
- preservation of `first_seen`;
- bootstrap admin from `APP_USER/APP_PASSWORD`;
- password is not stored plaintext;
- successful login;
- invalid password;
- logout;
- disabled user;
- admin permissions;
- reviewer permissions;
- reviewer can change review status;
- reviewer cannot start browser collect;
- reviewer cannot import CSV;
- reviewer cannot manage users;
- invalid review status -> 4xx;
- nonexistent listing -> 404;
- browser refresh does not erase review state;
- CSV refresh does not erase review state;
- review author/time are preserved correctly;
- CSRF protection;
- Origin protection;
- existing browser collector test suite;
- existing CSV tests;
- both Compose configurations;
- browser/noVNC smoke checks.

Do not delete existing tests just to make CI pass.

---

# 23. UI / UX style

Keep the current XATA style:
- light background;
- green palette;
- calm minimal interface;
- existing `ХАТА` branding;
- no heavy frontend framework.

The primary screen must remain focused on quickly reviewing apartments, not look like a generic admin dashboard.

Information priority:

```text
№ ЦИАН -> объект -> цена -> характеристики -> статус
```

---

# 24. Performance

Avoid one SQL query per listing row.

`/api/state` should retrieve listing and review/user display data efficiently.

Do not add heavy frontend dependencies unless truly needed.

---

# 25. Logging / secrets

Never log:
- plaintext passwords;
- password hashes;
- session cookies;
- CSRF secret/tokens;
- browser API tokens;
- Cian cookies;
- Cian HTML;
- Chromium profile contents.

Safe to log:
- user ID;
- username;
- action type;
- listing ID;
- error type.

---

# 26. Docker / dependency constraints

Do not:
- rename Compose project;
- rename/replace `xata_data`;
- create a new empty data volume in place of production data;
- touch/remove `xata_browser_profile`.

If secure password hashing requires a new Python dependency, update requirements and Docker build appropriately.

Prefer keeping this feature work in the main `app` container. Avoid changing the browser image unless truly necessary.

---

# 27. Definition of Done

The task is complete when all of these are true:

- existing production SQLite upgrades automatically;
- owner/admin does not lose access during migration from Basic Auth;
- admin can create separate accounts for wife and realtor;
- each user logs in with their own credentials;
- reviewer can review listings but cannot use admin functions;
- `№ ЦИАН` is the first column and is the clickable Cian link;
- there is no separate link column;
- `Последнее обновление / Последний импорт / Источник` are removed from the main screen;
- desktop layout uses the available width and avoids unnecessary horizontal scrolling;
- listing review states `НОВОЕ / Просмотрено / Не подходит` work;
- review state survives browser refresh / CSV update;
- last reviewer and review timestamp are visible;
- browser collector still works;
- scheduler still works;
- CSV import still works;
- XLSX still works;
- noVNC / persistent profile still work;
- full CI passes.

After completion:
1. commit everything to `xata/initial-app`;
2. provide the commit SHA;
3. list changed files;
4. summarize migrations;
5. state how many tests passed;
6. confirm Compose and browser/noVNC checks;
7. provide a safe OMV deployment/upgrade sequence with backup first;
8. do not perform deployment yourself.
