{
    'name': 'Cold Email (Gmail API)',
    'version': '19.0.3.0.0',
    'category': 'Marketing/Email Marketing',
    'summary': 'Schedule one-off emails sent through the Gmail REST API',
    'description': """
Cold Email (Gmail API)
======================
A flat list of scheduled emails sent through Gmail entirely over its REST API:

* connect one mailbox per user with Google OAuth 2.0 (no password, no SMTP),
* compose a message (From, To, Subject, HTML body) and pick when it goes out,
* tag emails, pick a saved template, and insert the recipient's name/company
  as a dynamic placeholder (type / in the body),
* a cron sends everything due, respecting each mailbox's daily limit,
  pacing and sending window,
* detect bounces and mark the message accordingly,
* discuss, log notes and schedule follow-up activities per email via chatter.

Nothing in this module uses SMTP: no ir.mail_server, no mail.mail, no
fetchmail.server. Every Gmail operation is an HTTPS call to
gmail.googleapis.com.
    """,
    'author': 'Hirebeans',
    'license': 'LGPL-3',
    'price': 0.00,
    'currency': 'USD',
    'depends': ['mail'],
    'data': [
        'security/cold_groups.xml',
        'security/ir.model.access.csv',
        'security/cold_record_rules.xml',
        'data/ir_sequence_data.xml',
        'data/ir_cron_data.xml',
        'views/cold_templates.xml',
        'views/cold_mailbox_views.xml',
        'views/cold_mail_tag_views.xml',
        'views/cold_mail_template_views.xml',
        'views/cold_mail_views.xml',
        'views/res_config_settings_views.xml',
        'views/cold_menus.xml',
    ],
    'external_dependencies': {
        'python': ['requests'],
    },
    'installable': True,
    'application': True,
}
