import json
import tempfile
import threading
import unittest
from pathlib import Path

from ie_mcp.capture.redact import Redactor
from ie_mcp.capture.store import CaptureStore, NetworkEntry


def entry(**kw):
    values = dict(started=1.0, duration_ms=5, method="GET", url="http://h/p", status=200)
    values.update(kw)
    return NetworkEntry(**values)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.redactor = Redactor(set(), set())

    def store(self, **kw):
        return CaptureStore(self.redactor, **kw)

    def test_masks_redacted_value_in_response_body(self):
        store = self.store()
        form = [("Content-Type", "application/x-www-form-urlencoded")]

        entry_id = store.add(entry(
            method="POST", req_headers=form, req_body=b"pwd=s3cret&code=A1",
            resp_body=b'<dd id="echo_pwd">s3cret</dd>'))

        stored = store.get(entry_id)
        self.assertNotIn(b"s3cret", stored.resp_body)
        self.assertEqual(stored.resp_body, b'<dd id="echo_pwd"><redacted></dd>')
        self.assertEqual(stored.req_body, b"pwd=<redacted>&code=A1")

    def test_masks_secret_everywhere_in_the_entry(self):
        store = self.store()

        entry_id = store.add(entry(
            url="http://h/p?token=abcd1234", req_headers=[("Cookie", "SID=cafe1234")],
            resp_headers=[("X-Echo", "cafe1234"), ("Content-Type", "text/html")],
            resp_body=b"abcd1234 cafe1234", error="failed for abcd1234"))

        stored = store.get(entry_id)
        self.assertEqual(stored.url, "http://h/p?token=<redacted>")
        self.assertEqual(stored.req_headers, [("Cookie", "<redacted>")])
        self.assertEqual(stored.resp_headers,
                         [("X-Echo", "<redacted>"), ("Content-Type", "text/html")])
        self.assertEqual(stored.resp_body, b"<redacted> <redacted>")
        self.assertEqual(stored.error, "failed for <redacted>")

    def test_non_ascii_secret_is_masked_in_the_pages_charset(self):
        store = self.store()
        value = "paßwort9"

        entry_id = store.add(entry(
            method="POST", req_headers=[("Content-Type", "application/x-www-form-urlencoded")],
            req_body=b"pwd=pa%DFwort9", referer_charset="iso-8859-1",
            resp_headers=[("Content-Type", "text/html; charset=iso-8859-1")],
            resp_body=("<i>" + value + "</i>").encode("iso-8859-1")))

        self.assertEqual(store.get(entry_id).resp_body, b"<i><redacted></i>")

    def test_a_secret_learned_later_is_masked_in_stored_entries(self):
        store = self.store()
        first = store.add(entry(resp_body=b"<input value=zz-token-99>"))
        second = store.add(entry(method="POST", req_body=b"csrf=zz-token-99",
                                 req_headers=[("Content-Type", "text/plain")]))

        self.assertEqual(store.get(first).resp_body, b"<input value=<redacted>>")
        self.assertEqual([e.id for e in store.list(limit=None)], [first, second])
        self.assertNotIn(b"zz-token-99", store.list()[0].resp_body)

    def test_malformed_bodies_never_raise_out_of_add(self):
        store = self.store()

        store.add(entry(req_headers=[("Content-Type", "application/json; charset=bogus")],
                        req_body=b'\xff{"password":', resp_body=b"\xff\xfe"))
        store.add(entry(req_headers=[("Content-Type", "multipart/form-data; boundary=q")],
                        req_body=b"--q\r\nbroken"))

        self.assertEqual(len(store.list()), 2)

    def test_redaction_can_be_disabled(self):
        self.redactor = Redactor(set(), set(), enabled=False)
        store = self.store()

        entry_id = store.add(entry(url="http://h/?token=abcd1234"))

        self.assertEqual(store.get(entry_id).url, "http://h/?token=abcd1234")

    def test_add_does_not_change_the_callers_entry(self):
        store = self.store()
        original = entry(url="http://h/?token=abcd1234", req_headers=[("Cookie", "a=zzzz1111")])

        store.add(original)

        self.assertEqual(original.url, "http://h/?token=abcd1234")
        self.assertEqual(original.req_headers, [("Cookie", "a=zzzz1111")])
        self.assertEqual(original.id, 0)

    def test_store_ring_buffer_drops_oldest(self):
        store = self.store(max_entries=2)

        for _ in range(3):
            store.add(entry())

        self.assertEqual([e.id for e in store.list()], [2, 3])
        self.assertIsNone(store.get(1))
        self.assertEqual(store.get(3).id, 3)

    def test_ids_keep_counting_after_clear(self):
        store = self.store()
        store.add(entry())

        store.clear()

        self.assertEqual(store.list(), [])
        self.assertEqual(store.add(entry()), 2)

    def test_store_filters_by_url_method_status(self):
        store = self.store()
        store.add(entry(url="http://h/a", method="GET", status=200))
        store.add(entry(url="http://h/b", method="POST", status=500))
        store.add(entry(url="http://h/ab?token=abcd1234", method="POST", status=200))

        self.assertEqual([e.id for e in store.list(url_contains="/a")], [1, 3])
        self.assertEqual([e.id for e in store.list(method="post")], [2, 3])
        self.assertEqual([e.id for e in store.list(status=200)], [1, 3])
        self.assertEqual([e.id for e in store.list(method="POST", status=200)], [3])
        self.assertEqual([e.id for e in store.list(url_contains="abcd1234")], [])
        self.assertEqual([e.id for e in store.list(url_contains="token=<redacted>")], [3])

    def test_list_pages_oldest_first_with_since_id_and_limit(self):
        store = self.store()
        for _ in range(5):
            store.add(entry())

        self.assertEqual([e.id for e in store.list(limit=2)], [1, 2])
        self.assertEqual([e.id for e in store.list(since_id=2, limit=2)], [3, 4])
        self.assertEqual([e.id for e in store.list(since_id=4, limit=50)], [5])
        self.assertEqual(store.list(since_id=5), [])

    def test_a_returned_entry_is_a_copy(self):
        store = self.store()
        entry_id = store.add(entry(url="http://h/a", req_headers=[("Cookie", "a=zzzz1111")]))

        returned = store.get(entry_id)
        returned.url = "changed"
        returned.req_headers.append(("X", "y"))
        returned.resp_headers.append(("X", "y"))
        returned.req_body = b"changed"
        listed = store.list()[0]
        listed.resp_headers.append(("X", "y"))

        again = store.get(entry_id)
        self.assertEqual(again.url, "http://h/a")
        self.assertEqual(again.req_headers, [("Cookie", "<redacted>")])
        self.assertEqual(again.resp_headers, [])
        self.assertEqual(again.req_body, b"")

    def test_store_truncates_large_bodies(self):
        store = self.store(max_body=10)

        entry_id = store.add(entry(req_body=b"x" * 20, resp_body=b"y" * 5))

        stored = store.get(entry_id)
        self.assertEqual(len(stored.req_body), 10)
        self.assertEqual(len(stored.resp_body), 5)
        self.assertTrue(stored.truncated)

    def test_untruncated_entry_is_not_flagged(self):
        store = self.store(max_body=10)

        self.assertFalse(store.get(store.add(entry(resp_body=b"y" * 10))).truncated)

    def test_secret_past_the_cap_is_still_redacted(self):
        store = self.store(max_body=12)

        entry_id = store.add(entry(
            req_headers=[("Content-Type", "application/x-www-form-urlencoded")],
            req_body=b"a=" + b"x" * 30 + b"&pwd=s3cretvalue", resp_body=b"-" * 30 + b"s3cretvalue"))

        self.assertNotIn(b"s3cret", store.get(entry_id).resp_body)
        self.assertEqual(self.redactor.secret_values, {"s3cretvalue"})

    def test_jsonl_written_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            off = Path(tmp) / "off.jsonl"
            on = Path(tmp) / "sub" / "on.jsonl"
            self.store().add(entry())
            self.store(jsonl_path=on).add(entry(url="http://h/one", resp_body="café".encode()))
            self.store(jsonl_path=on).add(entry(url="http://h/two"))

            self.assertFalse(off.exists())
            lines = on.read_text(encoding="utf-8").splitlines()
            records = [json.loads(line) for line in lines]
            self.assertEqual([r["url"] for r in records], ["http://h/one", "http://h/two"])
            self.assertEqual(records[0]["resp_body"], "café")
            self.assertEqual(records[0]["method"], "GET")

    def test_jsonl_decodes_bodies_with_their_charset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.jsonl"
            self.store(jsonl_path=path).add(entry(
                resp_headers=[("Content-Type", "text/html; charset=iso-8859-1")],
                resp_body="café".encode("iso-8859-1")))

            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["resp_body"], "café")

    def test_jsonl_contains_no_secret_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.jsonl"
            store = self.store(jsonl_path=path)

            store.add(entry(
                method="POST", url="http://h/p?token=abcd1234",
                req_headers=[("Content-Type", "application/x-www-form-urlencoded"),
                             ("Cookie", "SID=cafe1234")],
                req_body=b"code=A1&pwd=s3cret", resp_headers=[("X-Echo", "s3cret")],
                resp_body=b'<dd>s3cret</dd> abcd1234 cafe1234', error="s3cret"))

            text = path.read_text(encoding="utf-8")
            for secret in ("s3cret", "abcd1234", "cafe1234"):
                self.assertNotIn(secret, text)
            self.assertIn("<redacted>", text)

    def test_jsonl_failure_does_not_break_add(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_bytes(b"")
            store = self.store(jsonl_path=blocker / "c.jsonl")  # parent is a file

            self.assertEqual(store.add(entry()), 1)
            self.assertIsNotNone(store.jsonl_error)

    def test_store_is_thread_safe(self):
        store = self.store(max_entries=10000)
        ids = []

        def work():
            local = [store.add(entry(url="http://h/?token=value%d" % i)) for i in range(100)]
            ids.extend(local)
            store.list(limit=5)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(set(ids)), 800)
        self.assertEqual(len(store.list(limit=None)), 800)


if __name__ == "__main__":
    unittest.main()
