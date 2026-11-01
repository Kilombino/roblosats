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
(the plain upstream settings do not install this app).
