# Cold Email (Gmail API)

Multi-step cold outreach for Odoo 19 Community that talks to Gmail **entirely
over its REST API**. No SMTP anywhere: no `ir.mail_server`, no `mail.mail`, no
`fetchmail.server`, no app password. Every operation is an HTTPS call to
`gmail.googleapis.com`.

- **Depends on:** `mail` only
- **Python:** `requests` (no `google-api-python-client`, no `google-auth`)
- **Scopes:** `gmail.send`, `gmail.readonly`
- **License:** LGPL-3

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
client. It still reuses generic core `mail` infrastructure — `mail.blacklist`,
the `mail.thread.blacklist` mixin, `mail.render.mixin` — because that has
nothing to do with the SMTP connector.

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
| **User** | Connect their own mailbox, manage their own campaigns |
| **Manager** | Everything above, plus see every mailbox and campaign in the company |

---

## 4. Concepts

```
res.users ──1:N──> cold.mailbox ──1:N──> cold.campaign
                        │                      │
                        │                      ├──1:N──> cold.sequence.step
                        │                      │
                        │                      └──1:N──> cold.enrollment <──N:1── cold.contact
                        │                                     │
                        └─────────────────────────────────────┴──1:N──> cold.mail
```

| Model | Role |
|---|---|
| `cold.mailbox` | One connected Gmail account. Owns the OAuth tokens and the sending policy. |
| `cold.contact` | A prospect on the cold list. Kept out of `res.partner` on purpose. |
| `cold.campaign` | A mailbox + an ordered sequence + the enrolled contacts. |
| `cold.sequence.step` | One message: its copy, its delay, and how it threads. |
| `cold.enrollment` | One contact's run through one campaign. **Holds the Gmail thread state.** |
| `cold.mail` | One outgoing email. **This is also the queue** — there is no separate queue model. |

`cold.enrollment` exists because the same contact can sit in several campaigns
at once, each with its own Gmail conversation.

---

## 5. Connecting a mailbox

**Cold Email → Configuration → Mailboxes → New.** Give it a name, save, then
**Connect Gmail**.

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
in Odoo. Using `users.getProfile` instead of Google's `oauth2/v2/userinfo` means
no `userinfo.email` scope is needed.

Use **Send Test Email** to prove the whole chain in one click.

### Mailbox fields

| Field | Default | Meaning |
|---|---|---|
| `daily_limit` | 50 | Max emails per day from this mailbox |
| `min_delay_seconds` | 60 | Minimum pause between two sends |
| `window_start` / `window_end` | 9.0 / 18.0 | Daily sending window, in `tz` |
| `tz` | user's tz | Timezone the window and daily reset use |
| `reply_sync_enabled` | on | Poll the inbox to detect replies |
| `debug_logging` | off | Log every API call (credentials never logged) |

Keep `daily_limit` well under Gmail's own ceiling (~500/day consumer,
~2 000/day Workspace). Those limits are separate from API quota.

**Mailbox states:** `draft → connected → expired ⇄ connected → disconnected`,
plus `error` on a hard API refusal.

---

## 6. Contacts

**Cold Email → Contacts.** Import with the standard Odoo CSV importer — nothing
custom.

`cold.contact` inherits `mail.thread.blacklist` with `_primary_email = 'email'`,
which brings `email_normalized`, a searchable `is_blacklisted`, the
`message_bounce` counter and chatter for free.

Merge fields available in step copy:

| Placeholder | Field |
|---|---|
| `{{ object.name }}` | Full name |
| `{{ object.email }}` | Email |
| `{{ object.company_name }}` | Company |
| `{{ object.job_title }}` | Job title |
| `{{ object.website }}` | Website |

Rendering uses `mail.render.mixin` with the `inline_template` engine. Use
`{{ object.name ||| there }}` to supply a fallback when a field is empty.

**Create Contact** promotes a warm prospect into a real `res.partner`. There is
deliberately no `crm.lead` link: a Many2one to it would make `crm` a hard
dependency of the whole module. A bridge module can add it.

---

## 7. Campaigns and sequences

**Cold Email → Campaigns → New.** Pick a connected mailbox, add steps, add
contacts, press **Start**.

```
Q3 SaaS Outreach            mailbox: sales@acme.com
├── step 1   +0d   new_thread   "Quick question about {{ object.company_name }}"
├── step 2   +3d   reply        "Following up"
├── step 3   +4d   reply        "Last note"
└── step 4   +7d   new_thread   "Different angle"
```

Delays are counted **from the previous step**, not from day zero, so reordering
or inserting a step does not silently shift everything after it.

**Campaign states:**

```
draft ──Start──> running ──Pause──> paused ──Resume──> running ──> done
  │                 │                                              ▲
  └─────────────────┴────────Cancel────────> cancelled      (all enrollments finished)
```

**Pause does not delete anything.** The cron skips campaigns that are not
running, so resuming costs nothing and re-enqueues nothing.

Adding contacts to an already-running campaign starts them immediately.
Blacklisted and already-enrolled contacts are skipped; the wizard says how many
and why before you commit.

---

## 8. Threading — new conversation vs. reply

This is the part with a trap in it.

```
STEP 1   send_mode = new_thread
  MIME    Subject: Quick question about Acme
          (no In-Reply-To, no References)
  POST /messages/send   { raw }                 ← no threadId
      ▼  Gmail creates thread T1
      ▼  ⚠ Gmail REWRITES the Message-ID we generated
  GET /messages/{id}?format=metadata&metadataHeaders=Message-Id
      └─> enrollment.gmail_thread_id     = "T1"
          enrollment.last_rfc_message_id = "<abc@mail.gmail.com>"

STEP 2   send_mode = reply
  MIME    Subject: Re: Quick question about Acme     ← prefix REQUIRED
          In-Reply-To: <abc@mail.gmail.com>
          References:  <abc@mail.gmail.com>
  POST /messages/send   { raw, threadId: "T1" }
      ▼  lands in the SAME conversation
```

Two things that bite people:

1. **Gmail overwrites `Message-ID` on send.** Whatever you put in the MIME is
   not what the recipient's client sees, so the ID needed for the next
   follow-up's `In-Reply-To` can only be obtained by reading the message back
   with `messages.get`. That single requirement is why `gmail.readonly` is in
   the scope list — it is not only for reply detection.
2. **`threadId` alone does not thread.** Gmail attaches a message to a thread
   only when the `threadId`, the `References`/`In-Reply-To` headers *and* the
   subject line all agree. Set one without the others and it silently starts a
   new conversation. The module always sets the three together.

A `reply` step with no thread to reply into (the first step, or one whose
predecessor never went out) automatically degrades to `new_thread` rather than
mis-threading. A mid-sequence `new_thread` step resets the chain and becomes the
new anchor for later `reply` steps.

---

## 9. Sending, replies and suppression

### `_cron_send_queued` — every 5 minutes

```
for each connected mailbox:
    ├─ outside its window ?    ──> skip mailbox
    ├─ daily quota spent ?     ──> skip mailbox
    └─ for each due cold.mail, oldest first:
           ├─ contact blacklisted / campaign not running ──> cancelled
           ├─ render copy, build MIME (+ unsubscribe link and header)
           ├─ POST /messages/send   (with threadId when replying)
           ├─ GET  /messages/{id}   → the real Message-ID
           ├─ store ids, update the enrollment's thread state
           └─ CREATE the next step's cold.mail   ← how sequences advance
```

Follow-ups are queued **lazily** — the next email is created only after the
current one actually sends. Nothing is pre-scheduled days ahead, so a pause or a
reply never leaves stale rows behind.

### `_cron_sync_replies` — every 15 minutes

One API call covers a whole mailbox rather than polling each thread:

```
GET /messages?q=-from:me newer_than:7d&labelIds=INBOX
  → collect threadIds → match cold.enrollment.gmail_thread_id in one query
  → sender is mailer-daemon@ / postmaster@ ?  bounced : replied
  → cancel that enrollment's remaining scheduled emails
```

Two hard bounces add the address to `mail.blacklist`.

### `_cron_gc` — weekly

Deletes `sent` / `cancelled` rows older than 12 months. Scheduled rows are never
collected.

### Unsubscribe

Every message carries a visible footer link **and** a `List-Unsubscribe` header
with `List-Unsubscribe-Post: List-Unsubscribe=One-Click` — both required by
Google's bulk-sender rules. The route is public and HMAC-signed:

```
/cold_email/unsubscribe/<contact_id>/<token>     →  403 on a forged token
  → mail.blacklist._add(email)      ← GLOBAL, applies to every future campaign
  → enrollments still running       → 'unsubscribed'
  → their pending cold.mail rows    → 'cancelled'
```

An enrollment that already reached `done` keeps that state — the unsubscribe
stops what is running, it does not rewrite the history of a finished sequence.
The address is suppressed either way.

---

## 10. Error handling

| Condition | Behaviour |
|---|---|
| `401` | Refresh the token and replay the request **once** |
| `429`, `403 rateLimitExceeded`, `5xx` | Transient: `retry_count++`, retry in `2^n` minutes (capped at 120), give up after 5 |
| `403 domainPolicy` / `failedPrecondition` | **Mailbox** problem: stop this mailbox's batch, leave its queue untouched, set `state = error` |
| OAuth `invalid_grant` | Authorisation revoked or expired: `state = expired`, queue untouched |
| OAuth `invalid_client` | Bad Client ID/Secret: `state = error`, queue untouched |
| Any other 4xx | This message is bad: `state = failed` with the error text |

**A mailbox-level failure never fails the messages.** A settings typo would
otherwise burn an entire campaign and hammer Google's token endpoint once per
queued email. Bad mailboxes are skipped; healthy mailboxes in the same cron run
keep sending.

Each send is isolated in a `cr.savepoint()`, so one bad row cannot roll back the
batch. Note that the savepoint's rollback calls `cr.clear()` — anything written
inside it is discarded, which is why mailbox diagnostics are written by the
caller, outside the savepoint.

---

## 11. Security

- OAuth credentials (`refresh_token`, `access_token`, `access_token_expiration`,
  `pkce_verifier`) live on `groups='base.group_system'` fields and appear in no
  view. A Cold Email User genuinely cannot read them; all sending goes through
  `sudo()`.
- The API client redacts the `Authorization` header before any debug logging,
  and tokens are never written to the log.
- The OAuth callback verifies an HMAC-signed `state`; a forged one gets a 403.
- PKCE (S256) is used on the authorization code exchange.
- Record rules: users see only their own mailboxes and campaigns (and the
  enrollments/emails belonging to them); managers see the whole company. The
  cold list itself is shared across the group — it is team property.
- `_get_unsubscribe_token` and `_get_access_token` are private and unreachable
  over RPC.

---

## 12. Compliance

`gmail.send` is a **sensitive** scope; `gmail.readonly` is a **restricted** one.

- **Internal Workspace app:** neither needs review.
- **Public / multi-customer product:** Google requires OAuth verification **and**
  a third-party CASA security assessment for the restricted scope. Budget for it
  early rather than treating it as a launch-day task.

If you later want the module to label threads inside Gmail, swap `gmail.readonly`
for `gmail.modify` — same restricted tier, so no additional review burden.

Keep the caps honest. Gmail's sending limits are separate from API quota, and
rotating mailboxes to exceed them violates Google's terms.

---

## 13. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| *"Gmail API credentials are missing"* | Client ID/Secret not set in Settings → Cold Email |
| Google shows `redirect_uri_mismatch` | The URI in the Cloud console must match the one on the Settings page **exactly**, including scheme and port |
| Mailbox flips to *Authorisation Expired* after a week | Consent screen is in *Testing*. Expected. Publish it, or use an *Internal* app |
| *"Google rejected the Client ID or Client Secret"* | Wrong credentials, or they belong to a different Cloud project |
| *"Google did not return a refresh token"* | Access was already granted. Remove the app at <https://myaccount.google.com/permissions> and reconnect |
| Nothing sends, everything stays *Scheduled* | Outside the sending window, daily limit reached, campaign not *Running*, or the mailbox is in `error`/`expired` — check `last_error` on the mailbox |
| Follow-ups arrive as separate conversations | The step's `send_mode` is `new_thread`, or the previous step's Message-ID could not be read back (check the log) |
| Replies not detected | `reply_sync_enabled` is off, the reply is older than 7 days, or it never reached the INBOX label |

To send immediately while testing, run the crons by hand from
**Settings → Technical → Scheduled Actions**.

---

## 14. Tests

```bash
./odoo-bin -c <conf> -d <db> -i cold_email_gmail \
  --test-tags /cold_email_gmail --stop-after-init
```

59 tests. The Gmail transport is swapped for a fake in `tests/common.py`; the
fake still resolves a token through `_get_access_token`, so the OAuth path stays
in the loop — without that, the suite silently skips token refresh and expiry
handling entirely.

| File | Covers |
|---|---|
| `test_mailbox.py` | Token reuse/refresh/expiry, CSRF state, PKCE, scopes |
| `test_sending.py` | Queue, limits, window, pacing, retries, mailbox-level failures |
| `test_threading.py` | Requirement D: new thread vs. reply, chain advance, Message-ID read-back |
| `test_replies.py` | Reply detection, bounces, blacklisting, GC |
| `test_unsubscribe.py` | Token signing, global suppression |
| `test_security.py` | Record rules, token invisibility |
| `test_controllers.py` | Public routes over real HTTP |

---

## 15. Layout

```
cold_email_gmail/
├── tools/
│   ├── oauth.py         auth URL, PKCE, code exchange, refresh, revoke
│   ├── gmail_api.py     the REST client — every HTTPS call lives here
│   └── mime.py          RFC 5322 builder, base64url, threading headers
├── models/              cold_mailbox, cold_contact, cold_campaign,
│                        cold_sequence, cold_enrollment, cold_mail
├── controllers/main.py  OAuth callback + public unsubscribe
├── wizard/              enroll wizard
├── data/                three crons
├── security/            groups, ACLs, record rules
└── views/
```

Transport is isolated in `tools/` so the models stay free of request plumbing
and the tests can swap the whole client for a fake.

---

## 16. Not included (v1)

Open/click tracking, A/B testing, Pub/Sub push notifications (the 15-minute poll
is sufficient at this scale), an analytics dashboard beyond the campaign
counters, multi-company record rules (`company_id` is already on the models, so
the rules are a one-file addition), and automatic lead creation on reply.

### Known limitation

Between "Gmail accepted the message" and "we recorded it" there is a window of a
few ORM writes. If one failed, the savepoint would roll back and the next cron
run would re-send. The risky `messages.get` call is already guarded, so the
remaining window is very small — but delivery is **at-least-once**, not
exactly-once. Closing it properly means committing mid-savepoint.
