"""Google OAuth 2.0 for the Gmail API.

Everything that talks to accounts.google.com / oauth2.googleapis.com lives here,
so the models stay free of request plumbing and the tests can stub it out.

Deliberately independent of the ``google_gmail`` addon: that one mints a token
only to feed SMTP an XOAUTH2 password, and it hardcodes its own redirect URI.
"""
import base64
import hashlib
import json
import logging
import secrets

import requests
from werkzeug.urls import url_encode

from odoo import _
from odoo.exceptions import UserError
from odoo.tools.urls import urljoin as url_join

_logger = logging.getLogger(__name__)

AUTH_ENDPOINT = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_ENDPOINT = 'https://oauth2.googleapis.com/token'
REVOKE_ENDPOINT = 'https://oauth2.googleapis.com/revoke'

CALLBACK_PATH = '/cold_email/oauth/callback'

# gmail.send  -> users.messages.send
# gmail.readonly -> users.getProfile, users.messages.get (reading back the real
#   Message-ID that Gmail assigns on send) and users.messages.list (reply sync)
SCOPES = (
    'https://www.googleapis.com/auth/gmail.send '
    'https://www.googleapis.com/auth/gmail.readonly'
)

TIMEOUT = (5, 10)

# Seconds shaved off the expiry so a token can never go stale mid-request.
EXPIRY_MARGIN = 15

# HMAC scope string. Changing it invalidates every outstanding OAuth attempt.
CSRF_SCOPE = 'cold_email_oauth'


class GoogleOAuthError(UserError):
    """Google refused an OAuth request.

    ``reason`` is Google's machine-readable code (``invalid_grant``,
    ``invalid_client``, ...), used to decide whether reconnecting can help.
    """

    def __init__(self, message, reason=None):
        super().__init__(message)
        self.reason = reason


def get_credentials(env):
    """Return ``(client_id, client_secret)`` from our own config params."""
    config = env['ir.config_parameter'].sudo()
    return (
        config.get_param('cold_email.gmail_client_id'),
        config.get_param('cold_email.gmail_client_secret'),
    )


def is_configured(env):
    return all(get_credentials(env))


def redirect_uri(env):
    base_url = env['ir.config_parameter'].sudo().get_param('web.base.url')
    return url_join(base_url, CALLBACK_PATH)


def new_pkce_verifier():
    """A fresh PKCE code verifier (RFC 7636 §4.1: 43-128 unreserved chars)."""
    return secrets.token_urlsafe(64)


def pkce_challenge(verifier):
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip('=')


def authorization_url(env, state, verifier):
    """Build the URL the user's browser is sent to in order to grant access."""
    client_id, client_secret = get_credentials(env)
    if not client_id or not client_secret:
        raise UserError(_(
            'Gmail API credentials are missing. Set the Client ID and Client '
            'Secret in Settings > Cold Email.'
        ))

    return '%s?%s' % (AUTH_ENDPOINT, url_encode({
        'client_id': client_id,
        'redirect_uri': redirect_uri(env),
        'response_type': 'code',
        'scope': SCOPES,
        # Both are required for Google to hand back a refresh token at all.
        'access_type': 'offline',
        'prompt': 'consent',
        'include_granted_scopes': 'true',
        'code_challenge': pkce_challenge(verifier),
        'code_challenge_method': 'S256',
        'state': json.dumps(state),
    }))


def _post_token(env, grant_type, **values):
    """Single place where a token request is made and its errors are mapped."""
    client_id, client_secret = get_credentials(env)

    try:
        response = requests.post(
            TOKEN_ENDPOINT,
            data={
                'client_id': client_id,
                'client_secret': client_secret,
                'grant_type': grant_type,
                **values,
            },
            timeout=TIMEOUT,
        )
    except requests.exceptions.RequestException as error:
        _logger.warning('Cold Email: cannot reach Google OAuth: %s', error)
        raise GoogleOAuthError(_('Could not reach Google. Please try again later.')) from error

    if response.ok:
        return response.json()

    # Never log the response body verbatim: an authorization_code request
    # echoes back enough to be worth keeping out of the log file.
    try:
        payload = response.json()
        reason = payload.get('error')
        detail = payload.get('error_description') or reason
    except ValueError:
        reason, detail = None, response.reason

    _logger.warning('Cold Email: OAuth %s failed (%s): %s', grant_type, response.status_code, reason)

    if reason == 'invalid_grant':
        raise GoogleOAuthError(_(
            'Google rejected the stored authorisation. This happens when access was '
            'revoked, or when the OAuth consent screen is still in Testing mode '
            '(refresh tokens then expire after 7 days). Reconnect the mailbox.'
        ), reason=reason)
    if reason in ('invalid_client', 'unauthorized_client'):
        raise GoogleOAuthError(_(
            'Google rejected the Client ID or Client Secret. Check them in '
            'Settings > Cold Email.'
        ), reason=reason)

    raise GoogleOAuthError(
        _('Google refused the request: %s', detail or response.status_code),
        reason=reason,
    )


def exchange_code(env, code, verifier):
    """Trade the authorization code for the initial token pair.

    :return: ``(refresh_token, access_token, expires_in)``
    """
    payload = _post_token(
        env, 'authorization_code',
        code=code,
        code_verifier=verifier,
        redirect_uri=redirect_uri(env),
    )

    refresh_token = payload.get('refresh_token')
    if not refresh_token:
        # Happens when the user had already granted access and Google decided
        # to skip the consent screen; prompt=consent normally prevents it.
        raise GoogleOAuthError(_(
            'Google did not return a refresh token. Remove this app from your '
            'Google account permissions and connect again.'
        ))

    return refresh_token, payload['access_token'], payload.get('expires_in', 3600)


def refresh_access_token(env, refresh_token):
    """:return: ``(access_token, expires_in)``"""
    payload = _post_token(env, 'refresh_token', refresh_token=refresh_token)
    return payload['access_token'], payload.get('expires_in', 3600)


def revoke(env, token):
    """Best-effort revocation; a failure here must not block disconnecting."""
    try:
        response = requests.post(REVOKE_ENDPOINT, data={'token': token}, timeout=TIMEOUT)
        if not response.ok:
            _logger.info('Cold Email: token revocation returned %s', response.status_code)
    except requests.exceptions.RequestException as error:
        _logger.info('Cold Email: could not revoke token: %s', error)
