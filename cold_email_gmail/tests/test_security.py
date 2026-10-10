from odoo.exceptions import AccessError
from odoo.tests import tagged

from .common import ColdEmailCommon


@tagged('post_install', '-at_install', 'cold_email')
class TestSecurity(ColdEmailCommon):

    def test_oauth_credentials_are_invisible_to_a_rep(self):
        """The tokens sit on group_system fields; a rep must not read them."""
        mailbox = self.mailbox.with_user(self.rep)
        with self.assertRaises(AccessError):
            mailbox.read(['refresh_token'])

    def test_rep_cannot_see_another_reps_mailbox(self):
        foreign = self.env['cold.mailbox'].create({
            'name': 'Other mailbox', 'user_id': self.other_rep.id,
        })
        visible = self.env['cold.mailbox'].with_user(self.rep).search([])
        self.assertNotIn(foreign, visible)
        self.assertIn(self.mailbox, visible)

    def test_rep_cannot_see_another_reps_mail(self):
        mine = self.create_mail(user_id=self.rep.id)
        foreign = self.create_mail(user_id=self.other_rep.id, email_to='other@example.com')

        visible = self.env['cold.mail'].with_user(self.rep).search([])
        self.assertIn(mine, visible)
        self.assertNotIn(foreign, visible)

    def test_manager_sees_every_mail(self):
        mine = self.create_mail(user_id=self.rep.id)
        foreign = self.create_mail(user_id=self.other_rep.id, email_to='other@example.com')

        visible = self.env['cold.mail'].with_user(self.manager).search([])
        self.assertIn(mine, visible)
        self.assertIn(foreign, visible)

    def test_a_plain_user_sees_nothing(self):
        plain = self.env['res.users'].create({
            'name': 'Plain', 'login': 'plain_user',
            'group_ids': [(4, self.env.ref('base.group_user').id)],
        })
        with self.assertRaises(AccessError):
            self.env['cold.mail'].with_user(plain).search([])
