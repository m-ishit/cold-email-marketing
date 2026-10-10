"""RFC 5322 message construction for the Gmail API.

``users.messages.send`` wants a base64url-encoded MIME message, so we build one
ourselves rather than going anywhere near ``ir.mail_server``.
"""
import base64
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from odoo.tools import html2plaintext


def encode(message):
    """base64url the message, as ``messages.send`` expects."""
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def build(from_name, from_email, to_email, subject, body_html, in_reply_to=None, references=None):
    """Assemble a multipart/alternative message.

    :param in_reply_to: RFC Message-ID of the message being answered. Setting
        it (together with ``references`` and a matching ``Re:`` subject) is what
        makes Gmail file the message into the existing conversation.
    :return: ``EmailMessage``
    """
    message = EmailMessage()
    message['From'] = formataddr((from_name or '', from_email))
    message['To'] = to_email
    message['Subject'] = subject
    message['Date'] = formatdate(localtime=True)
    # Gmail overwrites this on send; we set it so the message is well-formed
    # even in the tests, where nothing round-trips through Google.
    message['Message-ID'] = make_msgid(domain=from_email.split('@')[-1])

    if in_reply_to:
        message['In-Reply-To'] = in_reply_to
        # References carries the whole chain; keep the parent last.
        chain = [ref for ref in (references or '').split() if ref != in_reply_to]
        chain.append(in_reply_to)
        message['References'] = ' '.join(chain)

    message.set_content(html2plaintext(body_html or ''))
    message.add_alternative(body_html or '', subtype='html')

    return message


def reply_subject(subject):
    """Gmail refuses to thread a message whose subject drifted from the thread."""
    subject = subject or ''
    if subject[:3].lower() == 're:':
        return subject
    return 'Re: %s' % subject
