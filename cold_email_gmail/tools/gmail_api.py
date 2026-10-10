"""Thin transport layer over the Gmail REST API.

Every HTTPS call to gmail.googleapis.com goes through here, so the models stay
free of request plumbing and the tests can swap the whole class for a fake.

Reference: https://developers.google.com/workspace/gmail/api/reference/rest
"""
import logging

import requests

from odoo import _

_logger = logging.getLogger(__name__)

API_BASE = 'https://gmail.googleapis.com/gmail/v1/users/me'
UPLOAD_BASE = 'https://gmail.googleapis.com/upload/gmail/v1/users/me'

TIMEOUT = (10, 60)

# Gmail switches to the resumable/multipart upload endpoint past 5 MB of raw
# MIME; below that the plain JSON body is cheaper.
SIMPLE_UPLOAD_LIMIT = 5 * 1024 * 1024

# Google error reasons that clear up on their own.
TRANSIENT_REASONS = {
    'rateLimitExceeded',
    'userRateLimitExceeded',
    'backendError',
    'internalError',
    'quotaExceeded',
}

# Google error reasons that will keep failing until a human intervenes.
FATAL_MAILBOX_REASONS = {
    'domainPolicy',
    'failedPrecondition',
    'forbidden',
    'insufficientPermissions',
}


class GmailApiError(Exception):
    """A call reached Gmail and Gmail refused it.

    ``transient`` says whether retrying later is worthwhile; ``fatal_mailbox``
    says the mailbox itself needs attention and the whole batch should stop.
    ``raw`` is Google's own error body, kept verbatim so it can be stored and
    inspected later instead of only the summarised message/status/reason.
    """

    def __init__(self, message, status=None, reason=None, raw=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.reason = reason
        self.raw = raw

    @property
    def transient(self):
        return self.status == 429 or (self.status or 0) >= 500 or self.reason in TRANSIENT_REASONS

    @property
    def fatal_mailbox(self):
        return self.reason in FATAL_MAILBOX_REASONS


class GmailApi:
    """Bound to one ``cold.mailbox`` record; carries its credentials."""

    def __init__(self, mailbox):
        # sudo: the OAuth fields are restricted to base.group_system, but a
        # Cold Email User must still be able to send from their own mailbox.
        self.mailbox = mailbox.sudo()

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    def _request(self, method, url, *, params=None, json=None, data=None,
                 headers=None, _retried=False):
        request_headers = dict(headers or {})
        request_headers['Authorization'] = 'Bearer %s' % self.mailbox._get_access_token()

        if self.mailbox.debug_logging:
            # Never let the bearer token reach the log file.
            _logger.info('Cold Email: %s %s params=%s', method, url, params)

        try:
            response = requests.request(
                method, url,
                params=params, json=json, data=data,
                headers=request_headers, timeout=TIMEOUT,
            )
        except requests.exceptions.RequestException as error:
            raise GmailApiError(_('Could not reach Gmail: %s', error)) from error

        if response.ok:
            return response.json() if response.content else {}

        status, reason, message, raw = self._parse_error(response)

        # An expired access token is the one error worth papering over: refresh
        # and replay exactly once, so a token that died mid-batch costs nothing.
        if status == 401 and not _retried:
            _logger.info('Cold Email: access token rejected, refreshing and retrying once')
            self.mailbox._invalidate_access_token()
            return self._request(
                method, url, params=params, json=json, data=data,
                headers=headers, _retried=True,
            )

        raise GmailApiError(message, status=status, reason=reason, raw=raw)

    @staticmethod
    def _parse_error(response):
        reason = None
        message = response.reason
        raw = None
        try:
            raw = response.json()
            error = raw.get('error') or {}
            message = error.get('message') or message
            details = error.get('errors') or []
            if details:
                reason = details[0].get('reason')
            reason = reason or error.get('status')
        except ValueError:
            pass
        return response.status_code, reason, message, raw

    # ------------------------------------------------------------------
    # endpoints
    # ------------------------------------------------------------------

    def get_profile(self):
        """users.getProfile -> {'emailAddress', 'messagesTotal', 'historyId'}.

        Used instead of Google's oauth2/v2/userinfo so the module needs no
        userinfo.email scope.
        """
        return self._request('GET', '%s/profile' % API_BASE)

    def send(self, raw_message, thread_id=None):
        """users.messages.send -> {'id', 'threadId', 'labelIds'}.

        :param raw_message: base64url-encoded RFC 5322 message
        :param thread_id: Gmail thread to append to. Gmail only honours it when
            the message also carries matching References/In-Reply-To headers
            and a matching subject line.
        """
        body = {'raw': raw_message}
        if thread_id:
            body['threadId'] = thread_id

        if len(raw_message) > SIMPLE_UPLOAD_LIMIT:
            return self._send_upload(raw_message, thread_id)

        return self._request('POST', '%s/messages/send' % API_BASE, json=body)

    def _send_upload(self, raw_message, thread_id=None):
        """Large messages must go through the media upload endpoint."""
        params = {'uploadType': 'media'}
        body = {'raw': raw_message}
        if thread_id:
            body['threadId'] = thread_id
        return self._request(
            'POST', '%s/messages/send' % UPLOAD_BASE,
            params=params, json=body,
        )

    def get_headers(self, message_id, header_names):
        """Return the requested RFC headers of a message as a dict.

        Gmail rewrites the Message-ID on send, so the value we put in the MIME
        is not the one recipients see. Threading a later follow-up needs the
        real one, and this is the only way to obtain it.
        """
        payload = self._request(
            'GET', '%s/messages/%s' % (API_BASE, message_id),
            params={'format': 'metadata', 'metadataHeaders': header_names},
        )
        headers = (payload.get('payload') or {}).get('headers') or []
        wanted = {name.lower() for name in header_names}
        return {
            header['name'].lower(): header['value']
            for header in headers
            if header.get('name', '').lower() in wanted
        }

    def list_messages(self, query, label_ids=None, limit=500):
        """users.messages.list, following nextPageToken.

        :return: list of ``{'id', 'threadId'}``
        """
        params = {'q': query, 'maxResults': 100}
        if label_ids:
            params['labelIds'] = label_ids

        messages = []
        page_token = None
        while len(messages) < limit:
            if page_token:
                params['pageToken'] = page_token
            payload = self._request('GET', '%s/messages' % API_BASE, params=params)
            messages.extend(payload.get('messages') or [])
            page_token = payload.get('nextPageToken')
            if not page_token:
                break
        return messages[:limit]

    def get_message_summary(self, message_id):
        """Headers needed to tell a human reply from a bounce notification."""
        return self.get_headers(message_id, ['From', 'Subject', 'Message-Id', 'In-Reply-To'])
