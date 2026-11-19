// Drains ONE document, for COM targets (a modal dialog, a pop-up, an attached tab): injects
// instrument.js where it is missing and returns the queued records with an empty frame path
// (escapes x1e and x1f as separators, see hook.js). It runs as the body of a COM request, in
// the global scope of the target window, so window is that document's window.
// arguments[0] = the source of instrument.js, arguments[1] = stub flag, arguments[2] = wrap_xhr flag.
// The last record is a pseudo-record of kind "state" whose text is open=<n>: the number of
// alert/confirm/prompt calls in flight in this window (see instrument.js).
// ES3 only; kept in step with hook.js.
var src = arguments[0], w = window, s, host, got, state = '', fs;
try{
  if(typeof w.__iemcp == 'undefined'){
    w.__iemcp_stub = !!arguments[1];
    w.__iemcp_wrap_xhr = !!arguments[2];
    if(w.execScript){ w.execScript(src); }
    else{
      s = w.document.createElement('script');
      s.type = 'text/javascript';
      s.text = src;
      host = w.document.getElementsByTagName('head')[0] || w.document.documentElement;
      host.appendChild(s);
      host.removeChild(s);
    }
  }
  if(typeof w.__iemcp != 'undefined'){
    got = w.__iemcp.sync('');
    fs = String.fromCharCode(31);
    state = fs + 'state' + fs + fs + '0' + fs + 'open=' + w.__iemcp.open;
    return got ? got + String.fromCharCode(30) + state : state;
  }
}catch(e){}
return '';
