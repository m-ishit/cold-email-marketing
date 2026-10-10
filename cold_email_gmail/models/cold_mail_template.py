from odoo import fields, models


class ColdMailTemplate(models.Model):
    """Reusable canned content for a ``cold.mail``.

    Kept deliberately small -- a name, a subject and a body -- rather than
    reusing ``mail.template``, which drags in reports, attachments and mail
    server settings that have no place here. Picking one on a ``cold.mail``
    copies its subject/body across; nothing links back to this record
    afterwards, so editing a template never touches emails already scheduled.

    ``model`` is a plain constant, not a compute: it exists only so the
    Subject/Body editors here can offer ``{{object.field}}`` dynamic
    placeholders scoped to ``cold.mail``, the same way ``mail.template``
    uses its own ``model`` field for the same widget.
    """

    _name = 'cold.mail.template'
    _description = 'Cold Email Template'
    _order = 'name'

    name = fields.Char(required=True)
    model = fields.Char(default='cold.mail', readonly=True)
    subject = fields.Char()
    body_html = fields.Html(sanitize=False)

    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    active = fields.Boolean(default=True)
