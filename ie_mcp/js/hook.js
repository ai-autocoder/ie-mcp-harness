// Post-call hook (plan Task 12): runs once, from the driver's current document, and walks the
// same-origin frames of the top window. In each document it injects instrument.js where it is
// missing, re-chains onerror when the page replaced it, and drains the queue.
// arguments[0] = the source of instrument.js, arguments[1] = flags {stub, wrap_xhr}.
// Returns every record, each prefixed with its frame path ("" for the top window, "0/1" for a
// nested frame) and the field separator, joined by the record separator (escapes x1e, x1f).
// ES3 only (Trident, any document mode); every probe is in try/catch (cross-origin frames
// throw). The injected script element is removed again right after it ran, so ie_html shows
// the original DOM. Kept in step with drain.js, which does the same for one document.
var src = arguments[0], flags = arguments[1] || {};
var out = [];
function inject(w){
  var d = w.document, s, host;
  if(w.execScript){ w.execScript(src); return; }  // old document modes
  s = d.createElement('script');
  s.type = 'text/javascript';
  s.text = src;  // runs on insertion
  host = d.getElementsByTagName('head')[0] || d.documentElement;
  host.appendChild(s);
  host.removeChild(s);
}
function visit(w, path, depth){
  var i, n, got;
  try{
    if(!w.document || !w.document.documentElement){ return; }  // cross-origin: access denied
  }catch(e0){ return; }
  try{
    if(typeof w.__iemcp == 'undefined'){
      w.__iemcp_stub = !!flags.stub;
      w.__iemcp_wrap_xhr = !!flags.wrap_xhr;
      inject(w);
    }
    if(typeof w.__iemcp != 'undefined'){
      got = w.__iemcp.sync(path);
      if(got){ out.push(got); }
    }
  }catch(e1){}
  if(depth >= 8){ return; }
  try{
    n = w.frames.length;
    for(i = 0; i < n; i++){
      visit(w.frames[i], path == '' ? String(i) : path + '/' + i, depth + 1);
    }
  }catch(e2){}
}
var root = window;
try{
  if(window.top && window.top.document){ root = window.top; }  // the driver may be inside a frame
}catch(e3){}
visit(root, '', 0);
return out.join('\x1e');
