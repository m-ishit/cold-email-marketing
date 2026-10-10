import logging

from werkzeug.exceptions import Forbidden

from odoo import _, http
from odoo.http import request

from ..tools import oauth

_logger = logging.getLogger(__name__)


class ColdEmailOAuth(http.Controller):
    """Receives Google's redirect after the user grants (or denies) access."""

    @http.route(oauth.CALLBACK_PATH, type='http', auth='user', website=False)
    def oauth_callback(self, code=None, state=None, error=None, **kwargs):
        if error:
            _logger.info('Cold Email: user declined the Google consent screen (%s)', error)
            return request.render('cold_email_gmail.oauth_error', {
                'error': _('Authorisation was cancelled or refused by Google.'),
                'redirect_url': '/odoo/cold-mailboxes',
            })

        try:
            mailbox = request.env['cold.mailbox']._get_state_from_callback(state)
        except ValueError as exception:
            # A bad state means the request did not originate from our own
            # "Connect Gmail" button; refuse it outright.
            _logger.warning('Cold Email: rejected OAuth callback (%s)', exception)
            raise Forbidden() from exception

        verifier = mailbox.pkce_verifier
        if not verifier:
            return request.render('cold_email_gmail.oauth_error', {
                'error': _('This authorisation link has already been used. Start again from the mailbox.'),
                'redirect_url': '/odoo/cold.mailbox/%s' % mailbox.id,
            })

        try:
            refresh_token, access_token, expires_in = oauth.exchange_code(request.env, code, verifier)
            address = mailbox._store_tokens(refresh_token, access_token, expires_in)
        except Exception as exception:  # noqa: BLE001 - surfaced to the user below
            _logger.warning('Cold Email: token exchange failed for mailbox %s', mailbox.id)
            mailbox.write({'state': 'error', 'last_error': str(exception), 'pkce_verifier': False})
            return request.render('cold_email_gmail.oauth_error', {
                'error': str(exception),
                'redirect_url': '/odoo/cold.mailbox/%s' % mailbox.id,
            })

        _logger.info('Cold Email: mailbox %s connected as %s', mailbox.id, address)
        return request.redirect('/odoo/cold.mailbox/%s' % mailbox.id)
