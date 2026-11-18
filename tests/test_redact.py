import base64
import dataclasses
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ie_mcp import config, session
from ie_mcp.capture import redact
from ie_mcp.capture.redact import MARKER, Redactor


class NameTests(unittest.TestCase):
    def test_name_segments(self):
        self.assertEqual(redact.name_segments("lastName"), ["last", "name"])
        self.assertEqual(redact.name_segments("X-Auth-Token"), ["x", "auth", "token"])
        self.assertEqual(redact.name_segments("password2"), ["password", "2"])
        self.assertEqual(redact.name_segments("XMLHttpRequest"), ["xml", "http", "request"])
        self.assertEqual(redact.name_segments("USERPWD"), ["userpwd"])
        self.assertEqual(redact.name_segments(""), [])

    def test_ordinary_field_names_are_not_secret(self):
        for name in ("lastName", "compassHeading", "keyword", "author", "keyCode", "bypassCache",
                     "spinner", "sCode", "user", "code", "x"):
            self.assertFalse(redact.is_secret_field(name, set()), name)

    def test_legacy_password_field_names_are_secret(self):
        for name in ("newpassword", "USERPWD", "TXTPASSWORD", "password1", "password2",
                     "userPass", "cardPin", "pass", "PIN", "secret", "access_token", "otp",
                     "sid", "JSESSIONID", "api_key", "csrf", "xsrf", "passphrase", "passwd"):
            self.assertTrue(redact.is_secret_field(name, set()), name)

    def test_extra_fields_are_matched_case_insensitively_by_whole_name(self):
        self.assertTrue(redact.is_secret_field("Badge-No", {"badgeno"}))
        self.assertFalse(redact.is_secret_field("badge", {"badgeno"}))

    def test_secret_headers(self):
        for name in ("Cookie", "Set-Cookie", "authorization", "Proxy-Authorization",
                     "X-Auth-Token", "X-Api-Key", "X-CSRF-Token", "X-Session-Id", "X-Secret"):
            self.assertTrue(redact.is_secret_header(name, set()), name)
        for name in ("Content-Type", "Accept", "Referer", "User-Agent", "Keep-Alive"):
            self.assertFalse(redact.is_secret_header(name, set()), name)
        self.assertTrue(redact.is_secret_header("X-Badge", {"x-badge"}))


class RedactorTests(unittest.TestCase):
    def setUp(self):
        self.r = Redactor(set(), set())

    def test_redacts_password_field_in_form_body(self):
        body = self.r.redact_body("application/x-www-form-urlencoded", b"code=A1&pwd=s3cret", None)

        self.assertEqual(body, b"code=A1&pwd=<redacted>")

    def test_form_body_without_content_type_is_sniffed(self):
        self.assertEqual(self.r.redact_body("", b"code=A1&pwd=s3cret", None),
                         b"code=A1&pwd=<redacted>")

    def test_form_body_keeps_everything_else_byte_for_byte(self):
        raw = b"a=%E9&b=x+y&pwd=s3cret&empty=&flag&c=\xe9"

        out = self.r.redact_body("application/x-www-form-urlencoded", raw, "iso-8859-1")

        self.assertEqual(out, b"a=%E9&b=x+y&pwd=<redacted>&empty=&flag&c=\xe9")

    def test_percent_encoded_secret_is_tracked_decoded_and_raw(self):
        self.r.redact_body("application/x-www-form-urlencoded", b"pwd=a%26b%20c", None)

        self.assertIn("a&b c", self.r.secret_values)
        self.assertIn("a%26b%20c", self.r.secret_values)

    def test_redacts_query_string_field(self):
        self.assertEqual(self.r.redact_url("/x?user=a&token=abcd1234"),
                         "/x?user=a&token=<redacted>")

    def test_redacts_fragment_and_userinfo(self):
        url = self.r.redact_url("http://bob:hunter22@h/p#access_token=abcd1234&x=1")

        self.assertEqual(url, "http://bob:<redacted>@h/p#access_token=<redacted>&x=1")

    def test_jsessionid_path_parameter_redacted(self):
        url = self.r.redact_url("/app;jsessionid=ABCDEF123456?x=1")

        self.assertIn(";jsessionid=<redacted>", url)
        self.assertTrue(url.endswith("?x=1"))
        self.assertNotIn("ABCDEF123456", url)

    def test_redacts_json_and_xml_keys(self):
        self.assertNotIn(b"s3cret", self.r.redact_body(
            "application/json", b'{"a":{"password":"s3cret"}}', None))
        self.assertNotIn(b"s3cret", self.r.redact_body(
            "text/xml", b"<r><pwd>s3cret</pwd></r>", None))
        self.assertNotIn(b"s3cret", self.r.redact_body(
            "application/xml", b'<x password="s3cret"/>', None))

    def test_json_redaction_keeps_layout_and_other_values(self):
        raw = b'{ "user" : "ann",\n  "list": [ {"Token": ["abcd1234", 99999999]}, 1 ],\n "n": 5 }'

        out = self.r.redact_body("application/json", raw, None)

        self.assertEqual(
            out, b'{ "user" : "ann",\n  "list": [ {"Token": "<redacted>"}, 1 ],\n "n": 5 }')
        self.assertEqual(self.r.secret_values, {"abcd1234", "99999999"})

    def test_malformed_json_and_xml_do_not_raise(self):
        for ctype, body in (("application/json", b'{"password": "abc'),
                            ("application/json", b'{"password":'),
                            ("application/json", b'\xff\xfe{"password":"x"}'),
                            ("text/xml", b"<pwd>unclosed"),
                            ("multipart/form-data; boundary=zz", b"--zz\r\ngarbage"),
                            ("multipart/form-data", b"no boundary given")):
            self.r.redact_body(ctype, body, None)

    def test_xml_cdata_and_attributes(self):
        out = self.r.redact_body(
            "text/xml", b'<a id="1" apiKey="k-12345678"><password><![CDATA[p@ss<word>]]></password>'
                        b"<name>Ann</name></a>", None)

        self.assertEqual(out, b'<a id="1" apiKey="<redacted>"><password><redacted></password>'
                              b"<name>Ann</name></a>")

    def test_multipart_secret_part_is_redacted_and_others_kept(self):
        body = (b"--zz\r\nContent-Disposition: form-data; name=\"user\"\r\n\r\nann\r\n"
                b"--zz\r\nContent-Disposition: form-data; name=\"passwd\"\r\n\r\ns3cret\r\n"
                b"--zz\r\nContent-Disposition: form-data; name=\"f\"; filename=\"a.txt\"\r\n"
                b"Content-Type: text/plain\r\n\r\nfile body\r\n--zz--\r\n")

        out = self.r.redact_body("multipart/form-data; boundary=zz", body, None)

        self.assertEqual(out, body.replace(b"s3cret", b"<redacted>"))
        self.assertIn("s3cret", self.r.secret_values)

    def test_multipart_json_part_is_redacted_by_key(self):
        body = (b"--zz\r\nContent-Disposition: form-data; name=\"data\"\r\n"
                b"Content-Type: application/json\r\n\r\n{\"pin\":\"4711\",\"token\":\"abcd1234\"}"
                b"\r\n--zz--\r\n")

        out = self.r.redact_body('multipart/form-data; boundary="zz"', body, None)

        self.assertNotIn(b"abcd1234", out)

    def test_redacts_cookie_auth_and_custom_token_headers(self):
        headers = [("Cookie", "SID=abcd1234; theme=dark"), ("Authorization", "Bearer zzzz9999"),
                   ("X-Auth-Token", "tok-5678"), ("X-Api-Key", "k3y-12345"),
                   ("Content-Type", "text/html"), ("Set-Cookie", "id=cafe1234; Path=/; HttpOnly")]

        out = dict(self.r.redact_headers(headers))

        for name in ("Cookie", "Authorization", "X-Auth-Token", "X-Api-Key", "Set-Cookie"):
            self.assertEqual(out[name], MARKER)
        self.assertEqual(out["Content-Type"], "text/html")

    def test_cookie_pairs_are_tracked_one_by_one_and_attributes_are_not(self):
        self.r.redact_headers([("Cookie", "a=aaaa1111; b=bbbb2222; theme=dark")])
        self.r.redact_headers([("Set-Cookie", "c=cccc3333; Path=/app; Expires=Wed, 21 Oct 2030; "
                                              "HttpOnly")])

        # Every Cookie pair is a value ("dark" included); Path/Expires of Set-Cookie are not.
        self.assertEqual(self.r.secret_values, {"aaaa1111", "bbbb2222", "cccc3333", "dark"})

    def test_authorization_scheme_and_basic_password(self):
        token = base64.b64encode(b"ann:hunter22").decode()
        self.r.redact_headers([("Authorization", "Basic " + token),
                               ("Proxy-Authorization", "Bearer zzzz9999")])

        self.assertLessEqual({"hunter22", token, "zzzz9999"}, self.r.secret_values)
        self.assertNotIn("Basic", self.r.secret_values)

    def test_redacts_referer_and_location_query(self):
        out = dict(self.r.redact_headers([
            ("Referer", "http://h/p?token=abcd1234"), ("Location", "/next?pwd=s3cret&a=1"),
            ("Content-Location", "/c;jsessionid=ZZZZ9999")]))

        self.assertEqual(out["Referer"], "http://h/p?token=<redacted>")
        self.assertEqual(out["Location"], "/next?pwd=<redacted>&a=1")
        self.assertEqual(out["Content-Location"], "/c;jsessionid=<redacted>")

    def test_extra_fields_and_headers_from_settings(self):
        r = Redactor({"badgeno"}, {"x-badge"})

        self.assertEqual(r.redact_url("/p?badgeNo=12345678&a=1"), "/p?badgeNo=<redacted>&a=1")
        self.assertEqual(r.redact_headers([("X-Badge", "b-123")]), [("X-Badge", MARKER)])

    def test_ordinary_fields_not_redacted(self):
        raw = b"lastName=Smith&compassHeading=N&keyword=abc&author=Ann&keyCode=13&" \
              b"bypassCache=1&spinner=on"

        self.assertEqual(
            self.r.redact_body("application/x-www-form-urlencoded", raw, None), raw)
        self.assertEqual(self.r.secret_values, set())

    def test_legacy_password_names_redacted(self):
        raw = (b"newpassword=aaaa1111&USERPWD=bbbb2222&TXTPASSWORD=cccc3333&password1=dddd4444"
               b"&password2=eeee5555&userPass=ffff6666&cardPin=gggg7777")

        out = self.r.redact_body("application/x-www-form-urlencoded", raw, None)

        self.assertEqual(out.count(b"=<redacted>"), 7)
        self.assertEqual(len(self.r.secret_values), 7)

    def test_short_values_masked_only_as_tokens(self):
        self.r.record("test")

        self.assertEqual(self.r.mask("latest test"), "latest <redacted>")
        # "_" is not alphanumeric, so it bounds a token; a digit or letter does not.
        self.assertEqual(self.r.mask("test_x test2 (test)"), "<redacted>_x test2 (<redacted>)")

    def test_prose_with_header_like_words_is_left_alone_and_not_recorded(self):
        for prose in ("KeyError: 'ready-state'", "no such session: window handle gone",
                      "Waiting for session: the browser is starting",
                      "token expired: retry later", "secret: none yet"):
            self.assertEqual(self.r.redact_text(prose), prose)
        both = "KeyError: 'ready-state'\nno such session: window handle gone"
        self.assertEqual(self.r.redact_text(both), both)

        self.assertEqual(self.r.secret_values, set())

    def test_colon_form_for_secret_headers_takes_the_rest_of_the_line(self):
        self.assertEqual(self.r.redact_text("Authorization: Bearer abc123xyz"),
                         "Authorization: <redacted>")
        self.assertEqual(self.r.redact_text("X-Auth-Token: abcd1234"), "X-Auth-Token: <redacted>")
        self.assertEqual(self.r.redact_text("Cookie: a=cccc3333; b=dddd4444 tail"),
                         "Cookie: <redacted>")
        self.assertLessEqual({"abc123xyz", "Bearer abc123xyz", "abcd1234", "cccc3333"},
                             self.r.secret_values)

    def test_colon_form_for_password_like_names_takes_the_first_token(self):
        self.assertEqual(self.r.redact_text("password: hunter22 next"), "password: <redacted> next")
        self.assertEqual(self.r.redact_text("userPwd: 'two words' next"),
                         "userPwd: '<redacted>' next")
        self.assertIn("two words", self.r.secret_values)

    def test_colon_form_for_configured_names(self):
        r = Redactor({"badgeno"}, {"badge-header", "xcustom"})

        self.assertEqual(r.redact_text("badgeNo: 12345678 next"), "badgeNo: <redacted> next")
        self.assertEqual(r.redact_text("Badge-Header: some value"), "Badge-Header: <redacted>")
        self.assertEqual(r.redact_text("XCustom: whole line"), "XCustom: <redacted>")

    def test_equals_form_keeps_the_looser_name_rules(self):
        self.assertEqual(self.r.redact_text("token=abcd1234"), "token=<redacted>")
        self.assertEqual(self.r.redact_text("go session=sess9876 now"), "go session=<redacted> now")

    def test_str_and_bytes_masks_use_the_same_token_boundaries(self):
        self.r.record("test")
        text = "\u00e9test test\u00e9 latest"

        # A non-ASCII letter next to the secret does not make it part of a longer word.
        self.assertEqual(self.r.mask(text), "\u00e9<redacted> <redacted>\u00e9 latest")
        self.assertEqual(self.r.mask_bytes(text.encode()),
                         "\u00e9<redacted> <redacted>\u00e9 latest".encode())

    def test_long_values_masked_anywhere(self):
        self.r.record("abcdefgh")

        self.assertEqual(self.r.mask("xabcdefghx"), "x<redacted>x")

    def test_trivial_values_not_tracked(self):
        self.r.redact_body("application/x-www-form-urlencoded",
                           b"pwd=true&pin=null&otp=12345&token=ON&pass=no&key=1234567", None)

        self.assertEqual(self.r.secret_values, set())
        self.assertEqual(self.r.mask("true and false 12345"), "true and false 12345")
        self.r.record("12345678")
        self.assertIn("12345678", self.r.secret_values)

    def test_empty_and_marker_values_are_ignored(self):
        self.r.redact_body("application/x-www-form-urlencoded", b"pwd=&pin=<redacted>", None)
        self.r.record("redacted")
        self.r.record("dact")

        self.assertEqual(self.r.secret_values, set())
        self.assertEqual(self.r.mask(MARKER), MARKER)

    def test_secret_value_is_masked_in_bytes_in_utf8_and_the_charset(self):
        value = "pässwörd9"
        self.r.record(value)

        for charset, encoding in ((None, "utf-8"), ("iso-8859-1", "utf-8"),
                                  ("iso-8859-1", "iso-8859-1"), ("windows-1252", "cp1252"),
                                  ("no-such-charset", "utf-8"), ("utf-16", "utf-16-le")):
            data = "<p>".encode(encoding) + value.encode(encoding) + "</p>".encode(encoding)
            out = self.r.mask_bytes(data, [charset])
            self.assertNotIn(value.encode(encoding), out, (charset, encoding))
            # The marker is plain ASCII even in a UTF-16 body.
            self.assertIn(b"<redacted>" if "16" in encoding else "<redacted>".encode(encoding),
                          out, (charset, encoding))

    def test_mask_bytes_never_raises_on_odd_charsets(self):
        self.r.record("abcdefgh")

        self.assertEqual(self.r.mask_bytes(b"abcdefgh", ["???", "", None]), b"<redacted>")

    def test_masks_redacted_value_anywhere_afterwards(self):
        self.r.redact_url("/x?token=abcd1234")

        self.assertEqual(self.r.mask("echo abcd1234!"), "echo <redacted>!")

    def test_redact_text_handles_pairs_and_known_values(self):
        self.assertEqual(self.r.redact_text("pwd=xyz9 sCode=A1"), "pwd=<redacted> sCode=A1")
        self.assertEqual(self.r.redact_text("seen xyz9"), "seen <redacted>")

    def test_redact_text_json_and_colon_pairs(self):
        out = self.r.redact_text(
            '{"password":"s3cret!", "user":"ann"} Authorization: Bearer abcd1234 xyz\n'
            "token: tok-99999 and more\nContent-Type: text/html")

        self.assertEqual(
            out, '{"password":"<redacted>", "user":"ann"} Authorization: <redacted>\n'
                 "token: tok-99999 and more\nContent-Type: text/html")  # prose-like: kept

    def test_redact_text_is_idempotent_and_keeps_urls(self):
        once = self.r.redact_text("GET http://h/p?token=abcd1234&x=1 pwd=<redacted>")

        self.assertEqual(once, "GET http://h/p?token=<redacted>&x=1 pwd=<redacted>")
        self.assertEqual(self.r.redact_text(once), once)

    def test_redaction_can_be_disabled(self):
        r = Redactor({"x"}, {"y"}, enabled=False)
        headers = [("Cookie", "a=abcd1234")]

        self.assertIs(r.redact_headers(headers), headers)
        self.assertEqual(r.redact_url("/x?pwd=abcd1234"), "/x?pwd=abcd1234")
        self.assertEqual(r.redact_body("application/x-www-form-urlencoded", b"pwd=abcd1234", None),
                         b"pwd=abcd1234")
        self.assertEqual(r.redact_text("pwd=abcd1234"), "pwd=abcd1234")
        r.record("abcd1234")
        self.assertEqual(r.mask("abcd1234"), "abcd1234")
        self.assertEqual(r.mask_bytes(b"abcd1234"), b"abcd1234")
        self.assertEqual(r.secret_values, set())

    def test_is_thread_safe(self):
        def work(n):
            for i in range(100):
                self.r.redact_url("/x?token=value%d-%d" % (n, i))
                self.r.mask("value%d-%d" % (n, i))
                self.r.mask_bytes(b"value1-1")

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(self.r.secret_values), 800)


class LogRedactionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log_file = Path(tmp.name) / "ie-mcp.log"
        paths = dataclasses.replace(config.SETTINGS.session_paths, log_file=self.log_file)
        original = config.SETTINGS
        config.SETTINGS = dataclasses.replace(original, session_paths=paths)
        self.addCleanup(setattr, config, "SETTINGS", original)
        original_redactor = session.REDACTOR
        session.REDACTOR = Redactor(set(), set())
        self.addCleanup(setattr, session, "REDACTOR", original_redactor)
        stderr = mock.patch("sys.stderr", io.StringIO())
        self.stderr = stderr.start()
        self.addCleanup(stderr.stop)

    def test_log_lines_are_redacted(self):
        session.log("GET http://h/?token=abcd1234")
        session.log("later: abcd1234 again")

        text = self.log_file.read_text(encoding="utf-8")
        self.assertIn("token=<redacted>", text)
        self.assertNotIn("abcd1234", text)
        self.assertNotIn("abcd1234", self.stderr.getvalue())

    def test_a_failing_redactor_never_lets_the_line_through(self):
        with mock.patch.object(session.REDACTOR, "redact_text", side_effect=RuntimeError("x")):
            session.log("pwd=s3cretvalue")

        self.assertNotIn("s3cretvalue", self.log_file.read_text(encoding="utf-8"))

    def test_disabled_redactor_leaves_the_log_alone(self):
        session.REDACTOR = Redactor(set(), set(), enabled=False)

        session.log("pwd=s3cretvalue")

        self.assertIn("pwd=s3cretvalue", self.log_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
