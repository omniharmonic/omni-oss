"""proton_mail.py writes the email's Cc into the note metadata (final review N2), in the same shape and key
place the Prism server ingest uses (apps/server/src/worker/proton-parse.ts noteMetadata, pinned there by
test/fixtures/proton-script-fixture.json, which was re-derived from THIS function). Synthetic mail only."""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
os.environ.setdefault('PROTON_BRIDGE_USER', 'inbox@example.test')
import proton_mail as pm  # noqa: E402

RAW = (b"From: \"Grace Q. Sample\" <grace@example.net>\r\nTo: inbox@example.test, jordan@example.com\r\n"
       b"Cc: Team Lead <team-lead@example.com>, pat@example.org\r\nSubject: Re: Budget\r\n"
       b"Date: Wed, 30 Sep 2026 11:05:00 -0600\r\nMessage-ID: <m2@example.net>\r\n\r\nHello\r\n")

RAW_WITH_ATTACHMENT = (b"From: Grace <grace@example.net>\r\nTo: inbox@example.test\r\n"
                       b"Subject: Draft\r\nMIME-Version: 1.0\r\n"
                       b"Content-Type: multipart/mixed; boundary=part\r\n\r\n"
                       b"--part\r\nContent-Type: text/plain\r\n\r\nPlease review.\r\n"
                       b"--part\r\nContent-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n"
                       b"Content-Disposition: attachment; filename=Draft.docx\r\n"
                       b"Content-Transfer-Encoding: base64\r\n\r\nUEsDBA==\r\n"
                       b"--part--\r\n")

RAW_WITH_HTML_LINKS = (b"From: Grace <grace@example.net>\r\nTo: inbox@example.test\r\n"
                       b"Subject: Onboarding\r\nMIME-Version: 1.0\r\n"
                       b"Content-Type: text/html; charset=utf-8\r\n\r\n"
                       b'<p>Fill out <a href="https://forms.example.test/start?a=1&amp;b=2">this form</a>.</p>'
                       b'<a href="https://forms.example.test/start?a=1&amp;b=2">duplicate</a>')


class Capture:
    def get_note(self, *a, **k):
        return None

    def create_note(self, **k):
        self.metadata = k['metadata']


def metadata(raw):
    c = Capture()
    pm.upsert_email_note(c, pm.parse_message(raw, '', 'INBOX', 7))
    return c.metadata


class CcMetadataTests(unittest.TestCase):
    def test_cc_is_stored_as_the_parsed_addresses_right_after_to(self):
        m = metadata(RAW)
        self.assertEqual(m['cc'], 'team-lead@example.com, pat@example.org')
        keys = list(m)
        self.assertEqual(keys[keys.index('to') + 1], 'cc', 'same key order as the server ingest')

    def test_no_cc_header_is_an_empty_string(self):
        raw = RAW.replace(b"Cc: Team Lead <team-lead@example.com>, pat@example.org\r\n", b"")
        self.assertEqual(metadata(raw)['cc'], '')

    def test_extract_attachment_returns_exact_named_payload(self):
        self.assertEqual(pm.extract_attachment(RAW_WITH_ATTACHMENT, 'Draft.docx'), b'PK\x03\x04')

    def test_extract_attachment_rejects_missing_name(self):
        with self.assertRaisesRegex(ValueError, 'not found'):
            pm.extract_attachment(RAW_WITH_ATTACHMENT, 'Other.docx')

    def test_extract_links_preserves_html_href_and_decodes_entities(self):
        self.assertEqual(
            pm.extract_links(RAW_WITH_HTML_LINKS),
            ['https://forms.example.test/start?a=1&b=2'],
        )

    def test_exact_message_payload_includes_body_and_links(self):
        payload = pm.message_payload(RAW_WITH_HTML_LINKS, '', 'INBOX', 42)
        self.assertEqual(payload['uid'], 42)
        self.assertIn('Fill out this form', payload['body'])
        self.assertEqual(payload['links'], ['https://forms.example.test/start?a=1&b=2'])


if __name__ == '__main__':
    unittest.main()


class MailboxNameTest(unittest.TestCase):
    """A mailbox name is quoted into an IMAP command: nothing that could close the quote gets through."""

    def test_ordinary_names_pass_and_injection_is_refused(self):
        import proton_mail
        for name in proton_mail.DEFAULT_MAILBOXES + ["Folders/Receipts", "Labels/A & B"]:
            self.assertEqual(proton_mail._quoted_mailbox(name), '"%s"' % name)
        for bad in ('INBOX"\r\nA1 STORE 1 +FLAGS (\\Deleted)', 'INBOX\nX', 'a"b', "a\\b", "", "x" * 101, None):
            with self.assertRaises(proton_mail.BridgeError, msg=repr(bad)):
                proton_mail._quoted_mailbox(bad)
