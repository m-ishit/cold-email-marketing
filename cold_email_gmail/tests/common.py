"""Shared fixtures and a fake Gmail transport.

Nothing here touches the network: ``FakeGmailApi`` records every call and hands
back the shapes the real API returns, so the tests assert on what we *would*
have sent rather than on Google's behaviour.
"""
import time
from contextlib import contextmanager
from unittest.mock import patch

from odoo.tests import TransactionCase


class FakeGmailApi:
    """Stand-in for ``tools.gmail_api.GmailApi``.

    Class-level ``sent`` collects every send across instances, because the code
    under test constructs its own instance per message.
    """

    sent = []
    inbox = []
    profile_email = 'rep@example.com'
    fail_with = None
    message_id_counter = 0

    def __init__(self, mailbox):
        self.mailbox = mailbox
        # The real client resolves a bearer token on every request. Doing the
        # same here keeps the OAuth path in the loop: without it the suite
        # silently skips token refresh and expiry handling entirely.
        self.token = mailbox.sudo()._get_access_token()

    @classmethod
    def reset(cls):
        cls.sent = []
        cls.inbox = []
        cls.fail_with = None
        cls.message_id_counter = 0

    def get_profile(self):
        return {'emailAddress': self.profile_email, 'historyId': '12345'}

    def send(self, raw_message, thread_id=None):
        if FakeGmailApi.fail_with:
            raise FakeGmailApi.fail_with

        FakeGmailApi.message_id_counter += 1
        counter = FakeGmailApi.message_id_counter

        gmail_thread = thread_id or 'thread-%s' % counter

        FakeGmailApi.sent.append({
            'raw': raw_message,
            'thread_id': thread_id,
            'message_id': 'msg-%s' % counter,
            'gmail_thread_id': gmail_thread,
        })
        return {'id': 'msg-%s' % counter, 'threadId': gmail_thread}

    def get_headers(self, message_id, header_names):
        # Gmail rewrites the Message-ID; return the rewritten one.
        return {'message-id': '<%s@mail.gmail.com>' % message_id}

    def get_message_summary(self, message_id):
        for message in FakeGmailApi.inbox:
            if message['id'] == message_id:
                return {'from': message.get('from', 'prospect@example.com')}
        return {'from': 'prospect@example.com'}

    def list_messages(self, query, label_ids=None, limit=500):
        return [{'id': m['id'], 'threadId': m['threadId']} for m in FakeGmailApi.inbox]

    @staticmethod
    def decode_last():
        """The raw MIME of the most recent send, as text."""
        import base64
        return base64.urlsafe_b64decode(FakeGmailApi.sent[-1]['raw']).decode()


class ColdEmailCommon(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        FakeGmailApi.reset()

        cls.manager = cls.env['res.users'].create({
            'name': 'Cold Manager',
            'login': 'cold_manager',
            'email': 'manager@example.com',
            'group_ids': [(4, cls.env.ref('cold_email_gmail.group_cold_email_manager').id)],
        })
        cls.rep = cls.env['res.users'].create({
            'name': 'Cold Rep',
            'login': 'cold_rep',
            'email': 'rep@example.com',
            'group_ids': [(4, cls.env.ref('cold_email_gmail.group_cold_email_user').id)],
        })
        cls.other_rep = cls.env['res.users'].create({
            'name': 'Other Rep',
            'login': 'cold_other',
            'email': 'other@example.com',
            'group_ids': [(4, cls.env.ref('cold_email_gmail.group_cold_email_user').id)],
        })

        cls.env['ir.config_parameter'].sudo().set_param('cold_email.gmail_client_id', 'test-client-id')
        cls.env['ir.config_parameter'].sudo().set_param('cold_email.gmail_client_secret', 'test-secret')

        cls.mailbox = cls.env['cold.mailbox'].create({
            'name': 'Rep Mailbox',
            'user_id': cls.rep.id,
            'daily_limit': 10,
            'min_delay_seconds': 0,
            'window_start': 0.0,
            'window_end': 24.0,
            'tz': 'UTC',
        })
        cls.mailbox.sudo().write({
            'email': 'rep@example.com',
            'state': 'connected',
            'refresh_token': 'fake-refresh-token',
            'access_token': 'fake-access-token',
            'access_token_expiration': int(time.time()) + 3600,
        })

    def setUp(self):
        super().setUp()
        # Per test, not per class: the fake's counters are class-level, so a
        # leftover from an earlier test would make send assertions meaningless.
        FakeGmailApi.reset()

    @contextmanager
    def mock_gmail(self):
        """Swap the transport everywhere the models reach for it."""
        with patch('odoo.addons.cold_email_gmail.models.cold_mail.GmailApi', FakeGmailApi), \
             patch('odoo.addons.cold_email_gmail.models.cold_mailbox.GmailApi', FakeGmailApi):
            yield FakeGmailApi

    def create_mail(self, **values):
        vals = {
            'mailbox_id': self.mailbox.id,
            'email_to': 'jane@prospect.example.com',
            'subject': 'Hello Jane',
            'body_html': '<p>Hi Jane, quick question.</p>',
        }
        vals.update(values)
        return self.env['cold.mail'].create(vals)
