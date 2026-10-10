from random import randint

from odoo import fields, models


class ColdMailTag(models.Model):
    """A free-form label on a ``cold.mail`` record.

    Deliberately not tied to any campaign/sequence concept -- this is purely
    a way to group emails visually (e.g. "Canada Drip 5") now that the
    campaign engine is gone.
    """

    _name = 'cold.mail.tag'
    _description = 'Cold Email Tag'
    _order = 'name'

    def _default_color(self):
        return randint(1, 11)

    name = fields.Char(required=True)
    color = fields.Integer(default=_default_color)

    _unique_name = models.Constraint('UNIQUE(name)', 'This tag already exists.')
