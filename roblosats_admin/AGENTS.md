# roblosats_admin

Coordinator admin additions shipped in the Roblosats image (installed by
`robosats/settings_roblosats_extra.py`, appended to `settings.py` in `Dockerfile.roblosats`).
It patches the already-registered `api` admin classes in `ready()` instead of editing
`api/admin.py`, so upstream rebases stay small.

## Dispute panel (`disputes.py`)
- No money logic of its own. Resolving calls the existing `OrderAdmin` actions
  (`maker_wins`, `taker_wins`, `return_everything`, `successful_trade`) on a one-order queryset,
  with a stand-in request that only collects their messages. Those actions keep their
  `transaction.atomic`, `_lock_disputed` (advisory lock + `select_for_update`, open dispute,
  escrow `SETLED`) and ledger guards.
- Buyer/seller map to maker/taker by order type: on a BUY order the maker buys, on a SELL order
  the taker buys (`roles`, `action_for`).
- A resolve POST needs `confirmed=yes`, a known decision, and the order status the
  confirmation page was rendered with; otherwise nothing runs. The views go through
  `admin_site.admin_view`, so CSRF and staff login apply.

- Opening a disputed order's change page (GET) redirects to its dispute page; `?raw=1` keeps
  the Django form (the dispute page links to it with that parameter).
- The list takes `?view=open` (default: open disputes + last 30 past), `waiting` (status 16
  only) or `history` (open + up to 500 past). The sidebar's dispute entries and the dashboard
  link to these views, not to changelist filters.

## Order page (`orders.py`)
- `/coordinator/api/order/<id>/summary/` shows any order (live, finished, cancelled, expired)
  with the dispute page's partials: parties, amounts (`spamcoin_sats` when set), bonds,
  escrow, payout, the JSON log as a timeline, chat message count (the chat is PGP-encrypted
  between the parties, so only the count), coordinator messages and a write form while the
  order is live. Read-only apart from that form (`disputes.message_post`).
- The order list has an "Order page" column; opening any order's change page (GET) redirects
  here, or to the dispute page when `is_disputed`. `?raw=1` keeps the Django form.
- `_party_summary.html` prints the statement line only when `p.show_statement` (dispute page).

## Telegram alerts (`models.py`, `alerts.py`, `admin.py`)
- One row (`TelegramAlerts`, pk 1, migration `0001_roblosats_telegram_alerts`) edited at
  `/coordinator/roblosats_admin/telegramalerts/`, which `TelegramAlertsAdmin.changelist_view`
  replaces. Only users with `roblosats_admin.change_telegramalerts` (superusers) see or use it.
  The token is never sent back to the browser: the page shows its last 4 characters.
- Without a row, `alerts.current()` returns an unsaved default: all off except "dispute
  opened" when both `TELEGRAM_TOKEN` and `TELEGRAM_COORDINATOR_CHAT_ID` are set, so the
  upstream coordinator dispute message keeps arriving. Empty token / chat id fall back to those
  env vars. `api/notifications.py::dispute_opened` skips its own coordinator copy when this
  app is installed (that path loops forever and does not URL-encode), so it is never sent twice.
- Hooks: `api.models.order.order_status_changed` (sent by `Order.announce_status`, called from
  `log_status_transition`, i.e. `update_status`/`transition_status`, and from the two direct
  assignments in `Logics.publish_order` and `Logics.finalize_contract`), `post_save` of
  `chat.Message`, and `disputes.write_to` / `messaging.message_parties`. `event_for` maps
  transitions: into PUB (not from PAU) = published, WF2 = taken (taker bond locked, upstream's
  "contract formalized"), CHA from WF2/WFE/WFI = escrow + invoice in, SUC = finished,
  UCA/EXP/CCA except from WFB (a maker bond never locked is noise) = cancelled, DIS = dispute.
  Dispute chat alerts only for status 11/16 and at most once per `CHAT_NOTIFICATION_TIMEGAP`
  minutes per order.
- Safety: the signal uses `send_robust`; the hook reads the row, and only if the event is on
  queues `roblosats_telegram_alert` with `transaction.on_commit`; every step catches its errors
  (broad `except` on purpose), so Telegram, Redis or Celery failures never touch the order.
  The task re-reads the row (a toggle switched off meanwhile wins), builds the text in the
  chosen language and sends with bounded retries (`tasks.send_telegram`); errors are logged
  with the token redacted. Links use the configured panel address, else `http://HOST_NAME`.

## Dashboard and menu (`dashboard.py`, `robosats/settings_roblosats_extra.py`)
- `UNFOLD["DASHBOARD_CALLBACK"]` is `roblosats_admin.dashboard.dashboard`; the sidebar titles are
  `pgettext_lazy` with the same context. Settings must not import this app (it is loaded before
  the app registry), so the badge counters stay in the settings file and the menu permission is
  a dotted path (`roblosats_admin.dashboard.can_configure_alerts`).

## Messages (`messaging.py`, `tasks.py`)
- `send_coordinator_message` stores a `Notification` (title and description are 240 chars
  each, so the text is split across them), sends the full text as a Nostr DM through the
  existing task, and queues Telegram/webhook delivery in `relay_coordinator_message`
  (bounded retries, unlike `Notifications.send_telegram_message`, which loops forever).
- Prefix: `<COORDINATOR_ALIAS> coordinator (order N): `; the panel finds sent messages by it.

## Language (`middleware.py`, `i18n.py`, `locale/`)
- `AdminLocaleMiddleware` activates a language only for `/coordinator/` requests: the
  `django_language` cookie (set by `?lang=xx`), then Accept-Language, then `LANGUAGE_CODE`.
  API responses are not affected.
- Strings use the gettext context `roblosats-admin` so they never collide with Django's or
  Unfold's catalogs. `.po` and compiled `.mo` files are committed; edit the `.po` and
  recompile (`pybabel compile` or `msgfmt`) when strings change.

## Tests
`python manage.py test roblosats_admin` in a container built from `Dockerfile.roblosats`
(the plain upstream settings do not install this app). `CatalogTest` checks that the nine
`.po` files carry the same strings, all translated, with the same placeholders.
