"""The public OAuth callback route, exercised over real HTTP.

The QWeb error page calls ``request.csrf_token``, so it can only be rendered
inside a genuine request -- an HttpCase is the only way to prove it works.
"""
from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install', 'cold_email')
class TestPublicRoutes(HttpCase):

    def test_oauth_callback_refuses_an_unsigned_state(self):
        self.authenticate('admin', 'admin')
        response = self.url_open(
            '/cold_email/oauth/callback?code=abc&state=%7B%22id%22%3A1%2C%22csrf%22%3A%22x%22%7D')
        self.assertEqual(response.status_code, 403)

    def test_oauth_callback_shows_a_page_when_the_user_declines(self):
        self.authenticate('admin', 'admin')
        response = self.url_open('/cold_email/oauth/callback?error=access_denied')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Could not connect', response.text)
