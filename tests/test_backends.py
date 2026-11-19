"""Unit tests for ie_mcp.backends. Fakes only: no browser and no real COM.

The COM fakes sit at the level of the raw ``IDispatch`` that ``htmldoc.document_from_hwnd``
returns (``GetIDsOfNames``/``Invoke``/``QueryInterface``), so the whole ComBackend stack above
it runs for real, apart from the page's own JavaScript: ``FakePage.reply`` plays the page.
"""
import dataclasses
import io
import json
import re
import sys
import threading
import types
import unittest
from unittest import mock

from ie_mcp import backends, config, js, protocol
import ie_mcp.tools  # noqa: F401  (importing the package registers every tool)
from ie_mcp.backends import ComBackend, ComElement
from ie_mcp.session import SESSION
from ie_mcp.win32 import htmldoc, messages

# The same constructs tests/test_js_resources.py forbids in the shipped scripts.
FORBIDDEN = ["=>", "`", r"\blet\b", r"\bconst\b", r"JSON\.", r"\.forEach\(", r"\.map\(",
             r"\.filter\(", r"Object\.keys", r"\.trim\(", r"\.apply\(", r"\.call\("]
FUNC_LITERAL = re.compile(r'new Function\(("(?:[^"\\]|\\.)*")\)')
RUN_LITERAL = re.compile(r'var run=("(?:[^"\\]|\\.)*");')
TARGET_AWARE = ["ie_html", "ie_text", "ie_js", "ie_click", "ie_fill", "ie_select", "ie_get",
                "ie_frames", "ie_wait_element", "ie_wait_text", "ie_grid", "ie_scroll",
                "ie_press_key", "ie_hover"]
FAKE_PYTHONCOM = types.SimpleNamespace(DISPATCH_METHOD=1, DISPATCH_PROPERTYGET=2,
                                       IID_IDispatchEx="IID_IDispatchEx")


def run_text(executed):
    """The runner script of a request: the request itself, or the literal a frame request carries."""
    found = RUN_LITERAL.search(executed)
    return json.loads(found.group(1)) if found else executed


def inner_body(executed):
    """The function body the request hands to ``new Function``, decoded from its string literal."""
    found = FUNC_LITERAL.findall(run_text(executed))
    assert len(found) == 1, f"expected exactly one Function literal, found {len(found)}"
    text = json.loads(found[0])
    assert text.endswith("\n"), text
    return text[:-1]


class FakeDispatchEx:
    def __init__(self, window):
        self.window = window

    def GetDispID(self, name, flags):
        assert flags == 1, "expando names are looked up case-sensitively (fdexNameCaseSensitive)"
        if name not in self.window.expandos:
            raise RuntimeError(f"unknown name {name}")
        return name

    def InvokeEx(self, dispid, lcid, flags, args):
        assert flags == FAKE_PYTHONCOM.DISPATCH_PROPERTYGET
        return self.window.expandos[dispid]


class FakeWindow:
    """The raw IDispatch of ``document.parentWindow``."""

    def __init__(self, page):
        self.page = page
        self.expandos = {}
        self.executed = []
        self.clears = 0
        self.fail_clear = False
        self.thread_ids = []

    def GetIDsOfNames(self, name):
        return [name]

    def Invoke(self, dispid, lcid, flags, wanted, *args):
        assert dispid == "execScript" and flags == FAKE_PYTHONCOM.DISPATCH_METHOD
        if args[0] == backends.CLEAR_SCRIPT:
            if self.fail_clear:
                raise RuntimeError("window is gone")
            self.clears += 1
            self.expandos["__iemcp_r"], self.expandos["__iemcp_e"] = None, ""
            return
        self.executed.append(args[0])
        self.thread_ids.append(threading.get_ident())
        self.page.run(self, args[0])

    def QueryInterface(self, iid):
        assert iid == FAKE_PYTHONCOM.IID_IDispatchEx
        return FakeDispatchEx(self)


class FakeDocument:
    """The raw IDispatch of the document: only ``parentWindow`` is read from it."""

    def __init__(self, window):
        self.window = window

    def GetIDsOfNames(self, name):
        return [name]

    def Invoke(self, dispid, lcid, flags, wanted, *args):
        assert dispid == "parentWindow" and flags == FAKE_PYTHONCOM.DISPATCH_PROPERTYGET
        return self.window


class FakePage:
    """Plays the page: ``reply(body, args)`` returns what the script returns, or raises
    ``PageError`` for a script error. ``args`` is the argument list literal as sent."""

    def __init__(self, reply=None, mode=11):
        self.reply = reply or (lambda body, args: "")
        self.mode = mode
        self.window = FakeWindow(self)

    def run(self, window, executed):
        body = inner_body(executed)
        try:
            if body.strip() == backends.DOC_MODE_BODY.strip():
                result = self.mode
            else:
                result = self.reply(body, re.search(r"\bfn\((.*)\);", run_text(executed)).group(1))
            window.expandos["__iemcp_e"] = ""
            window.expandos["__iemcp_r"] = result
        except PageError as exc:
            window.expandos["__iemcp_e"] = str(exc)

    def document(self):
        return FakeDocument(self.window)

    @property
    def executed(self):
        return self.window.executed


class PageError(Exception):
    pass


class BackendTestCase(unittest.TestCase):
    def setUp(self):
        stderr = mock.patch.object(sys, "stderr", io.StringIO())
        stderr.start()
        self.addCleanup(stderr.stop)
        for name in ("MODAL_PROBE", "CALL_CONTEXTS"):
            self.addCleanup(setattr, protocol, name, getattr(protocol, name))
        protocol.MODAL_PROBE = lambda: []
        protocol.CALL_CONTEXTS = []  # fakes need no COM apartment
        for attr in ("DEFAULT_TARGET", "ATTACHED_CHAINS", "TAB_ANCESTOR_CLASS"):
            self.addCleanup(setattr, backends, attr, getattr(backends, attr))
        original = config.SETTINGS
        config.SETTINGS = dataclasses.replace(original, tool_timeout=5.0)
        self.addCleanup(setattr, config, "SETTINGS", original)
        self.page = FakePage()
        self.docs = mock.Mock(side_effect=lambda hwnd: self.page.document())
        for patcher in (
            mock.patch.object(htmldoc, "document_from_hwnd", self.docs),
            mock.patch.object(htmldoc, "ie_server_child", return_value=11),
            mock.patch.object(backends, "_pythoncom", return_value=FAKE_PYTHONCOM),
            # The fake hwnds are no real windows; session membership is tested in
            # tests/test_attach.py (SessionTargetTests).
            mock.patch.object(backends, "check_target", lambda kind, hwnd: None),
            # A COM test that reaches the driver would start a real browser: fail instead.
            mock.patch.object(SESSION, "ensure",
                              side_effect=AssertionError("the test started a browser")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def com(self, hwnd=1, kind="hwnd"):
        return ComBackend(hwnd, kind)

    def call(self, name, **arguments):
        response = protocol.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}})
        return response["result"]

    def text_of(self, result):
        return result["content"][0]["text"]


class TargetTests(BackendTestCase):
    def test_resolve_backend_default_is_driver(self):
        self.assertIsInstance(backends.resolve_backend(None), backends.DriverBackend)
        self.assertEqual(backends.target_kind(None), "driver")
        self.assertEqual(backends.target_kind("driver"), "driver")

    def test_resolve_backend_parses_hwnd_target(self):
        backend = backends.resolve_backend("hwnd:42")
        self.assertIsInstance(backend, ComBackend)
        self.assertEqual((backend.hwnd, backend.kind), (42, "hwnd"))
        self.assertEqual(backends.target_kind("hwnd:42"), "hwnd")

    def test_resolve_backend_parses_tab_target(self):
        backend = backends.resolve_backend("tab:7")
        self.assertEqual((backend.hwnd, backend.kind), (7, "tab"))
        self.assertEqual(backends.target_kind("tab:7"), "tab")

    def test_unknown_target_rejected(self):
        for bad in ("frame:1", "hwnd:", "hwnd:abc", "hwnd:0", "tab:-1", "HWND:1x", 5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                backends.target_kind(bad)
        with self.assertRaises(ValueError):
            backends.resolve_backend("frame:1")

    def test_default_target_applies_when_no_target_is_given(self):
        backends.DEFAULT_TARGET = "hwnd:9"
        self.assertEqual(backends.target_kind(None), "hwnd")
        self.assertEqual(backends.target_kind(""), "hwnd")
        self.assertEqual(backends.resolve_backend(None).hwnd, 9)
        self.assertEqual(backends.target_kind("driver"), "driver")  # explicit wins
        self.assertEqual(backends.target_kind("tab:3"), "tab")

    def test_needs_driver_false_for_hwnd_target(self):
        for name in TARGET_AWARE:
            spec = protocol.REGISTRY[name]
            self.assertFalse(spec.needs_driver({"target": "hwnd:1"}), name)
            self.assertFalse(spec.needs_driver({"target": "tab:1"}), name)
            self.assertTrue(spec.needs_driver({}), name)
            self.assertTrue(spec.needs_driver({"target": "driver"}), name)
            self.assertIn("target", spec.input_schema["properties"], name)

    def test_tool_with_unknown_target_reports_an_error(self):
        result = self.call("ie_text", target="frame:1")
        self.assertTrue(result.get("isError"))
        self.assertIn("target", self.text_of(result))

    def test_other_tools_are_not_target_aware(self):
        # ie_screenshot gained its own hwnd-only target in Task 14 (tests/test_capture.py).
        for name in ("ie_open", "ie_wait_ready", "ie_upload"):
            self.assertNotIn("target", protocol.REGISTRY[name].input_schema["properties"], name)


class ServerHwndTests(BackendTestCase):
    def test_hwnd_target_uses_the_ie_server_child(self):
        with mock.patch.object(htmldoc, "ie_server_child", return_value=77) as child:
            self.assertEqual(self.com(5).server_hwnd(), 77)
        child.assert_called_once_with(5)

    def test_hwnd_target_without_an_ie_server_window_fails(self):
        with mock.patch.object(htmldoc, "ie_server_child", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Internet Explorer_Server"):
                self.com(5).server_hwnd()

    def test_tab_target_uses_its_own_hwnd_while_it_is_the_ie_server(self):
        with mock.patch.object(htmldoc, "window_class", return_value=htmldoc.IE_SERVER_CLASS):
            self.assertEqual(self.com(5, "tab").server_hwnd(), 5)

    def test_tab_target_follows_new_ie_server(self):
        # D15 names no ancestor class (the server hwnd survives navigation), so the lookup by
        # ancestor is switched off in production; here it is switched on, with the class of 6.
        backends.TAB_ANCESTOR_CLASS = "TabWindow"
        backends.ATTACHED_CHAINS = {5: [6, 7]}  # 7 is the top window
        classes = {5: "", 6: "TabWindow", 7: "Top", 8: htmldoc.IE_SERVER_CLASS}
        with mock.patch.object(htmldoc, "window_class", side_effect=lambda h: classes.get(h, "")), \
                mock.patch.object(htmldoc, "ie_server_descendants", return_value=[8]) as servers:
            self.assertEqual(self.com(5, "tab").server_hwnd(), 8)
        servers.assert_called_once_with(6)

    def test_tab_target_never_uses_top_window(self):
        backends.TAB_ANCESTOR_CLASS = "TabWindow"
        backends.ATTACHED_CHAINS = {5: [6, 7]}
        # 6 is gone as well; the top window 7 has two IE servers and must not be used.
        classes = {5: "", 6: "", 7: "Top"}
        with mock.patch.object(htmldoc, "window_class", side_effect=lambda h: classes.get(h, "")), \
                mock.patch.object(htmldoc, "ie_server_descendants", return_value=[8, 9]):
            with self.assertRaisesRegex(RuntimeError, "ie_attach"):
                self.com(5, "tab").server_hwnd()
        # 6 is live but holds two IE servers (one visible, one hidden): ambiguous, so an error.
        classes[6] = "TabWindow"
        with mock.patch.object(htmldoc, "window_class", side_effect=lambda h: classes.get(h, "")), \
                mock.patch.object(htmldoc, "ie_server_descendants", return_value=[8, 9]):
            with self.assertRaisesRegex(RuntimeError, "ie_attach"):
                self.com(5, "tab").server_hwnd()

    def test_tab_target_without_ancestor_class_never_falls_back(self):
        self.assertIsNone(backends.TAB_ANCESTOR_CLASS)  # D15 = ie-server
        backends.ATTACHED_CHAINS = {5: [6, 7]}
        with mock.patch.object(htmldoc, "window_class", return_value=""), \
                mock.patch.object(htmldoc, "ie_server_descendants", return_value=[8]):
            with self.assertRaisesRegex(RuntimeError, "the attached tab is gone"):
                self.com(5, "tab").server_hwnd()


class DocumentLifetimeTests(BackendTestCase):
    def test_com_document_obtained_once_per_call(self):
        backend = self.com()
        backend.text(None)
        backend.html(None)
        self.assertEqual(self.docs.call_count, 1)
        self.docs.assert_called_once_with(11)

    def test_refresh_drops_the_cached_document(self):
        backend = self.com()
        backend.text(None)
        backend.refresh()
        backend.text(None)
        self.assertEqual(self.docs.call_count, 2)

    def test_com_backend_refuses_another_thread(self):
        backend = self.com()
        backend.text(None)
        errors = []

        def other():
            try:
                backend.text(None)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertIn("thread", str(errors[0]))

    def test_com_calls_go_through_com_call(self):
        calls = []
        real = htmldoc.com_call

        def spy(fn, *args):
            calls.append(getattr(fn, "__name__", repr(fn)))
            return real(fn, *args)

        with mock.patch.object(htmldoc, "com_call", side_effect=spy):
            self.com().text(None)
        # get parentWindow (name lookup + read), execScript (lookup + call), expando read-back.
        self.assertGreaterEqual(len(calls), 4)

    def test_rejected_calls_are_retried(self):
        class Rejected(Exception):
            hresult = htmldoc.RPC_E_CALL_REJECTED

        window = self.page.window
        original = window.Invoke
        state = {"n": 0}

        def flaky(*args):
            state["n"] += 1
            if state["n"] == 1:
                raise Rejected()
            return original(*args)

        window.Invoke = flaky
        with mock.patch.object(htmldoc.time, "sleep"):
            self.com().text(None)
        self.assertGreaterEqual(state["n"], 2)


class WaitTests(BackendTestCase):
    def replies(self, *texts):
        """Each document the fake hands out answers with the next text."""
        pages = []
        for text in texts:
            page = FakePage(lambda body, args, text=text: "" if "tagName" in body else text)
            pages.append(page)
        state = {"n": 0}

        def next_doc(hwnd):
            page = pages[min(state["n"], len(pages) - 1)]
            state["n"] += 1
            return page.document()

        self.docs.side_effect = next_doc

    def test_com_wait_reobtains_document_each_poll(self):
        self.replies("nothing yet", "Echo: A1")
        result = self.call("ie_wait_text", target="hwnd:1", contains="Echo",
                           timeout=5, interval=0.01)
        self.assertFalse(result.get("isError"), result)
        self.assertTrue(json.loads(self.text_of(result))["ok"])
        self.assertGreaterEqual(self.docs.call_count, 2)

    def test_com_wait_survives_brief_resolution_failure(self):
        self.replies("Echo: A1")
        calls = []

        def flaky(hwnd):
            calls.append(hwnd)
            return None if len(calls) == 1 else 11  # fails once, then resolves

        with mock.patch.object(htmldoc, "ie_server_child", side_effect=flaky):
            result = self.call("ie_wait_text", target="hwnd:1", contains="Echo",
                               timeout=5, interval=0.01)
        self.assertFalse(result.get("isError"), result)
        self.assertTrue(json.loads(self.text_of(result))["ok"])
        self.assertGreaterEqual(len(calls), 2)

    def test_com_wait_with_a_frame_reports_why_it_could_not_read(self):
        # With an explicit frame every read failure used to be reduced to a generic message.
        with mock.patch.object(htmldoc, "ie_server_child", return_value=None):
            result = self.call("ie_wait_text", target="hwnd:1", contains="Echo", frame="x",
                               timeout=0.05, interval=0.01)
        report = json.loads(self.text_of(result))
        self.assertTrue(report["timed_out"])
        self.assertIn("Internet Explorer_Server", report["last_preview"])

    def test_com_wait_with_a_frame_reports_a_frame_error(self):
        def denied(body, args):
            raise PageError('frame "foreign" cannot be used: Access is denied.')

        self.page.reply = denied
        result = self.call("ie_wait_text", target="hwnd:1", contains="Echo", frame="foreign",
                           timeout=0.05, interval=0.01)
        self.assertIn('frame "foreign"', json.loads(self.text_of(result))["last_preview"])

    def test_wait_element_usage_errors_are_not_retried(self):
        with mock.patch.object(SESSION, "pause", side_effect=AssertionError("polled again")):
            xpath = self.call("ie_wait_element", target="hwnd:1", by="xpath", value="//a",
                              timeout=30, interval=0.01)
            self.assertTrue(xpath.get("isError"), xpath)
            self.assertIn("xpath is not supported", self.text_of(xpath))
            self.page.mode = 7
            css = self.call("ie_wait_element", target="hwnd:1", by="css", value="#a",
                            timeout=30, interval=0.01)
            self.assertTrue(css.get("isError"), css)
            self.assertIn("documentMode", self.text_of(css))
            with self.assertRaises(ValueError):
                self.com().find("xpath", "//a", None, 30)

    def test_wait_element_retries_while_there_is_no_document(self):
        with mock.patch.object(htmldoc, "ie_server_child", return_value=None):
            result = self.call("ie_wait_element", target="hwnd:1", by="id", value="a",
                               timeout=0.05, interval=0.01)
        report = json.loads(self.text_of(result))
        self.assertTrue(report["timed_out"])
        self.assertIn("Internet Explorer_Server", report["last_error"])

    def test_driver_not_found_is_one_class_for_the_tools(self):
        from selenium.common.exceptions import NoSuchElementException
        with mock.patch.object(SESSION, "find",
                               side_effect=NoSuchElementException("no element id='a'")),                 mock.patch.object(SESSION, "ensure", return_value=None):
            with self.assertRaises(backends.NoSuchElement):
                backends.DriverBackend().find("id", "a", None, 0)
            # The tool treats it as "not yet" and times out cleanly.
            result = self.call("ie_wait_element", by="id", value="a", timeout=0.05, interval=0.01)
        report = json.loads(self.text_of(result))
        self.assertTrue(report["timed_out"])
        self.assertNotIn("last_error", report)

    def test_com_wait_reports_the_last_error_on_timeout(self):
        with mock.patch.object(htmldoc, "ie_server_child", return_value=None):
            result = self.call("ie_wait_text", target="hwnd:1", contains="Echo",
                               timeout=0.05, interval=0.01)
        report = json.loads(self.text_of(result))
        self.assertFalse(report["ok"])
        self.assertTrue(report["timed_out"])
        self.assertIn("Internet Explorer_Server", report["last_preview"])

    def test_com_wait_element_present_and_gone(self):
        self.page.reply = lambda body, args: "1" if "iemcpFind" in body else ""
        result = self.call("ie_wait_element", target="hwnd:1", by="id", value="echo",
                           timeout=1, interval=0.01)
        self.assertTrue(json.loads(self.text_of(result))["ok"])
        self.page.reply = lambda body, args: ""
        result = self.call("ie_wait_element", target="hwnd:1", by="id", value="echo",
                           timeout=0.05, interval=0.01)
        report = json.loads(self.text_of(result))
        self.assertFalse(report["ok"])
        self.assertTrue(report["timed_out"])
        result = self.call("ie_wait_element", target="hwnd:1", by="id", value="echo",
                           gone=True, timeout=1, interval=0.01)
        self.assertTrue(json.loads(self.text_of(result))["ok"])

    def test_com_find_with_a_timeout_refreshes_before_each_poll(self):
        answers = iter(["", "", "1"])
        self.page.reply = lambda body, args: next(answers)
        element = self.com().find("id", "late", None, 5, interval=0.01)
        self.assertEqual(element, ComElement(by="id", value="late", frame=None))
        self.assertEqual(self.docs.call_count, 3)  # first poll uses the call's document


class LocatorTests(BackendTestCase):
    def test_com_find_by_id(self):
        self.page.reply = lambda body, args: "1"
        element = self.com().find("id", "code", None, 0)
        self.assertEqual(element, ComElement(by="id", value="code", frame=None))
        executed = self.page.executed[-1]
        self.assertIn("iemcpFind", inner_body(executed))
        self.assertIn('"id","code"', run_text(executed))  # the locator travels as argument literals

    def test_com_find_missing_element_raises(self):
        self.page.reply = lambda body, args: ""
        with self.assertRaises(backends.NoSuchElement) as ctx:
            self.com().find("name", "nope", None, 0)
        self.assertIn("nope", str(ctx.exception))

    def test_com_find_remembers_the_frame(self):
        self.page.reply = lambda body, args: "1"
        element = self.com().find("tag", "input", "main/inner", 0)
        self.assertEqual(element.frame, "main/inner")

    def test_com_xpath_rejected(self):
        with self.assertRaisesRegex(ValueError, "xpath is not supported for COM targets"):
            self.com().find("xpath", "//a", None, 0)

    def test_com_unknown_locator_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown 'by'"):
            self.com().find("bogus", "x", None, 0)

    def test_com_css_requires_document_mode_8(self):
        self.page.mode = 7
        with self.assertRaisesRegex(ValueError, "documentMode"):
            self.com().find("css", "#code", None, 0)

    def test_com_css_allowed_from_document_mode_8(self):
        self.page.mode = 11
        self.page.reply = lambda body, args: "1"
        self.assertEqual(self.com().find("css", "#code", None, 0).by, "css")


class ScriptTests(BackendTestCase):
    def test_com_js_embeds_primitive_args(self):
        self.page.reply = lambda body, args: "a2"
        self.assertEqual(self.com().js("return arguments[0]+arguments[1]", None, ["a", 2]), "a2")
        executed = self.page.executed[-1]
        self.assertIn('"a"', executed)
        self.assertIn(",2)", executed)
        self.assertEqual(inner_body(executed), "return arguments[0]+arguments[1]")

    def test_com_js_embeds_every_primitive_kind(self):
        self.com().js("return 1", None, ["x", 3, 1.5, True, False, None])
        self.assertIn('fn("x",3,1.5,true,false,null);', self.page.executed[-1])

    def test_com_js_rejects_element_args(self):
        element = ComElement(by="id", value="x", frame=None)
        for bad in (element, object(), [1], {"a": 1}, b"x"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.com().js("return 1", None, [bad])
        self.assertEqual(self.page.executed, [])  # nothing reached the page

    def test_com_js_error_raises(self):
        def fail(body, args):
            raise PageError("boom")

        self.page.reply = fail
        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.com().js("throw new Error('boom')", None, [])

    def test_com_js_embeds_script_as_literal(self):
        script = 'var s = "q\\" \'x\'"; // </script> \\ end '
        self.com().js(script, None, [])
        executed = self.page.executed[-1]
        self.assertNotIn("</script>", executed)
        self.assertNotIn(" ", executed)
        self.assertEqual(len(FUNC_LITERAL.findall(executed)), 1)
        self.assertEqual(inner_body(executed), script)

    def test_com_js_result_is_read_back_whatever_its_type(self):
        for value in (11, 1.5, True, None, "text", "a\x1eb"):
            self.page.reply = lambda body, args, value=value: value
            self.assertEqual(self.com().js("return 1", None, []), value)

    def test_com_request_clears_its_expandos_afterwards(self):
        # __iemcp_r can hold page text or a field value: it must not stay readable by the page.
        self.page.reply = lambda body, args: "secret page text"
        window = self.page.window
        self.assertEqual(self.com().text(None), "secret page text")
        self.assertEqual(window.clears, 1)
        self.assertEqual((window.expandos["__iemcp_r"], window.expandos["__iemcp_e"]), (None, ""))
        # ... also when the script failed, and a clear that fails does not fail the call.
        def boom(body, args):
            raise PageError("boom")

        self.page.reply = boom
        with self.assertRaisesRegex(RuntimeError, "boom"):
            self.com().text(None)
        self.assertEqual(window.clears, 2)
        window.fail_clear = True
        self.page.reply = lambda body, args: "still works"
        self.assertEqual(self.com().text(None), "still works")

    def test_com_js_whole_numbers_read_back_as_integers(self):
        # JScript hands numbers over as doubles; documentMode 11 must not show up as 11.0.
        self.page.reply = lambda body, args: 11.0
        result = self.com().js("return document.documentMode", None, [])
        self.assertEqual(result, 11)
        self.assertIsInstance(result, int)

    def test_com_wrapper_leaves_no_globals_and_only_the_two_expandos(self):
        self.com().js("return 1", None, [])
        executed = self.page.executed[-1]
        self.assertTrue(executed.lstrip().startswith("(function(){"))
        self.assertTrue(executed.rstrip().endswith("})();"))
        self.assertEqual(sorted(set(re.findall(r"\.(__\w+)", executed))),
                         ["__iemcp_e", "__iemcp_r"])
        self.com().js("return 1", "a/b", [])  # the same holds for a frame request
        framed = self.page.executed[-1]
        self.assertEqual(sorted(set(re.findall(r"\.(__\w+)", framed + run_text(framed)))),
                         ["__iemcp_e", "__iemcp_r"])

    def test_com_scripts_are_es3(self):
        element = ComElement(by="id", value="x", frame="a/0")
        backend = self.com()
        self.page.reply = lambda body, args: "OK:B" if "selectedIndex" in body else ""
        backend.text("a")
        backend.html("a")
        backend.frames()
        backend.grid("a", "t", None, 10)
        backend.js("return 1", "a", [1])
        backend.click(element, "click", "async")
        backend.click(element, "double", "driver")
        backend.click(element, "right", "driver")
        backend.fill(element, "t", True)
        backend.select(element, "label", "B")
        backend.get(element, "value")
        backend.hover(element)
        backend.scroll("a", element, None, 0, 0)
        backend.scroll(None, None, "bottom", 0, 0)
        backend.scroll(None, None, None, 5, 6)
        with mock.patch.object(backends, "_post_message"), \
                mock.patch.object(backends, "_scan_code", return_value=1), \
                mock.patch.object(backends, "_char_vk", return_value=0x58):
            backend.press_key(element, "x", "a")
        self.page.mode = 5
        backend.press_key(element, "Enter", "a")
        self.assertGreater(len(self.page.executed), 15)
        for sent in self.page.executed:
            # A frame request carries the runner (and the body) as string literals: lint both
            # the text as sent and the texts decoded from it.
            texts = [sent, run_text(sent), inner_body(sent)]
            for executed in texts:
                for pattern in FORBIDDEN:
                    self.assertIsNone(re.search(pattern, executed), (pattern, executed[:200]))
                self.assertNotIn("\x1e", executed)
                self.assertNotIn("\x1f", executed)
                for line in executed.splitlines():
                    if "querySelector" in line:
                        self.assertIn("documentMode", line)

    def test_js_literal_escapes_everything_that_could_break_out(self):
        literal = backends.js_literal('a"b\\c\n</script>  \x00é\U0001F600')
        self.assertNotIn("</", literal)
        self.assertNotIn("\n", literal)
        self.assertTrue(literal.isascii())
        decoded = json.loads(literal.replace("<\\/", "</"))
        self.assertEqual(decoded, 'a"b\\c\n</script>  \x00é\U0001F600')

    def test_js_literal_primitives(self):
        self.assertEqual(backends.js_literal(None), "null")
        self.assertEqual(backends.js_literal(True), "true")
        self.assertEqual(backends.js_literal(False), "false")
        self.assertEqual(backends.js_literal(7), "7")
        self.assertEqual(backends.js_literal(2.5), "2.5")
        for bad in (float("nan"), float("inf"), [], {}, object()):
            with self.assertRaises(ValueError):
                backends.js_literal(bad)

    def test_frame_path_forms(self):
        for frame, expected in ((None, []), ("", []), (3, [3]), ("3", [3]), ("main", ["main"]),
                                ("main/inner", ["main", "inner"]), ("3/0", [3, 0]),
                                (["a", 1], ["a", 1]), (("a", "2"), ["a", 2])):
            self.com().js("return 1", frame, [])
            sent = self.page.executed[-1]
            if not expected:
                self.assertNotIn("path=", sent)  # no frame: the runner goes straight to the window
                self.assertIn("target = window,", sent)
            else:
                self.assertIn(f"path={json.dumps(expected, separators=(',', ':'))};", sent, frame)
                self.assertIn("target = window.top,", run_text(sent))  # a frame reports to the top window

    def test_frame_error_names_the_frame(self):
        # The page-side message (a cross-origin frame denies access) reaches the caller as is.
        def denied(body, args):
            raise PageError('frame "foreign" cannot be used: Access is denied.')

        self.page.reply = denied
        with self.assertRaisesRegex(RuntimeError, 'frame "foreign"'):
            self.com().text("foreign")
        self.assertIn("catch", self.page.executed[-1])
        self.assertIn("cannot be used", js.load("com_frame.js"))
        self.assertIn("execScript", self.page.executed[-1])


class OperationTests(BackendTestCase):
    def setUp(self):
        super().setUp()
        self.element = ComElement(by="id", value="code", frame="main")

    def last_body(self):
        return inner_body(self.page.executed[-1])

    def test_com_text_and_html(self):
        self.page.reply = lambda body, args: "Hello" if "innerText" in body else "<html></html>"
        self.assertEqual(self.com().text(None), "Hello")
        self.assertEqual(self.com().html(None), "<html></html>")
        self.page.reply = lambda body, args: None
        self.assertEqual(self.com().text(None), "")

    def test_com_frames_parses_the_frames_script(self):
        self.page.reply = lambda body, args: "0\x1fmenu\x1fmenu\x1fmenu.htm\x1e1\x1fmain\x1f\x1fform.htm"
        self.assertEqual(self.com().frames(), [
            {"index": 0, "name": "menu", "id": "menu", "src": "menu.htm"},
            {"index": 1, "name": "main", "id": "", "src": "form.htm"}])
        self.page.reply = lambda body, args: ""
        self.assertEqual(self.com().frames(), [])

    def test_com_grid_runs_grid_js_and_builds_the_result(self):
        self.page.reply = lambda body, args: "Code\x1fName\x1eA1\x1fAlpha\x1eB2\x1fBeta"
        grid = self.com().grid(None, "grid", None, 500)
        self.assertEqual(grid["headers"], ["Code", "Name"])
        self.assertEqual(grid["rows"], [{"Code": "A1", "Name": "Alpha"},
                                        {"Code": "B2", "Name": "Beta"}])
        self.assertIn('"grid"', run_text(self.page.executed[-1]))
        self.assertIn("getElementsByTagName('table')", self.last_body())

    def test_com_grid_without_a_table(self):
        self.page.reply = lambda body, args: ""
        self.assertEqual(self.com().grid(None, None, None, 500), {"rows": 0, "note": "no table found"})

    def test_com_get_reads_text_value_or_attribute(self):
        self.page.reply = lambda body, args: "A1"
        backend = self.com()
        self.assertEqual(backend.get(self.element, "text"), "A1")
        self.assertIn("innerText", self.last_body())
        backend.get(self.element, "value")
        self.assertIn(".value", self.last_body())
        backend.get(self.element, "href")
        self.assertIn("getAttribute", self.last_body())
        self.assertIn('"href"', run_text(self.page.executed[-1]))
        self.assertIn('"id","code"', run_text(self.page.executed[-1]))
        self.assertIn('path=["main"];', self.page.executed[-1])  # the element's frame

    def test_com_get_missing_element_raises(self):
        def missing(body, args):
            raise PageError("element not found: id=code")

        self.page.reply = missing
        with self.assertRaisesRegex(RuntimeError, "element not found"):
            self.com().get(self.element, "text")

    def test_com_click_is_queued_on_a_timer(self):
        self.com().click(self.element, "click", "driver")
        self.assertIn("setTimeout", self.last_body())
        self.assertNotIn("ondblclick", self.last_body())

    def test_com_double_and_right_click_fire_events(self):
        self.com().click(self.element, "double", "driver")
        self.assertIn("'ondblclick'", self.last_body())
        self.com().click(self.element, "right", "driver")
        self.assertIn("'oncontextmenu'", self.last_body())

    def test_com_click_that_closes_its_window_is_not_an_error(self):
        # The queued click closes the dialog before the caller reads the result back.
        def gone(window, name):
            raise RuntimeError("COM call failed (read __iemcp_e): Member not found.")

        with mock.patch.object(backends, "_com_read", gone), \
                mock.patch.object(messages, "is_window", return_value=False):
            self.com().click(self.element, "click", "driver")  # no error
            with self.assertRaises(backends.WindowClosed):
                self.com().get(self.element, "text")  # a read has nothing to return

    def test_com_failed_read_of_an_open_window_still_says_the_page_did_not_run_it(self):
        def failing(window, name):
            raise RuntimeError("Member not found.")

        with mock.patch.object(backends, "_com_read", failing), \
                mock.patch.object(messages, "is_window", return_value=True), \
                mock.patch.object(backends.ComBackend, "_window_closed", lambda s_, wait=0.5: False):
            with self.assertRaisesRegex(RuntimeError, "did not run the script"):
                self.com().click(self.element, "click", "driver")

    def test_com_click_rejects_unknown_actions(self):
        with self.assertRaises(ValueError):
            self.com().click(self.element, "middle", "driver")

    def test_com_fill_fires_event_sequence(self):
        self.com().fill(self.element, "A1", True)
        body = self.last_body()
        events = ['onkeydown', 'onkeypress', 'onkeyup', 'onchange', 'onblur']
        self.assertIn("[" + ",".join(f"'{e}'" for e in events) + "]", body)
        self.assertIn("focus()", body)
        self.assertIn('"A1"', run_text(self.page.executed[-1]))

    def test_com_fill_and_hover_report_events_that_could_not_be_fired(self):
        self.page.reply = lambda body, args: "EVENTS:onchange,onblur"
        with self.assertRaisesRegex(RuntimeError, "onchange,onblur.*value was set"):
            self.com().fill(self.element, "A1", True)
        with self.assertRaisesRegex(RuntimeError, "hover.*onchange,onblur"):
            self.com().hover(self.element)
        self.page.reply = lambda body, args: "ok"
        self.com().fill(self.element, "A1", True)
        self.com().hover(self.element)

    def test_com_fill_without_clear_appends(self):
        self.com().fill(self.element, "x", False)
        self.assertIn(".value + ", self.last_body())
        self.assertIn('"x",false)', run_text(self.page.executed[-1]))

    def test_com_select_returns_the_selected_text(self):
        self.page.reply = lambda body, args: "OK:B"
        self.assertEqual(self.com().select(self.element, "label", "B"), "B")
        self.assertIn("sel.selectedIndex=idx", self.last_body())  # select.js, shared with the driver

    def test_com_select_not_found(self):
        self.page.reply = lambda body, args: "NOTFOUND"
        with self.assertRaises(backends.OptionNotFound):
            self.com().select(self.element, "label", "Z")

    def test_com_hover_fires_mouseover_then_mouseenter(self):
        self.com().hover(self.element)
        body = self.last_body()
        self.assertLess(body.index("'onmouseover'"), body.index("'onmouseenter'"))

    def test_com_scroll_forms(self):
        backend = self.com()
        self.assertEqual(backend.scroll("main", self.element, None, 0, 0), "scrolled into view")
        self.assertIn("scrollIntoView", self.last_body())
        self.assertEqual(backend.scroll(None, None, "top", 0, 0), "scrolled page to top")
        self.assertIn("scrollTo", self.last_body())
        self.assertEqual(backend.scroll(None, self.element, "bottom", 0, 0),
                         "scrolled container to bottom")
        self.assertIn("scrollHeight", self.last_body())
        self.assertEqual(backend.scroll(None, None, None, 5, -6), "scrolled by (5,-6)")
        self.assertIn("scrollBy", self.last_body())


class PressKeyTests(BackendTestCase):
    def setUp(self):
        super().setUp()
        self.posted = []
        for patcher in (
            mock.patch.object(backends, "_post_message", side_effect=lambda *a: self.posted.append(a)),
            mock.patch.object(backends, "_scan_code", return_value=0x2D),
            mock.patch.object(backends, "_char_vk", return_value=0x58),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_com_press_key_uses_d7_path(self):
        # D7: posted WM_KEYDOWN then WM_KEYUP (no WM_CHAR) to the IE server window, in the
        # document modes it measured (7 and 11); lParam as the spike built it.
        for mode in (7, 11):
            self.posted.clear()
            self.page.mode = mode
            self.com().press_key(None, "x", None)
            down = 1 | (0x2D << 16)
            up = down | (1 << 30) | (1 << 31)
            self.assertEqual(self.posted, [(11, 0x100, 0x58, down), (11, 0x101, 0x58, up)], mode)

    def test_extended_keys_set_bit_24_of_lparam(self):
        base = 1 | (0x2D << 16)
        for name, vk in (("Left", 0x25), ("Up", 0x26), ("Right", 0x27), ("Down", 0x28),
                         ("Home", 0x24), ("End", 0x23), ("PageUp", 0x21), ("PageDown", 0x22),
                         ("Insert", 0x2D), ("Delete", 0x2E)):
            self.posted.clear()
            self.com().press_key(None, name, None)
            down, up = self.posted[0][3], self.posted[1][3]
            self.assertEqual(down, base | (1 << 24), name)
            self.assertEqual(up, base | (1 << 24) | (1 << 30) | (1 << 31), name)
        for name in ("Enter", "Tab", "Escape", "Space", "Backspace", "F5", "x"):
            self.posted.clear()
            self.com().press_key(None, name, None)
            self.assertEqual(self.posted[0][3], base, name)  # not an extended key

    def test_com_press_key_old_modes_use_fire_event(self):
        self.page.mode = 5
        self.com().press_key(None, "Enter", None)
        self.assertEqual(self.posted, [])
        body = inner_body(self.page.executed[-1])
        self.assertIn("fireEvent", body)
        for name in ("onkeydown", "onkeypress", "onkeyup"):
            self.assertIn(name, body)
        self.assertIn("13", self.page.executed[-1])  # the key code of Enter

    def test_com_press_key_focuses_the_element_first(self):
        element = ComElement(by="id", value="typed", frame=None)
        self.com().press_key(element, "Tab", None)
        first = inner_body(self.page.executed[0])
        self.assertIn("focus()", first)
        self.assertEqual(len(self.posted), 2)

    def test_com_press_key_named_keys(self):
        for name, vk in (("Enter", 0x0D), ("tab", 0x09), ("Escape", 0x1B), ("space", 0x20),
                         ("Backspace", 0x08), ("Delete", 0x2E), ("Left", 0x25), ("Up", 0x26),
                         ("Right", 0x27), ("Down", 0x28), ("Home", 0x24), ("End", 0x23),
                         ("PageUp", 0x21), ("PageDown", 0x22), ("F5", 0x74)):
            self.posted.clear()
            self.com().press_key(None, name, None)
            self.assertEqual(self.posted[0][2], vk, name)

    def test_com_press_key_rejects_unknown_names(self):
        with self.assertRaises(ValueError):
            self.com().press_key(None, "Hyper", None)
        self.assertEqual(self.posted, [])


class ToolRoutingTests(BackendTestCase):
    """The tools reach the COM backend only through the target argument and need no driver."""

    def test_ie_text_over_com_needs_no_session(self):
        self.page.reply = lambda body, args: "Hello legacy"
        with mock.patch.object(SESSION, "ensure", side_effect=AssertionError("driver used")):
            result = self.call("ie_text", target="hwnd:1")
        self.assertEqual(self.text_of(result), "Hello legacy")

    def test_ie_js_over_com_returns_json(self):
        self.page.reply = lambda body, args: 11
        with mock.patch.object(htmldoc, "window_class", return_value=htmldoc.IE_SERVER_CLASS):
            result = self.call("ie_js", target="tab:1", script="return document.documentMode")
        self.assertEqual(self.text_of(result), "11")

    def test_ie_js_syntax_error_is_an_error_result(self):
        def syntax(body, args):
            raise PageError("Syntax error")

        self.page.reply = syntax
        result = self.call("ie_js", target="hwnd:1", script="return (")
        self.assertTrue(result.get("isError"))
        self.assertIn("Syntax error", self.text_of(result))

    def test_ie_fill_and_get_over_com(self):
        self.page.reply = lambda body, args: "A1" if ".value;" in body else "1"
        self.assertIn("filled", self.text_of(self.call(
            "ie_fill", target="hwnd:1", by="id", value="code", text="A1")))
        self.assertEqual(self.text_of(self.call(
            "ie_get", target="hwnd:1", by="id", value="code", attr="value")), "A1")

    def test_ie_select_not_found_names_the_element(self):
        self.page.reply = lambda body, args: "NOTFOUND" if "selectedIndex" in body else "1"
        result = self.call("ie_select", target="hwnd:1", by="id", value="kind", label="Z", wait=0)
        self.assertTrue(result.get("isError"))
        self.assertIn("not found in id=kind", self.text_of(result))

    def test_ie_click_over_com_reports_the_queued_click(self):
        self.page.reply = lambda body, args: "1"
        result = self.call("ie_click", target="hwnd:1", by="id", value="send", wait=0)
        self.assertIn("queued", self.text_of(result))

    def test_ie_frames_over_com(self):
        self.page.reply = lambda body, args: "0\x1fa\x1fa\x1fa.htm"
        result = self.call("ie_frames", target="hwnd:1")
        self.assertEqual(json.loads(self.text_of(result)),
                         [{"index": 0, "name": "a", "id": "a", "src": "a.htm"}])

    def test_ie_grid_over_com(self):
        self.page.reply = lambda body, args: "H1\x1fH2\x1ea\x1fb"
        result = self.call("ie_grid", target="hwnd:1")
        self.assertEqual(json.loads(self.text_of(result))["rows"], [{"H1": "a", "H2": "b"}])

    def test_ie_press_key_over_com(self):
        with mock.patch.object(backends, "_post_message"), \
                mock.patch.object(backends, "_scan_code", return_value=1):
            result = self.call("ie_press_key", target="hwnd:1", key="Enter", wait=0)
        self.assertIn("pressed Enter", self.text_of(result))

    def test_ie_scroll_and_hover_over_com(self):
        self.page.reply = lambda body, args: "1"
        self.assertIn("scrolled", self.text_of(self.call(
            "ie_scroll", target="hwnd:1", to="bottom")))
        self.assertIn("hovered", self.text_of(self.call(
            "ie_hover", target="hwnd:1", by="id", value="x", wait=0)))

    def test_com_click_defers_its_effect_like_an_async_driver_click(self):
        # POST_HOOKS must not run before a queued click has landed: a COM plain click is always
        # queued, whatever the click mode; double/right fire at once; the driver target keeps
        # its own rule (async mode only).
        deferred = protocol.REGISTRY["ie_click"].defers_effect
        self.assertTrue(deferred({"target": "hwnd:1"}))
        self.assertTrue(deferred({"target": "tab:1", "action": "click", "mode": "driver"}))
        self.assertFalse(deferred({"target": "hwnd:1", "action": "double"}))
        self.assertFalse(deferred({"target": "hwnd:1", "action": "right"}))
        self.assertFalse(deferred({}))
        self.assertFalse(deferred({"target": "driver", "mode": "driver"}))
        self.assertTrue(deferred({"mode": "async"}))
        self.assertTrue(deferred({"target": "driver", "mode": "async"}))
        self.assertFalse(deferred({"mode": "async", "action": "double"}))

    def test_ie_html_description_says_com_returns_outer_html(self):
        self.assertIn("outerHTML", protocol.REGISTRY["ie_html"].description)

    def test_ie_html_over_com_honours_the_limit(self):
        self.page.reply = lambda body, args: "<html>0123456789</html>"
        self.assertEqual(self.text_of(self.call("ie_html", target="hwnd:1", limit=6)), "<html>")


if __name__ == "__main__":
    unittest.main()
