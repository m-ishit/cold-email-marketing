from odoo import api, fields, models

from ..tools import oauth


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Labels must stay distinct from google_gmail's own client id/secret
    # fields, which live on the same model; the view relabels them.
    cold_email_gmail_client_id = fields.Char(
        string='Cold Email Gmail Client ID',
        config_parameter='cold_email.gmail_client_id')
    cold_email_gmail_client_secret = fields.Char(
        string='Cold Email Gmail Client Secret',
        config_parameter='cold_email.gmail_client_secret')
    cold_email_redirect_uri = fields.Char(
        string='Cold Email Redirect URI', readonly=True,
        compute='_compute_cold_email_redirect_uri',
        help='Copy this into the OAuth client configuration in the Google Cloud console.')

    @api.depends_context('uid')
    def _compute_cold_email_redirect_uri(self):
        uri = oauth.redirect_uri(self.env)
        for setting in self:
            setting.cold_email_redirect_uri = uri
