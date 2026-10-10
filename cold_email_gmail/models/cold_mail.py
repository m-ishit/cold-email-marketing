import json
import logging
from datetime import timedelta

from odoo import _, api, fields, models, tools
from odoo.exceptions import UserError

from ..tools import mime
from ..tools.gmail_api import GmailApi, GmailApiError
from ..tools.oauth import GoogleOAuthError

_logger = logging.getLogger(__name__)

# Outcomes of one send attempt, as seen by the queue loop.
SEND_OK = 'sent'
SEND_SKIPPED = 'skipped'
SEND_ABORT = 'abort'

# How many times a transient failure is retried before the row is given up on.
MAX_RETRIES = 5

# Cap on the exponential backoff, so a long outage does not push a message
# weeks into the future.
MAX_BACKOFF_MINUTES = 120

# Senders whose reply means "this address is dead", not "someone answered".
BOUNCE_SENDERS = ('mailer-daemon@', 'postmaster@')


class ColdMail(models.Model):
    """One scheduled outgoing email.

    This model *is* the sending queue: a row in state ``scheduled`` with a
    ``scheduled_at`` in the future is a queued job, so there is no separate
    queue model to keep in sync.
    """

    _name = 'cold.mail'
    _description = 'Cold Email Message'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'mail.render.mixin']
    _order = 'scheduled_at, id'
    _rec_name = 'reference'

    reference = fields.Char(
        readonly=True, copy=False, default=lambda self: _('New'),
        help='Auto-numbered reference shown in the breadcrumb and lists.')

    mailbox_id = fields.Many2one(
        'cold.mailbox', string='Send From', required=True, index=True,
        domain="[('state', '=', 'connected')]", tracking=True)
    email_from = fields.Char(related='mailbox_id.email', store=True, string='Email From')

    email_to = fields.Char(string='To', required=True, tracking=True)
    name = fields.Char(string='Name', help="Recipient's name. Insert it into the "
                        "Subject/Body with the dynamic placeholder (type /).")
    company_name = fields.Char(string='Company Name', help="Recipient's company name. "
                                'Insert it into the Subject/Body with the dynamic placeholder (type /).')
    tag_ids = fields.Many2many('cold.mail.tag', string='Tags')

    template_id = fields.Many2one(
        'cold.mail.template', string='Template',
        help='Pick a saved template to prefill Subject and Body. You can still edit them after.')
    subject = fields.Char(render_engine='inline_template')
    body_html = fields.Html(sanitize=False, render_engine='qweb')

    send_mode = fields.Selection([
        ('new_thread', 'New Thread'),
        ('reply', 'Reply'),
    ], default='new_thread', required=True, tracking=True,
        help='"Reply" appends this email to the Gmail conversation of the email picked '
             'in "Reply To", instead of starting a new one.')
    parent_id = fields.Many2one(
        'cold.mail', string='Reply To', ondelete='restrict', copy=False, tracking=True,
        domain="[('mailbox_id', '=', mailbox_id), ('state', '=', 'sent')]",
        help='The email this one replies to, inside the same Gmail conversation. '
             'Must already be sent, from the same mailbox.')
    child_ids = fields.One2many('cold.mail', 'parent_id', string='Replies')

    scheduled_at = fields.Datetime(
        string='Schedule Date & Time', required=True, index=True, default=fields.Datetime.now)
    sent_at = fields.Datetime(readonly=True, copy=False)

    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    user_id = fields.Many2one(
        'res.users', string='Responsible',
        default=lambda self: self.env.user, required=True, tracking=True)

    # --- Gmail identifiers, filled in once sent -------------------------
    gmail_message_id = fields.Char(readonly=True, copy=False)
    gmail_thread_id = fields.Char(readonly=True, copy=False, index=True)
    rfc_message_id = fields.Char(
        string='Message-ID', readonly=True, copy=False,
        help='The RFC Message-ID Gmail actually assigned, read back after sending.')

    state = fields.Selection([
        ('scheduled', 'Scheduled'),
        ('sent', 'Sent'),
        ('bounced', 'Bounced'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ], default='scheduled', required=True, index=True, copy=False, tracking=True)
    error_message = fields.Char(readonly=True, copy=False)
    error_status = fields.Integer(
        string='HTTP Status', readonly=True, copy=False,
        help='HTTP status code of the last failed Gmail API call (e.g. 429, 403).')
    error_reason = fields.Char(
        readonly=True, copy=False,
        help="Gmail's machine-readable error reason (e.g. rateLimitExceeded, domainPolicy).")
    retry_count = fields.Integer(default=0, readonly=True, copy=False)

    # --- last raw Gmail API payload, whatever it was ---------------------
    gmail_raw_response = fields.Text(
        string='Raw Response', readonly=True, copy=False,
        help='The last JSON payload Gmail actually returned for this email -- '
             'the send confirmation on success, or the error body on failure.')

    def _compute_render_model(self):
        """mail.render.mixin renders {{object.x}} placeholders against the
        email itself -- there is no separate contact record to point at."""
        self.render_model = 'cold.mail'

    @api.constrains('send_mode', 'parent_id')
    def _check_reply_has_parent(self):
        for mail in self:
            if mail.send_mode == 'reply' and not mail.parent_id:
                raise UserError(_('Pick which email this one replies to, or switch back to "New Thread".'))
            if mail.parent_id and mail.parent_id.mailbox_id != mail.mailbox_id:
                raise UserError(_(
                    'Cannot reply through mailbox "%(mailbox)s": "%(parent)s" was sent from a '
                    'different mailbox, and Gmail can only continue a conversation from the '
                    'account that has it.', mailbox=mail.mailbox_id.name, parent=mail.parent_id.reference))

    @api.onchange('parent_id')
    def _onchange_parent_id(self):
        for mail in self:
            if mail.parent_id:
                mail.send_mode = 'reply'
                mail.mailbox_id = mail.parent_id.mailbox_id
                mail.email_to = mail.parent_id.email_to

    @api.onchange('send_mode')
    def _onchange_send_mode(self):
        for mail in self:
            if mail.send_mode == 'new_thread':
                mail.parent_id = False

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('reference', _('New')) == _('New'):
                vals['reference'] = self.env['ir.sequence'].next_by_code('cold.mail') or _('New')
            # The template->subject/body copy normally happens via onchange,
            # which does not run on import or other direct create() calls.
            template_id = vals.get('template_id')
            if template_id:
                template = self.env['cold.mail.template'].browse(template_id)
                vals.setdefault('subject', template.subject)
                vals.setdefault('body_html', template.body_html)
        mails = super().create(vals_list)
        for mail in mails:
            mail.message_post(body=_('Email created.'))
        return mails

    @api.onchange('template_id')
    def _onchange_template_id(self):
        for mail in self:
            if mail.template_id:
                mail.subject = mail.template_id.subject
                mail.body_html = mail.template_id.body_html

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------

    def _should_cancel(self):
        """Reasons never to send this row, checked at the last moment.

        :return: a reason string, or None when the message is good to go.
        """
        self.ensure_one()
        if self.mailbox_id.state != 'connected':
            return _('Mailbox is no longer connected to Gmail.')
        if not self.email_to:
            return _('No recipient address set.')
        if not self.subject or not self.body_html:
            return _('Subject and Body must be set before sending.')
        bounced = self._find_prior_bounce()
        if bounced:
            return _(
                '"%(address)s" bounced before (see %(reference)s) -- skipped to protect '
                'sender reputation.', address=self.email_to, reference=bounced.reference)
        return None

    def _find_prior_bounce(self):
        """Any other email that bounced for this same address, on any mailbox.

        A dead address stays dead regardless of who sends to it, so the check
        is deliberately not scoped to this record's mailbox.

        :return: a ``cold.mail`` recordset (empty if there is no prior bounce)
        """
        self.ensure_one()
        domain = [('email_to', '=ilike', self.email_to), ('state', '=', 'bounced')]
        if isinstance(self.id, int):
            domain.append(('id', '!=', self.id))
        return self.search(domain, limit=1)

    @api.onchange('email_to')
    def _onchange_email_to_bounce_warning(self):
        """Warn as early as possible, rather than let it silently self-cancel later."""
        for mail in self:
            if mail.email_to and mail._find_prior_bounce():
                return {'warning': {
                    'title': _('This address bounced before'),
                    'message': _(
                        '"%s" bounced on a previous email. Sending to it again may hurt '
                        "your sender reputation -- it will be auto-cancelled at send time.",
                        mail.email_to),
                }}

    def _render_subject_and_body(self):
        """Substitute dynamic placeholders (``{{object.x}}`` in Subject,
        ``<t t-out="object.x"/>`` in Body) against this record.

        :return: ``(subject, body_html)``
        """
        self.ensure_one()
        subject = self._render_field('subject', self.ids)[self.id]
        body = self._render_field('body_html', self.ids)[self.id]
        return subject, body

    def _effective_send_mode(self):
        """The mode actually used at send time.

        Falls back to ``new_thread`` when there is nothing to reply into --
        the parent was cleared, or never made it out with a real thread/
        Message-ID (e.g. it is still scheduled, or Gmail never confirmed one).
        """
        self.ensure_one()
        if self.send_mode != 'reply':
            return 'new_thread'
        if not self.parent_id or not self.parent_id.gmail_thread_id or not self.parent_id.rfc_message_id:
            return 'new_thread'
        return 'reply'

    def _prepare_message(self, subject, body_html, in_reply_to=None):
        """Build the MIME message from already-rendered subject/body.

        :return: an ``EmailMessage``
        """
        self.ensure_one()
        return mime.build(
            from_name=self.mailbox_id.name,
            from_email=self.mailbox_id.email,
            to_email=self.email_to,
            subject=subject,
            body_html=body_html or '',
            in_reply_to=in_reply_to,
            references=in_reply_to,
        )

    def _send_one(self):
        """Send this message through the Gmail API.

        Raises ``GmailApiError`` so the caller can decide between retrying and
        giving up; every other failure marks the row failed.
        """
        self.ensure_one()

        reason = self._should_cancel()
        if reason:
            self.write({'state': 'cancelled', 'error_message': reason})
            return False

        effective_mode = self._effective_send_mode()
        subject, body = self._render_subject_and_body()

        in_reply_to = self.parent_id.rfc_message_id if effective_mode == 'reply' else None
        thread_id = self.parent_id.gmail_thread_id if effective_mode == 'reply' else None
        if effective_mode == 'reply':
            subject = mime.reply_subject(subject)

        message = self._prepare_message(subject, body, in_reply_to=in_reply_to)

        api = GmailApi(self.mailbox_id)
        result = api.send(mime.encode(message), thread_id=thread_id)

        gmail_message_id = result.get('id')
        gmail_thread_id = result.get('threadId')

        rfc_message_id = None
        try:
            headers = api.get_headers(gmail_message_id, ['Message-Id'])
            rfc_message_id = headers.get('message-id')
        except GmailApiError as error:
            # Not fatal: the message did go out.
            _logger.warning(
                'Cold Email: sent %s but could not read its Message-ID back: %s',
                gmail_message_id, error.message)

        self.write({
            'state': 'sent',
            'sent_at': fields.Datetime.now(),
            # Freeze the rendered content: once sent, the record should show
            # what actually went out, not the raw {{object.x}} template.
            'subject': subject,
            'body_html': body,
            'gmail_message_id': gmail_message_id,
            'gmail_thread_id': gmail_thread_id,
            'rfc_message_id': rfc_message_id,
            'gmail_raw_response': json.dumps(result, indent=2, ensure_ascii=False),
            'error_message': False,
            'error_status': False,
            'error_reason': False,
        })

        self.mailbox_id._register_send()

        _logger.info('Cold Email: sent to %s (thread %s)', self.email_to, gmail_thread_id)
        return True

    @staticmethod
    def _error_values(error):
        """Everything the Response tab shows about a failed send attempt."""
        values = {'error_message': str(error), 'error_status': False, 'error_reason': False}
        if isinstance(error, GmailApiError):
            values['error_status'] = error.status
            values['error_reason'] = error.reason
            if error.raw is not None:
                values['gmail_raw_response'] = json.dumps(error.raw, indent=2, ensure_ascii=False)
        return values

    def _handle_send_error(self, error):
        """Decide between retrying later and giving up on this row."""
        self.ensure_one()

        if isinstance(error, GmailApiError) and error.transient and self.retry_count < MAX_RETRIES:
            retry_count = self.retry_count + 1
            delay = min(2 ** retry_count, MAX_BACKOFF_MINUTES)
            self.write({
                **self._error_values(error),
                'state': 'scheduled',
                'retry_count': retry_count,
                'scheduled_at': fields.Datetime.now() + timedelta(minutes=delay),
            })
            _logger.info('Cold Email: retry %s/%s for mail %s in %s min',
                         retry_count, MAX_RETRIES, self.id, delay)
            return

        self.write({**self._error_values(error), 'state': 'failed'})
        _logger.warning('Cold Email: giving up on mail %s: %s', self.id, error)

    def action_retry(self):
        """Put a failed message back in the queue."""
        for mail in self:
            if mail.state not in ('failed', 'cancelled'):
                raise UserError(_('Only a failed or cancelled email can be retried.'))
            if mail.mailbox_id.state != 'connected':
                raise UserError(_('Cannot retry: the mailbox is no longer connected to Gmail.'))
            mail.write({
                'state': 'scheduled',
                'retry_count': 0,
                'scheduled_at': fields.Datetime.now(),
                'error_message': False,
            })
        return True

    def action_cancel(self):
        for mail in self:
            if mail.state == 'sent':
                raise UserError(_('A sent email cannot be cancelled.'))
            mail.write({'state': 'cancelled'})
        return True

    def action_reply(self):
        """Open a new email pre-filled to continue this one's Gmail thread."""
        self.ensure_one()
        if self.state != 'sent':
            raise UserError(_('Only a sent email can be replied to.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Reply'),
            'res_model': 'cold.mail',
            'view_mode': 'form',
            'context': {
                'default_parent_id': self.id,
                'default_send_mode': 'reply',
                'default_mailbox_id': self.mailbox_id.id,
                'default_email_to': self.email_to,
                'default_subject': mime.reply_subject(self.subject),
            },
        }

    # ------------------------------------------------------------------
    # crons
    # ------------------------------------------------------------------

    @api.model
    def _cron_send_queued(self, limit=200):
        """Send everything that is due, mailbox by mailbox.

        The per-mailbox loop is what enforces the sending policy: a mailbox
        outside its window, or out of daily quota, is skipped whole rather than
        checked per message.
        """
        # Settle anything the caller left dirty before the first savepoint.
        self.env.flush_all()

        now = fields.Datetime.now()
        mailboxes = self.env['cold.mailbox'].search([('state', '=', 'connected')])
        total_sent = 0

        for mailbox in mailboxes:
            if total_sent >= limit:
                break

            quota = mailbox._remaining_quota()
            if not quota:
                continue

            due = self.search([
                ('mailbox_id', '=', mailbox.id),
                ('state', '=', 'scheduled'),
                ('scheduled_at', '<=', now),
            ], order='scheduled_at, id', limit=min(quota, limit - total_sent))

            if not due:
                continue

            for mail in due:
                if not mailbox._is_within_window():
                    continue
                if mailbox._seconds_until_next_send() > 0:
                    # Pacing: the rest of this mailbox's queue waits for the
                    # next cron beat rather than blocking the worker.
                    break

                outcome, mailbox_state, mailbox_error = self._send_with_isolation(mail)

                if outcome == SEND_OK:
                    total_sent += 1
                elif outcome == SEND_ABORT:
                    mailbox.sudo().write({'state': mailbox_state, 'last_error': mailbox_error})
                    _logger.warning(
                        'Cold Email: mailbox %s unusable (%s), leaving its queue untouched: %s',
                        mailbox.name, mailbox_state, mailbox_error)
                    break

        if total_sent:
            _logger.info('Cold Email: %s message(s) sent', total_sent)
        return total_sent

    def _send_with_isolation(self, mail):
        """Send one message so that its failure cannot roll back the batch.

        :return: ``(outcome, mailbox_state, mailbox_error)``. Outcome is
            ``SEND_OK``, ``SEND_SKIPPED`` (this message is done with, the queue
            continues) or ``SEND_ABORT`` (stop touching this mailbox).
        """
        try:
            with self.env.cr.savepoint():
                sent = mail._send_one()
        except GoogleOAuthError as error:
            state = 'expired' if error.reason == 'invalid_grant' else 'error'
            return SEND_ABORT, state, str(error)
        except GmailApiError as error:
            if error.fatal_mailbox:
                return SEND_ABORT, 'error', error.message
            mail._handle_send_error(error)
            return SEND_SKIPPED, None, None
        except Exception as error:  # noqa: BLE001 - one bad row must not stop the queue
            _logger.exception('Cold Email: unexpected failure sending mail %s', mail.id)
            mail._handle_send_error(error)
            return SEND_SKIPPED, None, None

        if not tools.config['test_enable']:
            self.env.cr.commit()
        return (SEND_OK if sent else SEND_SKIPPED), None, None

    @api.model
    def _cron_sync_bounces(self):
        """Detect bounces and mark the messages they belong to.

        One ``messages.list`` call per mailbox covers every conversation, which
        is far cheaper than polling each stored thread individually.
        """
        mailboxes = self.env['cold.mailbox'].search([
            ('state', '=', 'connected'),
            ('reply_sync_enabled', '=', True),
        ])

        for mailbox in mailboxes:
            try:
                self._sync_bounces_for_mailbox(mailbox)
            except GmailApiError as error:
                _logger.warning('Cold Email: bounce sync failed for %s: %s',
                                mailbox.name, error.message)
                mailbox.sudo().write({'last_error': error.message})

    @api.model
    def _sync_bounces_for_mailbox(self, mailbox):
        api = GmailApi(mailbox)

        # -from:me keeps our own sent copies out; INBOX keeps drafts and spam out.
        messages = api.list_messages('-from:me newer_than:7d', label_ids=['INBOX'])
        if not messages:
            mailbox.sudo().write({'last_reply_sync': fields.Datetime.now()})
            return

        by_thread = {}
        for message in messages:
            by_thread.setdefault(message['threadId'], message['id'])

        mails = self.search([
            ('mailbox_id', '=', mailbox.id),
            ('gmail_thread_id', 'in', list(by_thread)),
            ('state', '=', 'sent'),
        ])

        for mail in mails:
            message_id = by_thread.get(mail.gmail_thread_id)
            if not message_id:
                continue

            sender = ''
            try:
                sender = (api.get_message_summary(message_id).get('from') or '').lower()
            except GmailApiError as error:
                _logger.info('Cold Email: could not inspect message %s: %s', message_id, error.message)

            if any(daemon in sender for daemon in BOUNCE_SENDERS):
                mail.write({'state': 'bounced', 'error_message': _('Message bounced.')})
                _logger.info('Cold Email: %s bounced', mail.email_to)

        mailbox.sudo().write({'last_reply_sync': fields.Datetime.now()})

    @api.model
    def _cron_gc(self, months=12):
        """Drop finished rows that nobody will look at again."""
        cutoff = fields.Datetime.now() - timedelta(days=30 * months)
        stale = self.search([
            ('state', 'in', ('sent', 'bounced', 'cancelled')),
            ('create_date', '<', cutoff),
        ])
        count = len(stale)
        stale.unlink()
        if count:
            _logger.info('Cold Email: removed %s old message(s)', count)
        return count
