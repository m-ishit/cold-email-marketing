from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import tagged

from odoo.addons.cold_email_gmail.tools import oauth
from odoo.addons.cold_email_gmail.tools.gmail_api import GmailApiError

from .common import ColdEmailCommon, FakeGmailApi


@tagged('post_install', '-at_install', 'cold_email')
class TestSending(ColdEmailCommon):

    def test_scheduled_mail_is_queued(self):
        mail = self.create_mail(scheduled_at=fields.Datetime.now())
        self.assertEqual(mail.state, 'scheduled')

    def test_cron_sends_a_due_message(self):
        mail = self.create_mail()

        with self.mock_gmail():
            sent = self.env['cold.mail']._cron_send_queued()

        self.assertEqual(sent, 1)
        self.assertEqual(mail.state, 'sent')
        self.assertTrue(mail.sent_at)
        self.assertTrue(mail.gmail_message_id)
        self.assertTrue(mail.gmail_thread_id)
        self.assertIn(mail.gmail_message_id, mail.gmail_raw_response)
        self.assertFalse(mail.error_status)
        self.assertFalse(mail.error_reason)

    def test_cron_skips_messages_not_yet_due(self):
        mail = self.create_mail(scheduled_at=fields.Datetime.now() + timedelta(days=1))

        with self.mock_gmail():
            sent = self.env['cold.mail']._cron_send_queued()

        self.assertEqual(sent, 0)
        self.assertEqual(mail.state, 'scheduled')

    def test_daily_limit_caps_a_run(self):
        for i in range(5):
            self.create_mail(email_to='c%s@example.com' % i)
        self.mailbox.daily_limit = 2

        with self.mock_gmail():
            sent = self.env['cold.mail']._cron_send_queued()

        self.assertEqual(sent, 2)
        self.assertEqual(self.mailbox.sent_today, 2)

        # Quota exhausted: a second run inside the same day sends nothing.
        with self.mock_gmail():
            self.assertEqual(self.env['cold.mail']._cron_send_queued(), 0)

    def test_sending_window_blocks_out_of_hours(self):
        mail = self.create_mail()
        # A window that has already closed for every hour of the day.
        self.mailbox.write({'window_start': 0.0, 'window_end': 0.01})

        with self.mock_gmail():
            sent = self.env['cold.mail']._cron_send_queued()

        self.assertEqual(sent, 0)
        self.assertEqual(mail.state, 'scheduled')

    def test_min_delay_paces_consecutive_sends(self):
        for i in range(3):
            self.create_mail(email_to='p%s@example.com' % i)
        self.mailbox.min_delay_seconds = 600

        with self.mock_gmail():
            sent = self.env['cold.mail']._cron_send_queued()

        # One goes out; the rest wait for the next cron beat.
        self.assertEqual(sent, 1)

    def test_disconnected_mailbox_is_skipped(self):
        mail = self.create_mail()
        self.mailbox.sudo().state = 'disconnected'

        with self.mock_gmail() as api:
            sent = self.env['cold.mail']._cron_send_queued()

        self.assertEqual(sent, 0)
        self.assertEqual(mail.state, 'scheduled')
        self.assertEqual(api.sent, [])

    def test_transient_error_is_retried_with_backoff(self):
        mail = self.create_mail()
        FakeGmailApi.fail_with = GmailApiError('rate limited', status=429)

        with self.mock_gmail():
            self.env['cold.mail']._cron_send_queued()

        self.assertEqual(mail.state, 'scheduled')
        self.assertEqual(mail.retry_count, 1)
        self.assertGreater(mail.scheduled_at, fields.Datetime.now())

    def test_permanent_error_fails_the_message(self):
        mail = self.create_mail()
        FakeGmailApi.fail_with = GmailApiError(
            'bad recipient', status=400, reason='invalidArgument',
            raw={'error': {'code': 400, 'message': 'bad recipient', 'status': 'INVALID_ARGUMENT'}})

        with self.mock_gmail():
            self.env['cold.mail']._cron_send_queued()

        self.assertEqual(mail.state, 'failed')
        self.assertIn('bad recipient', mail.error_message)
        self.assertEqual(mail.error_status, 400)
        self.assertEqual(mail.error_reason, 'invalidArgument')
        self.assertIn('INVALID_ARGUMENT', mail.gmail_raw_response)

    def test_retry_action_requeues_a_failed_message(self):
        mail = self.create_mail()
        mail.write({'state': 'failed', 'retry_count': 3, 'error_message': 'boom'})

        mail.action_retry()

        self.assertEqual(mail.state, 'scheduled')
        self.assertEqual(mail.retry_count, 0)
        self.assertFalse(mail.error_message)

    def test_cancel_action_stops_a_scheduled_message(self):
        mail = self.create_mail()
        mail.action_cancel()
        self.assertEqual(mail.state, 'cancelled')

    def test_bad_client_credentials_do_not_burn_the_queue(self):
        """A settings typo is a mailbox problem, not a per-message failure."""
        for i in range(4):
            self.create_mail(email_to='q%s@example.com' % i)
        self.mailbox.sudo().access_token_expiration = 0

        error = oauth.GoogleOAuthError('bad client', reason='invalid_client')
        with self.mock_gmail(), patch.object(oauth, 'refresh_access_token', side_effect=error):
            sent = self.env['cold.mail']._cron_send_queued()

        self.assertEqual(sent, 0)
        # Every message stays queued: none of them did anything wrong.
        mails = self.env['cold.mail'].search([('mailbox_id', '=', self.mailbox.id)])
        self.assertEqual(set(mails.mapped('state')), {'scheduled'})
        self.assertFalse(any(mails.mapped('retry_count')))
        self.assertEqual(self.mailbox.state, 'error')
        self.assertIn('bad client', self.mailbox.last_error)

    def test_revoked_authorisation_marks_the_mailbox_expired(self):
        mail = self.create_mail()
        self.mailbox.sudo().access_token_expiration = 0

        error = oauth.GoogleOAuthError('revoked', reason='invalid_grant')
        with self.mock_gmail(), patch.object(oauth, 'refresh_access_token', side_effect=error):
            self.env['cold.mail']._cron_send_queued()

        self.assertEqual(self.mailbox.state, 'expired')
        self.assertEqual(mail.state, 'scheduled')

    def test_fatal_api_refusal_stops_the_mailbox(self):
        """A domain policy refusal will not clear up message by message."""
        for i in range(3):
            self.create_mail(email_to='f%s@example.com' % i)

        FakeGmailApi.fail_with = GmailApiError(
            'Delegation denied', status=403, reason='domainPolicy')
        with self.mock_gmail():
            sent = self.env['cold.mail']._cron_send_queued()

        self.assertEqual(sent, 0)
        mails = self.env['cold.mail'].search([('mailbox_id', '=', self.mailbox.id)])
        self.assertEqual(set(mails.mapped('state')), {'scheduled'})
        self.assertEqual(self.mailbox.state, 'error')
        self.assertIn('Delegation denied', self.mailbox.last_error)

    def test_message_is_multipart_text_and_html(self):
        self.create_mail()

        with self.mock_gmail() as api:
            self.env['cold.mail']._cron_send_queued()

        raw = api.decode_last()
        self.assertIn('text/plain', raw)
        self.assertIn('text/html', raw)

    def test_no_unsubscribe_header_is_added(self):
        """The unsubscribe mechanism was deliberately dropped from this module."""
        self.create_mail()

        with self.mock_gmail() as api:
            self.env['cold.mail']._cron_send_queued()

        raw = api.decode_last()
        self.assertNotIn('List-Unsubscribe', raw)

    def test_bounce_sync_marks_the_message_bounced(self):
        mail = self.create_mail()
        with self.mock_gmail():
            self.env['cold.mail']._cron_send_queued()

        FakeGmailApi.inbox = [{
            'id': 'bounce-1',
            'threadId': mail.gmail_thread_id,
            'from': 'Mail Delivery Subsystem <mailer-daemon@googlemail.com>',
        }]

        with self.mock_gmail():
            self.env['cold.mail']._cron_sync_bounces()

        self.assertEqual(mail.state, 'bounced')

    def test_reply_requires_a_parent(self):
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            self.create_mail(send_mode='reply')

    def test_reply_threads_into_the_parent_conversation(self):
        parent = self.create_mail(email_to='jane@prospect.example.com')
        with self.mock_gmail():
            self.env['cold.mail']._cron_send_queued()
        self.assertEqual(parent.state, 'sent')

        reply = self.create_mail(
            email_to='jane@prospect.example.com', send_mode='reply', parent_id=parent.id,
            subject='Re: Hello Jane', scheduled_at=fields.Datetime.now())

        with self.mock_gmail() as api:
            self.env['cold.mail']._cron_send_queued()

        self.assertEqual(reply.state, 'sent')
        self.assertEqual(reply.gmail_thread_id, parent.gmail_thread_id)
        raw = api.decode_last()
        self.assertIn('In-Reply-To', raw)
        self.assertIn(parent.rfc_message_id, raw)

    def test_reply_falls_back_to_new_thread_without_a_sendable_parent(self):
        """A reply whose parent never made it out threads as new mail instead
        of failing outright -- degraded, but visible and harmless."""
        pending_parent = self.create_mail(scheduled_at=fields.Datetime.now() + timedelta(days=1))
        reply = self.create_mail(send_mode='reply', parent_id=pending_parent.id)

        self.assertEqual(reply._effective_send_mode(), 'new_thread')

    def test_reply_from_a_different_mailbox_is_rejected(self):
        from odoo.exceptions import UserError
        parent = self.create_mail()
        with self.mock_gmail():
            self.env['cold.mail']._cron_send_queued()

        other_mailbox = self.env['cold.mailbox'].create({
            'name': 'Other Mailbox', 'user_id': self.rep.id, 'state': 'connected',
            'email': 'other-mailbox@example.com',
        })
        with self.assertRaises(UserError):
            self.create_mail(mailbox_id=other_mailbox.id, send_mode='reply', parent_id=parent.id)

    def test_a_previously_bounced_address_is_skipped(self):
        """A dead address must not be hit again just because a new email
        record was scheduled for it -- protects sender reputation."""
        bounced = self.create_mail(email_to='dead@example.com')
        with self.mock_gmail():
            self.env['cold.mail']._cron_send_queued()
        bounced.write({'state': 'bounced'})

        retry = self.create_mail(email_to='dead@example.com')
        with self.mock_gmail() as api:
            self.env['cold.mail']._cron_send_queued()

        self.assertEqual(retry.state, 'cancelled')
        self.assertIn('bounced before', retry.error_message)
        self.assertEqual(api.sent, [])

    def test_bounce_check_ignores_case(self):
        bounced = self.create_mail(email_to='Dead@Example.com')
        with self.mock_gmail():
            self.env['cold.mail']._cron_send_queued()
        bounced.write({'state': 'bounced'})

        retry = self.create_mail(email_to='dead@example.com')
        self.assertTrue(retry._find_prior_bounce())
