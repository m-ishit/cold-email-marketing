import time
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.cold_email_gmail.tools import oauth

from .common import ColdEmailCommon


@tagged('post_install', '-at_install', 'cold_email')
class TestMailboxOAuth(ColdEmailCommon):

    def test_access_token_reused_while_valid(self):
        """A token with time left must not cost a round trip to Google."""
        with patch.object(oauth, 'refresh_access_token') as refresh:
            token = self.mailbox._get_access_token()
        self.assertEqual(token, 'fake-access-token')
        refresh.assert_not_called()

    def test_access_token_refreshed_near_expiry(self):
        """Inside the safety margin the token is renewed before use."""
        self.mailbox.sudo().access_token_expiration = int(time.time()) + 5

        with patch.object(oauth, 'refresh_access_token', return_value=('new-token', 3600)) as refresh:
            token = self.mailbox._get_access_token()

        refresh.assert_called_once()
        self.assertEqual(token, 'new-token')
        self.assertEqual(self.mailbox.sudo().access_token, 'new-token')

    def test_invalid_grant_marks_mailbox_expired(self):
        """A revoked authorisation must surface as 'expired', not a crash loop."""
        self.mailbox.sudo().access_token_expiration = 0
        error = oauth.GoogleOAuthError('revoked', reason='invalid_grant')

        with patch.object(oauth, 'refresh_access_token', side_effect=error):
            with self.assertRaises(oauth.GoogleOAuthError):
                self.mailbox._get_access_token()

        # assertRaises rolls its block back, so re-read after the fact.
        self.mailbox.sudo().access_token_expiration = 0
        with patch.object(oauth, 'refresh_access_token', side_effect=error):
            try:
                self.mailbox._get_access_token()
            except oauth.GoogleOAuthError:
                pass
        self.assertEqual(self.mailbox.state, 'expired')
        self.assertTrue(self.mailbox.last_error)

    def test_csrf_token_is_bound_to_the_record(self):
        other = self.env['cold.mailbox'].create({'name': 'Other', 'user_id': self.rep.id})
        self.assertNotEqual(self.mailbox._get_csrf_token(), other._get_csrf_token())

    def test_callback_state_rejects_bad_csrf(self):
        import json
        good = json.dumps({'id': self.mailbox.id, 'csrf': self.mailbox._get_csrf_token()})
        self.assertEqual(
            self.env['cold.mailbox']._get_state_from_callback(good),
            self.mailbox,
        )

        tampered = json.dumps({'id': self.mailbox.id, 'csrf': 'not-the-right-token'})
        with self.assertRaises(ValueError):
            self.env['cold.mailbox']._get_state_from_callback(tampered)

        with self.assertRaises(ValueError):
            self.env['cold.mailbox']._get_state_from_callback('not json at all')

    def test_store_tokens_captures_the_authenticated_address(self):
        """The address comes from Gmail, not from whatever was typed in Odoo."""
        fresh = self.env['cold.mailbox'].create({'name': 'Fresh', 'user_id': self.rep.id})

        with self.mock_gmail():
            address = fresh._store_tokens('refresh-1', 'access-1', 3600)

        self.assertEqual(address, 'rep@example.com')
        self.assertEqual(fresh.state, 'connected')
        self.assertEqual(fresh.sudo().refresh_token, 'refresh-1')
        self.assertFalse(fresh.sudo().pkce_verifier)

    def test_rep_cannot_connect_someone_elses_mailbox(self):
        foreign = self.env['cold.mailbox'].create({
            'name': 'Manager mailbox', 'user_id': self.manager.id,
        })
        with self.assertRaises(AccessError):
            foreign.with_user(self.rep)._check_can_manage()

    def test_sending_window_must_be_coherent(self):
        with self.assertRaises(UserError):
            self.env['cold.mailbox'].create({
                'name': 'Bad window', 'user_id': self.rep.id,
                'window_start': 18.0, 'window_end': 9.0,
            })

    def test_pkce_challenge_is_url_safe_and_unpadded(self):
        verifier = oauth.new_pkce_verifier()
        challenge = oauth.pkce_challenge(verifier)
        self.assertNotIn('=', challenge)
        self.assertNotIn('+', challenge)
        self.assertNotIn('/', challenge)

    def test_authorization_url_requests_the_right_scopes(self):
        url = oauth.authorization_url(self.env, {'id': self.mailbox.id, 'csrf': 'x'},
                                      oauth.new_pkce_verifier())
        self.assertIn('gmail.send', url)
        self.assertIn('gmail.readonly', url)
        self.assertIn('access_type=offline', url)
        self.assertIn('prompt=consent', url)
        self.assertIn('code_challenge_method=S256', url)
        # No SMTP/IMAP scope: this module never opens a mail socket.
        self.assertNotIn('mail.google.com', url)
