"""JS dialog tool."""
import json

from ie_mcp.protocol import text_result, tool
from ie_mcp.session import SESSION


@tool(
    "ie_dialog",
    "Handle a JS dialog (alert/confirm/prompt). action: accept (default) | dismiss "
    "| text | sendkeys (with text). Note: OS-level IE modals may be unreachable.",
    {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "accept | dismiss | text | sendkeys"},
            "text": {"type": "string", "description": "text for sendkeys"},
        },
    },
)
def t_dialog(args):
    """Handle a JS dialog (alert/confirm/prompt). action: accept|dismiss|text|sendkeys.

    Caveat: true OS-level IE modals can block IEDriver and may be unreachable here.
    """
    d = SESSION.ensure()
    action = args.get("action", "accept")
    try:
        al = d.switch_to.alert
    except Exception as exc:
        return text_result(json.dumps({"ok": False, "note": f"no dialog present: {str(exc).splitlines()[0]}"}))
    try:
        if action == "text":
            return text_result(json.dumps({"ok": True, "text": al.text}))
        if action == "sendkeys":
            al.send_keys(args.get("text", ""))
            al.accept()
            return text_result(json.dumps({"ok": True, "did": "sent+accepted"}))
        if action == "dismiss":
            txt = al.text
            al.dismiss()
            return text_result(json.dumps({"ok": True, "did": "dismissed", "text": txt}))
        txt = al.text
        al.accept()
        return text_result(json.dumps({"ok": True, "did": "accepted", "text": txt}))
    except Exception as exc:
        return text_result(json.dumps({"ok": False, "note": str(exc).splitlines()[0]}))
