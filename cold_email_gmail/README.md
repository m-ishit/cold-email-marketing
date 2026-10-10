# Cold Email (Gmail API)

Schedule one-off emails in Odoo 19 Community and send them through Gmail
**entirely over its REST API**. No SMTP anywhere: no `ir.mail_server`, no
`mail.mail`, no `fetchmail.server`, no app password. Every operation is an HTTPS
call to `gmail.googleapis.com`.

- **Author:** Hirebeans Cloud — <https://hirebeans.cloud>
- **Depends on:** `mail` only
- **Python:** `requests` (no `google-api-python-client`, no `google-auth`)
- **Scopes:** `gmail.send`, `gmail.readonly`
- **License:** OPL-1

---

## 1. Why not the `google_gmail` addon?

Odoo ships `google_gmail`, but it uses OAuth only to mint a bearer token and
then hands that token to **SMTP** as an XOAUTH2 password:

```python
# addons/google_gmail/models/ir_mail_server.py
connection.docmd('AUTH', f'XOAUTH2 {oauth_param}')
```

Its default scope is `https://mail.google.com/` — the SMTP/IMAP scope, not an
API scope. Depending on it would also force three concessions:

- `_fetch_gmail_token` hardcodes the `/google_gmail/confirm` redirect URI, so we
  would be locked into its controller and its callback URL.
- `open_google_gmail_uri` hard-fails for anyone who is not `env.is_admin()`,
  which breaks per-user mailboxes.
- It carries Enterprise-only IAP branches and shares one set of client
  credentials with any Gmail SMTP server on the same database.

So this module brings its own OAuth flow, controller, token store and API
client. It still reuses generic core `mail` infrastructure (chatter, activities
and `mail.render.mixin` for placeholders), because that has nothing to do with
the SMTP connector.

---

## 2. Google Cloud setup (do this first)

Nothing works until an OAuth client exists.

1. Create a project at <https://console.cloud.google.com>.
2. **APIs & Services → Library → enable the Gmail API.**
3. **OAuth consent screen:**
   - *Internal* if everyone using it is on your Google Workspace domain — no
     review needed, and refresh tokens are long-lived.
   - *External* otherwise. Add each sender under **Test users**.
4. **Credentials → Create credentials → OAuth client ID → Web application.**
5. Under **Authorised redirect URIs**, add exactly:

   ```
   https://your-odoo-domain.com/cold_email/oauth/callback
   ```

   For local development: `http://localhost:8069/cold_email/oauth/callback`.
   The Settings page displays the exact string to paste — copy it from there.
6. Copy the Client ID and Client Secret into **Settings → Cold Email**.

> **Testing-mode caveat.** While the consent screen is *Testing*, Google expires
> refresh tokens after **7 days**. The mailbox flips to *Authorisation Expired*
> and **Reconnect** fixes it. This is Google's behaviour, not a defect. Only an
> *Internal* app or a published/verified app gets long-lived tokens.

---

## 3. Installation

```bash
./odoo-bin -c <conf> -d <db> -i cold_email_gmail
```

Then grant users a role under **Settings → Users → Cold Email**:

| Group | Can do |
|---|---|
| **User** | Connect their own mailbox, schedule and manage their own emails, use templates and tags |
| **Manager** | Everything above, plus every mailbox and email in the company, and delete rights |

---

## 4. Concepts

```
res.users ──1:N──> cold.mailbox ──1:N──> cold.mail ──┐
                                           ▲         │ parent_id
                                           └─────────┘ (replies)
```

| Model | Role |
|---|---|
| `cold.mailbox` | One connected Gmail account. Owns the OAuth tokens and the sending policy. |
| `cold.mail` | One outgoing email. **This is also the queue** — there is no separate queue model. Auto-numbered (`SQ1`, `SQ2`, …). |
| `cold.mail.template` | A name, subject and body to copy onto an email. |
| `cold.mail.tag` | A coloured label for grouping emails. |

There are no campaigns, sequences or contact lists: each email is one row in a
flat list.

---

## 5. Connecting a mailbox

**Cold Email → Configuration → Mailboxes → New.** Give it a name, save, then
**Connect Gmail**. A rep can only connect a mailbox that belongs to them; a
manager can connect anyone's.

```
Connect Gmail
      │  builds the auth URL under sudo(): scopes, PKCE challenge,
      │  state = {mailbox id + HMAC}
      ▼
accounts.google.com — user grants gmail.send + gmail.readonly
      │  ?code=…&state=…
      ▼
/cold_email/oauth/callback   verify state → consteq(HMAC) → 403 if forged
      ▼
POST oauth2.googleapis.com/token   (authorization_code + PKCE verifier)
      ▼
GET gmail/v1/users/me/profile      ← captures the address that ACTUALLY authenticated
      ▼
state = 'connected'
```

The `Gmail Address` field is filled in **by Google**, not by whatever was typed
in Odoo, and the same address cannot be connected twice in one company. Using
`users.getProfile` instead of Google's `oauth2/v2/userinfo` means no
`userinfo.email` scope is needed.

Buttons: **Send Test Email** (sends a short message to your own user's address),
**Reconnect**, and **Disconnect** (revokes the token at Google, then forgets it).

### Mailbox fields

| Field | Default | Meaning |
|---|---|---|
| `daily_limit` | 50 | Max emails per day from this mailbox |
| `min_delay_seconds` | 60 | Minimum pause between two sends |
| Send From / Send Until | 9.0 / 18.0 | Daily sending window, in `tz` |
| `tz` | user's tz | Timezone the window and the daily reset use |
| Detect Bounces | on | Poll the inbox so bounced messages get marked |
| `debug_logging` | off | Log every API call (credentials never logged) |

Keep `daily_limit` well under Gmail's own ceiling (~500/day consumer,
~2 000/day Workspace). Those limits are separate from API quota.

**Mailbox states:** `draft → connected → expired ⇄ connected → disconnected`,
plus `error` on a hard API refusal.

---

## 6. Templates, tags and placeholders

**Cold Email → Configuration → Email Templates** holds reusable content. Picking
a template on an email *copies* its subject and body across; nothing links back,
so editing a template never touches emails already scheduled.

**Tags** are free-form coloured labels for grouping emails. They have no effect
on sending.

Each email has a **Name** and a **Company Name** field. Type `/` in the Subject
or Body to insert them as dynamic placeholders, rendered at send time:

| Where | Syntax | Example |
|---|---|---|
| Subject | `{{ object.field }}` | `Quick question about {{ object.company_name }}` |
| Body | `<t t-out="object.field"/>` | `Hi <t t-out="object.name"/>,` |

Once sent, the subject and body are replaced with what actually went out.

---

## 7. Scheduling an email

**Cold Email → Emails → New.**

1. Pick **Send From** — only connected mailboxes are offered.
2. Fill in **To**, and optionally Name, Company Name and Tags.
3. Pick a **Template**, or write the Subject and Body directly.
4. Set **Schedule Date & Time** (defaults to now) and save.

**States:** `scheduled → sent | bounced | failed | cancelled`.

- **Cancel** stops a scheduled email (a sent one cannot be cancelled).
- **Retry** puts a failed or cancelled email back in the queue.
- Every email has chatter: log notes and schedule activities per recipient.
- Filters: *Scheduled*, *Sent*, *Failed*, *Bounced*, *Due Now*, *My Emails*.

If an address bounced before, on any mailbox, the form warns as soon as you type
it, and the email is cancelled at send time to protect sender reputation. The
check ignores letter case.

---

## 8. Replies and threading

An email is either a **New Thread** or a **Reply**. Open a sent email and press
**Reply** to create a follow-up with *Reply To*, the mailbox, the recipient and a
`Re:` subject already filled in.

```
NEW THREAD
  MIME    Subject: Quick question about Acme
  POST /messages/send   { raw }                 ← no threadId
      ▼  Gmail creates thread T1
      ▼  ⚠ Gmail REWRITES the Message-ID we generated
  GET /messages/{id}?format=metadata&metadataHeaders=Message-Id
      └─> gmail_thread_id = "T1"
          rfc_message_id  = "<abc@mail.gmail.com>"

REPLY   (parent_id = the sent email above)
  MIME    Subject: Re: Quick question about Acme     ← prefix added
          In-Reply-To: <abc@mail.gmail.com>
          References:  <abc@mail.gmail.com>
  POST /messages/send   { raw, threadId: "T1" }
      ▼  lands in the SAME conversation
```

Two things that bite people:

1. **Gmail overwrites `Message-ID` on send.** The ID needed for the next
   `In-Reply-To` can only be obtained by reading the message back with
   `messages.get`. That is why `gmail.readonly` is in the scope list.
2. **`threadId` alone does not thread.** Gmail attaches a message to a thread
   only when the `threadId`, the `References`/`In-Reply-To` headers *and* the
   subject line all agree. The module always sets the three together.

Rules enforced: a reply must point at an email that is already **sent**, from the
**same mailbox**. A reply whose parent has no thread or Message-ID yet falls back
to a new thread instead of mis-threading.

---

## 9. Sending and bounces

### `_cron_send_queued` — every 5 minutes

```
for each connected mailbox:
    ├─ daily quota spent ?          ──> skip mailbox
    └─ for each due email, oldest first (up to the remaining quota):
           ├─ outside the sending window ?      ──> leave it
           ├─ min_delay_seconds not elapsed ?   ──> stop, wait for the next beat
           ├─ mailbox disconnected / no recipient / empty subject or body
           │  / address bounced before          ──> cancelled, with the reason
           ├─ render subject and body, build MIME (text + HTML)
           ├─ POST /messages/send   (with threadId when replying)
           ├─ GET  /messages/{id}   → the real Message-ID
           └─ store ids, freeze the rendered text, count against the quota
```

At most 200 messages are sent per cron run across all mailboxes. The daily
counter rolls over at midnight in each mailbox's timezone, and each send is
committed on its own.

### `_cron_sync_bounces` — every 15 minutes

One API call covers a whole mailbox rather than polling each thread:

```
GET /messages?q=-from:me newer_than:7d&labelIds=INBOX
  → collect threadIds → match sent emails by gmail_thread_id in one query
  → sender is mailer-daemon@ / postmaster@ ?  state = bounced
```

The module detects **bounces only**, not human replies.

### `_cron_gc` — weekly

Deletes `sent`, `bounced` and `cancelled` emails older than 12 months.
Scheduled and failed emails are never collected.

---

## 10. Error handling

| Condition | Behaviour |
|---|---|
| `401` | Refresh the token and replay the request **once** |
| `429`, `5xx`, rate-limit / backend-error reasons | Transient: retry in `2^n` minutes (capped at 120), give up after 5 attempts |
| `domainPolicy`, `failedPrecondition`, `forbidden`, `insufficientPermissions` | **Mailbox** problem: stop this mailbox's batch, leave its queue untouched, set `state = error` |
| OAuth `invalid_grant` | Authorisation revoked or expired: `state = expired`, queue untouched |
| Other OAuth errors (e.g. `invalid_client`) | Bad Client ID/Secret: `state = error`, queue untouched |
| Any other 4xx | This message is bad: `state = failed` with the error text |

**A mailbox-level failure never fails the messages.** A settings typo would
otherwise burn the whole queue and hammer Google's token endpoint once per
email. Bad mailboxes are skipped; healthy mailboxes in the same run keep
sending. Each send is isolated in a `cr.savepoint()`.

The **Response** tab of an email shows the outcome, Gmail identifiers, the HTTP
status and reason of a failure, and Google's raw response.

---

## 11. Security

- OAuth credentials (`refresh_token`, `access_token`, `access_token_expiration`,
  `pkce_verifier`) live on `groups='base.group_system'` fields and appear in no
  view. A Cold Email User cannot read them; all sending goes through `sudo()`.
- The OAuth callback verifies an HMAC-signed `state`; a forged one gets a 403.
- PKCE (S256) is used on the authorisation-code exchange.
- Record rules: users see only their own mailboxes and emails; managers see the
  whole company.
- Users can create and edit mailboxes, emails, templates and tags; only managers
  can delete.

---

## 12. Compliance

`gmail.send` is a **sensitive** scope; `gmail.readonly` is a **restricted** one.

- **Internal Workspace app:** neither needs review.
- **Public / multi-customer product:** Google requires OAuth verification **and**
  a third-party CASA security assessment for the restricted scope. Budget for it
  early.

**No unsubscribe link is added.** The module adds no unsubscribe footer and no
`List-Unsubscribe` header; that mechanism was deliberately dropped. If your
outreach needs an opt-out, put it in the body yourself, and respect the laws that
apply to your recipients.

Keep the caps honest. Gmail's sending limits are separate from API quota, and
rotating mailboxes to exceed them violates Google's terms.

---

## 13. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| *"Gmail API credentials are missing"* | Client ID/Secret not set in Settings → Cold Email |
| Google shows `redirect_uri_mismatch` | The URI in the Cloud console must match the one on the Settings page **exactly**, including scheme and port |
| Mailbox flips to *Authorisation Expired* after a week | Consent screen is in *Testing*. Expected. Publish it, or use an *Internal* app, then Reconnect |
| Nothing sends, everything stays *Scheduled* | Outside the sending window, daily limit reached, `min_delay_seconds` not elapsed, or the mailbox is in `error`/`expired` — check `last_error` on the mailbox |
| Email is *Cancelled* with a reason | Mailbox disconnected, no recipient, empty subject or body, or the address bounced before |
| A reply arrives as a separate conversation | The parent had no thread or Message-ID (falls back to a new thread), or the Message-ID could not be read back — check the log |
| Bounces not detected | Detect Bounces is off, the notice is older than 7 days, or it never reached the INBOX label |

To send immediately while testing, run the crons by hand from
**Settings → Technical → Scheduled Actions**.

---

## 14. Tests

```bash
./odoo-bin -c <conf> -d <db> -i cold_email_gmail \
  --test-tags /cold_email_gmail --stop-after-init
```

40 tests. The Gmail transport is swapped for a fake in `tests/common.py`; the
fake still resolves a token through `_get_access_token`, so the OAuth path stays
in the loop.

| File | Covers |
|---|---|
| `test_mailbox.py` | Token reuse/refresh/expiry, CSRF state, PKCE, scopes, window validation |
| `test_sending.py` | Queue, limits, window, pacing, retries, mailbox-level failures, threading, bounces |
| `test_security.py` | Record rules, token invisibility |
| `test_controllers.py` | The OAuth callback over real HTTP |

---

## 15. Layout

```
cold_email_gmail/
├── tools/
│   ├── oauth.py         auth URL, PKCE, code exchange, refresh, revoke
│   ├── gmail_api.py     the REST client — every HTTPS call lives here
│   └── mime.py          RFC 5322 builder, base64url, threading headers
├── models/              cold_mailbox, cold_mail, cold_mail_template, cold_mail_tag
├── controllers/main.py  OAuth callback
├── data/                three crons, one sequence
├── security/            groups, ACLs, record rules
└── views/
```

Transport is isolated in `tools/` so the models stay free of request plumbing
and the tests can swap the whole client for a fake.

---

## 16. Not included

Campaigns and multi-step sequences, contact lists, open/click tracking, A/B
testing, reply detection, unsubscribe handling, Pub/Sub push notifications (the
15-minute poll is sufficient at this scale) and multi-company record rules
(`company_id` is already on the models, so the rules are a one-file addition).

### Known limitation

Between "Gmail accepted the message" and "we recorded it" there is a window of a
few ORM writes. If one failed, the savepoint would roll back and the next cron
run would re-send. The risky `messages.get` call is already guarded, so the
remaining window is very small — but delivery is **at-least-once**, not
exactly-once.
