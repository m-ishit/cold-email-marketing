import datetime
import json
import logging
import time

import pytz

from odoo import _, api, fields, models, tools
from odoo.exceptions import AccessError, UserError

from ..tools import mime, oauth
from ..tools.gmail_api import GmailApi, GmailApiError

_logger = logging.getLogger(__name__)


def _tz_get(self):
    return [(tz, tz) for tz in sorted(pytz.all_timezones, key=lambda tz: tz if not tz.startswith('Etc/') else '_')]


class ColdMailbox(models.Model):
    """A Gmail account connected over OAuth 2.0 and driven through the REST API.

    Every rep owns their own mailbox record; the OAuth credentials live on
    ``base.group_system`` fields so nobody but a system administrator can read
    them, and all sending goes through ``sudo()``.
    """

    _name = 'cold.mailbox'
    _description = 'Cold Email Mailbox'
    _order = 'name'

    name = fields.Char(required=True)
    email = fields.Char(
        string='Gmail Address', readonly=True, copy=False,
        help='Filled in from Google once the mailbox is connected.')
    google_user_id = fields.Char(readonly=True, copy=False)
    user_id = fields.Many2one(
        'res.users', string='Owner', required=True, default=lambda self: self.env.user,
        help='Only this user (and Cold Email managers) can send from this mailbox.')
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, required=True)
    active = fields.Boolean(default=True)

    state = fields.Selection([
        ('draft', 'Not Connected'),
        ('connected', 'Connected'),
        ('expired', 'Authorisation Expired'),
        ('error', 'Error'),
        ('disconnected', 'Disconnected'),
    ], default='draft', required=True, copy=False, readonly=True)
    last_error = fields.Char(readonly=True, copy=False)

    # --- sending policy -------------------------------------------------
    daily_limit = fields.Integer(
        default=50, required=True,
        help='Maximum number of emails this mailbox may send per day. Keep it well '
             'under Gmail\'s own limit (about 500/day for a personal account, '
             '2000/day for Workspace).')
    sent_today = fields.Integer(readonly=True, copy=False)
    last_send_date = fields.Date(readonly=True, copy=False)
    min_delay_seconds = fields.Integer(
        default=60, required=True,
        help='Minimum pause between two emails sent from this mailbox.')
    last_send_at = fields.Datetime(readonly=True, copy=False)
    window_start = fields.Float(
        string='Send From', default=9.0, required=True,
        help='Start of the daily sending window, in the mailbox timezone.')
    window_end = fields.Float(string='Send Until', default=18.0, required=True)
    tz = fields.Selection(
        _tz_get, string='Timezone', required=True,
        default=lambda self: self.env.user.tz or 'UTC')
    tz_offset = fields.Char(compute='_compute_tz_offset', string='Timezone Offset')

    # --- bounce detection -------------------------------------------------
    reply_sync_enabled = fields.Boolean(
        string='Detect Bounces', default=True,
        help='Poll the inbox so a bounced message gets marked as such.')
    last_reply_sync = fields.Datetime(string='Last Bounce Sync', readonly=True, copy=False)

    debug_logging = fields.Boolean(
        help='Log every Gmail API call. Credentials are never logged.')

    # --- OAuth credentials, never shown in a view ------------------------
    refresh_token = fields.Char(groups='base.group_system', copy=False)
    access_token = fields.Char(groups='base.group_system', copy=False)
    access_token_expiration = fields.Integer(groups='base.group_system', copy=False)
    pkce_verifier = fields.Char(groups='base.group_system', copy=False)

    is_credentials_set = fields.Boolean(compute='_compute_is_credentials_set')

    _unique_email_per_company = models.Constraint(
        'UNIQUE(email, company_id)',
        'This Gmail address is already connected in this company.',
    )

    def _compute_is_credentials_set(self):
        configured = oauth.is_configured(self.env)
        for mailbox in self:
            mailbox.is_credentials_set = configured

    @api.depends('tz')
    def _compute_tz_offset(self):
        for mailbox in self:
            mailbox.tz_offset = datetime.datetime.now(pytz.timezone(mailbox.tz or 'GMT')).strftime('%z')

    @api.constrains('window_start', 'window_end')
    def _check_window(self):
        for mailbox in self:
            if not 0 <= mailbox.window_start < 24 or not 0 < mailbox.window_end <= 24:
                raise UserError(_('The sending window must fall inside a single day.'))
            if mailbox.window_start >= mailbox.window_end:
                raise UserError(_('The sending window must start before it ends.'))

    @api.constrains('daily_limit', 'min_delay_seconds')
    def _check_limits(self):
        for mailbox in self:
            if mailbox.daily_limit < 1:
                raise UserError(_('The daily limit must be at least 1.'))
            if mailbox.min_delay_seconds < 0:
                raise UserError(_('The delay between sends cannot be negative.'))

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def _check_can_manage(self):
        """A rep may connect their own mailbox; a manager may connect anyone's."""
        self.ensure_one()
        if self.env.user.has_group('cold_email_gmail.group_cold_email_manager'):
            return
        if not self.env.user.has_group('cold_email_gmail.group_cold_email_user'):
            raise AccessError(_('You are not allowed to manage cold email mailboxes.'))
        if self.user_id != self.env.user:
            raise AccessError(_('You can only connect a mailbox that belongs to you.'))

    def action_connect(self):
        """Send the browser to Google's consent screen."""
        self.ensure_one()
        self._check_can_manage()

        if not oauth.is_configured(self.env):
            raise UserError(_(
                'Gmail API credentials are missing. Set the Client ID and Client '
                'Secret in Settings > Cold Email first.'
            ))

        # sudo: pkce_verifier is a group_system field, but the rep who owns the
        # mailbox is the one clicking the button.
        mailbox = self.sudo()
        verifier = oauth.new_pkce_verifier()
        mailbox.pkce_verifier = verifier

        state = {
            'id': self.id,
            'csrf': self._get_csrf_token(),
        }

        return {
            'type': 'ir.actions.act_url',
            'url': oauth.authorization_url(self.env, state, verifier),
            'target': 'self',
        }

    def action_disconnect(self):
        """Revoke at Google, then forget everything locally."""
        self.ensure_one()
        self._check_can_manage()

        mailbox = self.sudo()
        if mailbox.refresh_token:
            oauth.revoke(self.env, mailbox.refresh_token)

        mailbox.write({
            'refresh_token': False,
            'access_token': False,
            'access_token_expiration': False,
            'pkce_verifier': False,
            'state': 'disconnected',
            'last_error': False,
        })
        return True

    def _get_csrf_token(self):
        """Signed token proving the OAuth callback belongs to this record."""
        self.ensure_one()
        return tools.misc.hmac(
            env=self.env(su=True),
            scope=oauth.CSRF_SCOPE,
            message=(self._name, self.id),
        )

    def _store_tokens(self, refresh_token, access_token, expires_in):
        """Called by the OAuth callback once Google has handed over the tokens."""
        mailbox = self.sudo()
        mailbox.write({
            'refresh_token': refresh_token,
            'access_token': access_token,
            'access_token_expiration': int(time.time()) + expires_in,
            'pkce_verifier': False,
            'state': 'connected',
            'last_error': False,
        })

        # Record the address that actually authenticated, which need not be the
        # one whoever created the record had in mind.
        try:
            profile = GmailApi(mailbox).get_profile()
        except GmailApiError as error:
            mailbox.write({'state': 'error', 'last_error': error.message})
            raise

        mailbox.write({
            'email': profile.get('emailAddress'),
            'google_user_id': profile.get('historyId'),
        })
        return mailbox.email

    def _get_access_token(self):
        """Return a usable bearer token, refreshing it when it is about to expire."""
        self.ensure_one()
        mailbox = self.sudo()

        if not mailbox.refresh_token:
            raise UserError(_('Mailbox "%s" is not connected to Gmail.', self.name))

        now = int(time.time())
        expiration = mailbox.access_token_expiration or 0
        if mailbox.access_token and expiration - oauth.EXPIRY_MARGIN > now:
            return mailbox.access_token

        try:
            access_token, expires_in = oauth.refresh_access_token(self.env, mailbox.refresh_token)
        except oauth.GoogleOAuthError as error:
            if error.reason == 'invalid_grant':
                mailbox.write({'state': 'expired', 'last_error': str(error)})
            else:
                mailbox.write({'state': 'error', 'last_error': str(error)})
            raise

        mailbox.write({
            'access_token': access_token,
            'access_token_expiration': now + expires_in,
        })
        if mailbox.state in ('expired', 'error'):
            mailbox.write({'state': 'connected', 'last_error': False})

        _logger.info('Cold Email: refreshed access token for %s (valid %i min)',
                     mailbox.name, expires_in // 60)
        return access_token

    def _invalidate_access_token(self):
        """Force the next call to fetch a fresh token (used after a 401)."""
        self.sudo().write({'access_token': False, 'access_token_expiration': 0})

    # ------------------------------------------------------------------
    # sending policy
    # ------------------------------------------------------------------

    def _reset_daily_counter_if_needed(self):
        """Roll ``sent_today`` over at midnight in the mailbox timezone."""
        self.ensure_one()
        today = fields.Date.context_today(self.with_context(tz=self.tz))
        if self.last_send_date != today:
            self.sudo().write({'sent_today': 0, 'last_send_date': today})

    def _remaining_quota(self):
        self.ensure_one()
        self._reset_daily_counter_if_needed()
        return max(0, self.daily_limit - self.sent_today)

    def _is_within_window(self):
        """Is the current time inside this mailbox's sending window?"""
        self.ensure_one()
        local_now = fields.Datetime.context_timestamp(
            self.with_context(tz=self.tz), fields.Datetime.now())
        hour = local_now.hour + local_now.minute / 60.0
        return self.window_start <= hour < self.window_end

    def _seconds_until_next_send(self):
        """How long the mailbox must still wait to respect ``min_delay_seconds``."""
        self.ensure_one()
        if not self.last_send_at or not self.min_delay_seconds:
            return 0
        elapsed = (fields.Datetime.now() - self.last_send_at).total_seconds()
        return max(0, self.min_delay_seconds - elapsed)

    def _register_send(self):
        """Book one send against the daily quota and the pacing clock."""
        self.ensure_one()
        self._reset_daily_counter_if_needed()
        self.sudo().write({
            'sent_today': self.sent_today + 1,
            'last_send_at': fields.Datetime.now(),
        })

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def action_send_test_email(self):
        """Prove the whole chain — token, MIME, Gmail API — in one click."""
        self.ensure_one()
        self._check_can_manage()

        if self.state != 'connected':
            raise UserError(_('Connect the mailbox to Gmail first.'))

        recipient = self.env.user.email or self.email
        if not recipient:
            raise UserError(_('Set an email address on your user to receive the test.'))

        message = mime.build(
            from_name=self.name,
            from_email=self.email,
            to_email=recipient,
            subject=_('Cold Email test message'),
            body_html=_(
                '<p>This test was sent from Odoo through the Gmail API '
                '(no SMTP involved).</p><p>Mailbox: %s</p>', self.email),
        )

        try:
            result = GmailApi(self).send(mime.encode(message))
        except GmailApiError as error:
            self.sudo().write({'last_error': error.message})
            raise UserError(_('Gmail refused the test message: %s', error.message)) from error

        self.sudo().write({'last_error': False})
        _logger.info('Cold Email: test message %s sent from %s', result.get('id'), self.email)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _('Test message sent'),
                'message': _('Check the inbox of %s.', recipient),
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

    def action_open_google_console(self):
        return {
            'type': 'ir.actions.act_url',
            'url': 'https://console.cloud.google.com/apis/credentials',
            'target': 'new',
        }

    @api.model
    def _get_state_from_callback(self, raw_state):
        """Resolve and authenticate the ``state`` blob coming back from Google.

        :return: the mailbox record, as sudo
        :raises ValueError: on anything malformed or unsigned
        """
        try:
            state = json.loads(raw_state)
            mailbox_id = int(state['id'])
            csrf = state['csrf']
        except (TypeError, ValueError, KeyError) as error:
            raise ValueError('malformed OAuth state') from error

        mailbox = self.browse(mailbox_id).exists().sudo()
        if not mailbox:
            raise ValueError('unknown mailbox in OAuth state')

        if not csrf or not tools.consteq(csrf, mailbox._get_csrf_token()):
            raise ValueError('bad CSRF token in OAuth state')

        return mailbox
