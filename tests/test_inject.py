"""Unit tests for the loader insertion rules (no browser, no network)."""
import unittest

from ie_mcp.capture.proxy import (NEED_MORE, NEVER, find_insertion_point, is_document_request,
                                  loader_tag)

BOM = b"\xef\xbb\xbf"
XUA = b'<meta http-equiv="X-UA-Compatible" content="IE=7">'
DOCTYPE = b'<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">'

LEADING = (DOCTYPE + b'<html><head><title>t</title>' + XUA + b'<base href="http://example.test/">'
           b'<link rel="stylesheet" href="a.css"></head><body>x</body></html>')
HEADLESS = (DOCTYPE + b'<html>' + XUA + b'<title>x</title><body>hello</body></html>')
AFTER_HEAD = BOM + b'<!DOCTYPE html><html><head><script>var a;</script></head><body></body></html>'
UPPER = b'<HTML><HEAD><TITLE>x</TITLE><SCRIPT>alert(1)</SCRIPT></HEAD><BODY></BODY></HTML>'
FRAMESET = b'<html><frameset cols="*,*"><frame src="a.htm"><frame src="b.htm"></frameset></html>'
BODY_FIRST = b"<body>x</body>"
FRAMESET_FIRST = b"<frameset cols='*'><frame src=a></frameset>"
HEADER_TAG = b"<!DOCTYPE html><html><header>x</header><body></body></html>"
QUOTED = (b'<!DOCTYPE html><html><head><meta content="a>b"><title>a <meta> b</title>'
          b"<meta name='x>y' content=z><link href=q></head></html>")
COMMENTS = (b"<!-- lead --><!DOCTYPE html>\n<!-- <head> -->\n<html>\n<!-- <meta> -->\n<head>\n"
            b"<!-- c --><meta charset=utf-8>\n<script>1</script></head></html>")
FRAGMENTS = (b"OK|A1", b"<tr><td>1</td></tr>", b"  <div>x</div>", b"<?xml version='1.0'?><a/>",
             b"<![CDATA[x]]>", b"</html>", b"< 3 is less")

ALL_DOCS = (LEADING, HEADLESS, AFTER_HEAD, UPPER, FRAMESET, BODY_FIRST, FRAMESET_FIRST,
            HEADER_TAG, QUOTED, COMMENTS) + FRAGMENTS


class InsertionPointTests(unittest.TestCase):
    def test_inject_after_leading_meta_title_base(self):
        self.assertEqual(find_insertion_point(LEADING, False), LEADING.index(b"<link"))

    def test_headless_document_keeps_xua_meta_first(self):
        offset = find_insertion_point(HEADLESS, False)
        self.assertEqual(offset, HEADLESS.index(b"<body>"))
        self.assertGreater(offset, HEADLESS.index(b"X-UA-Compatible"))

    def test_never_inject_before_doctype_or_bom(self):
        doc = b"\xef\xbb\xbf<!DOCTYPE html><html><head><script>"
        offset = find_insertion_point(doc, False)
        self.assertEqual(offset, doc.index(b"<script>"))
        self.assertGreater(offset, len(BOM + b"<!DOCTYPE html>"))
        self.assertEqual(doc[:offset].endswith(b"<head>"), True)

    def test_uppercase_tags_recognised(self):
        self.assertEqual(find_insertion_point(UPPER, False), UPPER.index(b"<SCRIPT"))

    def test_fragment_never_injected(self):
        for fragment in FRAGMENTS:
            with self.subTest(fragment=fragment):
                self.assertIs(find_insertion_point(fragment, True), NEVER)

    def test_utf16_never_injected(self):
        self.assertIs(find_insertion_point(b"\xff\xfe<\x00h\x00t\x00m\x00l\x00>\x00", False), NEVER)
        self.assertIs(find_insertion_point(b"\xfe\xff\x00<\x00h", True), NEVER)

    def test_headless_frameset_not_injected(self):
        self.assertIs(find_insertion_point(FRAMESET, False), NEVER)
        self.assertIs(find_insertion_point(DOCTYPE + FRAMESET, False), NEVER)
        # a comment that mentions <head> is not a head tag
        doc = b"<html><!-- <head> --><frameset cols='*'></frameset></html>"
        self.assertIs(find_insertion_point(doc, False), NEVER)

    def test_frameset_with_head_gets_the_loader_in_the_head(self):
        doc = b"<html><head><title>t</title></head><frameset cols='*'></frameset></html>"
        self.assertEqual(find_insertion_point(doc, False), doc.index(b"</head>"))

    def test_every_prefix_needs_more_or_final_answer(self):
        for doc in ALL_DOCS:
            full = find_insertion_point(doc, False)
            with self.subTest(doc=doc[:40]):
                self.assertIsNot(full, NEED_MORE, "a complete document must be decided")
            for length in range(len(doc) + 1):
                prefix = doc[:length]
                got = find_insertion_point(prefix, False)
                with self.subTest(doc=doc[:40], length=length):
                    self.assertTrue(got is NEED_MORE or got == full or got is full,
                                    "prefix %r gave %r, full answer %r" % (prefix, got, full))
                    # at eof there is always a final answer
                    self.assertIsNot(find_insertion_point(prefix, True), NEED_MORE)

    def test_body_first_and_frameset_first(self):
        self.assertEqual(find_insertion_point(BODY_FIRST, False), 0)
        self.assertEqual(find_insertion_point(b"\xef\xbb\xbf  <BODY class=a>", False), 5)
        self.assertIs(find_insertion_point(FRAMESET_FIRST, False), NEVER)

    def test_tag_names_need_a_delimiter(self):
        offset = find_insertion_point(HEADER_TAG, False)
        self.assertEqual(offset, HEADER_TAG.index(b"<header>"))
        self.assertEqual(HEADER_TAG[:offset], b"<!DOCTYPE html><html>")
        # <head/ and <head\t count as head tags
        doc = b"<html><head\n><link></head></html>"
        self.assertEqual(find_insertion_point(doc, False), doc.index(b"<link>"))
        # <metadata> and <titles> are not skippable meta/title elements
        doc = b"<html><head><metadata></metadata></head></html>"
        self.assertEqual(find_insertion_point(doc, False), doc.index(b"<metadata>"))

    def test_quoted_gt_and_comments_ignored(self):
        self.assertEqual(find_insertion_point(QUOTED, False), QUOTED.index(b"<link"))
        self.assertEqual(find_insertion_point(COMMENTS, False), COMMENTS.index(b"<script>"))

    def test_skipped_run_includes_whitespace_and_comments(self):
        doc = b"<html><head>\n <meta charset=x>\n<!-- c -->\n<style>a{}</style></head></html>"
        self.assertEqual(find_insertion_point(doc, False), doc.index(b"<style>"))

    def test_charset_meta_in_the_skipped_run_stays_before_the_insert(self):
        # IE prescans only the start of a document for a charset meta. The loader goes after
        # every leading meta/title/base, so it never pushes such a meta later.
        for meta in (b'<meta http-equiv="Content-Type" content="text/html; charset=windows-1252">',
                     b'<meta charset="utf-8">'):
            doc = (b"<!DOCTYPE html><html><head>" + XUA + b"<title>t</title>" + meta +
                   b"<script>var a;</script></head><body></body></html>")
            with self.subTest(meta=meta):
                offset = find_insertion_point(doc, False)
                self.assertGreaterEqual(offset, doc.index(meta) + len(meta))
                self.assertEqual(doc[:offset].count(meta), 1)

    def test_title_content_is_skipped_as_text(self):
        doc = b"<html><head><title>a</title ><link></head></html>"
        self.assertEqual(find_insertion_point(doc, False), doc.index(b"<link>"))
        # an unfinished title needs more data, or at eof ends the run where the title starts
        partial = b"<html><head><title>abc"
        self.assertIs(find_insertion_point(partial, False), NEED_MORE)
        self.assertEqual(find_insertion_point(partial, True), partial.index(b"<title>"))

    def test_eof_decisions(self):
        self.assertIs(find_insertion_point(b"", False), NEED_MORE)
        self.assertIs(find_insertion_point(b"", True), NEVER)
        self.assertIs(find_insertion_point(b"<ht", True), NEVER)
        self.assertEqual(find_insertion_point(b"<html><head>", True), len(b"<html><head>"))
        self.assertIs(find_insertion_point(b"<html><head>", False), NEED_MORE)
        self.assertEqual(find_insertion_point(b"<html><he", True), len(b"<html>"))
        self.assertIs(find_insertion_point(b"<html><he", False), NEED_MORE)

    def test_nothing_decided_within_64k_is_never(self):
        padding = b" " * 70000
        self.assertIs(find_insertion_point((b"<html><head>" + padding)[:60000], False), NEED_MORE)
        self.assertIs(find_insertion_point((b"<html><head>" + padding)[:65536], False), NEVER)
        self.assertIs(find_insertion_point((b"<!-- " + b"x" * 70000)[:65536], False), NEVER)
        self.assertIs(find_insertion_point((b"<!-- " + b"x" * 70000)[:1000], False), NEED_MORE)


class LoaderTagTests(unittest.TestCase):
    def test_absolute_http_url_of_the_document_host(self):
        self.assertEqual(
            loader_tag("http://127.0.0.1:8080/dir/page.htm?x=1#f"),
            b'<script type="text/javascript" src="http://127.0.0.1:8080/__iemcp__/i.js"></script>')

    def test_default_port_userinfo_and_ipv6(self):
        self.assertIn(b'src="http://app.example.test/__iemcp__/i.js"',
                      loader_tag("http://user:pw@app.example.test/a"))
        self.assertIn(b'src="http://[::1]:9/__iemcp__/i.js"', loader_tag("http://[::1]:9/"))

    def test_tag_is_short_for_a_typical_url(self):
        self.assertLess(len(loader_tag("http://127.0.0.1:51234/some/page.htm")), 100)

    def test_url_without_host_is_rejected(self):
        with self.assertRaises(ValueError):
            loader_tag("/relative/path")


class DocumentRequestTests(unittest.TestCase):
    def test_accept_text_html(self):
        self.assertTrue(is_document_request("GET", {"Accept": "text/html, application/xhtml+xml"}))
        self.assertTrue(is_document_request("post", {"accept": "image/gif, TEXT/HTML, */*"}))

    def test_xhr_and_other_requests(self):
        self.assertFalse(is_document_request("POST", {"Accept": "*/*"}))
        self.assertFalse(is_document_request("GET", {"Accept": "image/png"}))
        self.assertFalse(is_document_request("GET", {}))
        self.assertFalse(is_document_request("HEAD", {"Accept": "text/html"}))
        self.assertFalse(is_document_request("PUT", {"Accept": "text/html"}))

    def test_http_message_headers(self):
        import http.client
        import io
        message = http.client.parse_headers(io.BytesIO(b"aCCept: text/html\r\nHost: x\r\n\r\n"))
        self.assertTrue(is_document_request("GET", message))


if __name__ == "__main__":
    unittest.main()
