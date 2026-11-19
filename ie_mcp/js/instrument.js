// Page instrumentation (plan Task 12). ES3 only: it runs in Trident, in any document mode.
// It is idempotent through window.__iemcp and keeps a queue of records, window.__iemcp.q, that
// hook.js / drain.js hand to the server. A record is the string
//   kind FS level FS time FS text      (FS = the escape x1f; the text escapes x1e, x1f, backslash)
// The kinds: error, dialog-open (pushed BEFORE the dialog opens, so it exists while it is open),
// dialog (after it returned), modal, open, console and xhr.
// The level field is "name[#dialogid][@popup]": the dialog id (unique across documents: a token
// per install plus a counter) is repeated in the dialog record that closes a dialog-open one, so
// the server pairs them whatever route each was drained by; @popup marks a record a popup handed
// to its opener. st.open counts the alert/confirm/prompt calls in flight (also in a same-origin
// top window): the server reads it to know that a dialog really is the page's own.
// Fidelity rules (CLAUDE.md "observe, don't alter"): every wrapper calls the original and
// returns its value; host functions are called directly (no apply/call: absent in IE < 9) with
// exactly the caller's number of arguments; nothing is created that the page does not have
// (console only with window.__iemcp_stub); ActiveX XMLHTTP objects are never touched.
// It is loaded by the recording proxy through <script src=".../__iemcp__/i.js"> (the script
// element removes itself first, so the DOM matches the original) or injected by hook.js.
(function(){
  var w = window;
  var st, scripts, last, lastErr = '', lastErrAt = 0, busy = false;
  var MAXQ = 2000, MAXTEXT = 2000;
  var CON = ['log','info','warn','error','debug','dir','dirxml','group','groupCollapsed','groupEnd',
             'time','timeEnd','count','assert','trace','table','clear','profile','profileEnd'];

  try{
    scripts = document.getElementsByTagName('script');
    last = scripts.length ? scripts[scripts.length - 1] : null;
    if(last && last.getAttribute && String(last.getAttribute('src', 2) || '').indexOf('/__iemcp__/i.js') >= 0){
      last.parentNode.removeChild(last);
    }
  }catch(e0){}

  if(typeof w.__iemcp != 'undefined'){ return; }
  st = { q: [], v: 1, open: 0, n: 0, sid: '' };
  try{ st.sid = (new Date()).getTime().toString(36) + Math.random().toString(36).substring(2, 8); }catch(e1){ st.sid = 'x'; }
  w.__iemcp = st;

  function str(v){
    try{ return String(v); }catch(e){ return '[unprintable]'; }
  }
  function esc(s){
    return s.replace(/\\/g, '\\\\').replace(/\x1e/g, '\\x1e').replace(/\x1f/g, '\\x1f');
  }
  function now(){
    try{ return str((new Date()).getTime() / 1000); }catch(e){ return '0'; }
  }
  function cut(s, n){
    return s.length > n ? s.substring(0, n) + '...' : s;
  }
  // A popup hands its records to its opener's queue when that is reachable (same origin): the
  // popup may be closed before anyone drains it. Reading another origin's window throws.
  function sink(){
    var o;
    try{
      o = w.opener;
      if(o && !o.closed && o.__iemcp && o.__iemcp.q){ return o.__iemcp.q; }
    }catch(e){}
    return st.q;
  }
  function push(kind, level, text){
    var q, rec;
    try{
      q = sink();
      text = cut(str(text), MAXTEXT);
      if(q !== st.q){ level += '@popup'; }
      rec = kind + '\x1f' + level + '\x1f' + now() + '\x1f' + esc(text);
      q.push(rec);
      if(q.length > MAXQ){ q.shift(); }
    }catch(e){}
  }

  // --- onerror: chained to the page's own handler, which keeps its return value ----------------
  function onerrorWrapper(msg, url, line, col, err){
    var n = arguments.length, prev = st.prev, key, t, r;
    if(busy){ return r; }  // the page chained us inside its own handler: do not loop
    try{
      key = str(msg) + '|' + str(url) + '|' + str(line);
      t = (new Date()).getTime();
      if(!(key == lastErr && t - lastErrAt < 100)){  // the same error seen twice at once
        push('error', 'error', str(msg) + ' (' + str(url) + ':' + str(line) + ')');
        lastErr = key;
        lastErrAt = t;
      }
    }catch(e){}
    if(typeof prev != 'function'){ return r; }
    busy = true;
    try{
      if(n >= 5){ r = prev(msg, url, line, col, err); }
      else if(n == 4){ r = prev(msg, url, line, col); }
      else if(n == 3){ r = prev(msg, url, line); }
      else if(n == 2){ r = prev(msg, url); }
      else if(n == 1){ r = prev(msg); }
      else { r = prev(); }
    }finally{
      busy = false;
    }
    return r;
  }
  onerrorWrapper.__iemcp = 1;
  // Install, or install again after the page replaced window.onerror (hook.js calls this).
  st.chain = function(){
    var cur;
    try{
      cur = w.onerror;
      if(cur && cur.__iemcp === 1){ return; }
      st.prev = cur;
      w.onerror = onerrorWrapper;
    }catch(e){}
  };
  st.chain();

  // --- alert / confirm / prompt ------------------------------------------------------------------
  // Counts the dialog calls in flight, here and in a same-origin top window (a frame's dialog
  // blocks the whole page). Only the page's own wrapper can move it: a native dialog (such as
  // onbeforeunload) leaves it at 0.
  st.bump = function(d){
    var t;
    try{ st.open += d; if(st.open < 0){ st.open = 0; } }catch(e){}
    try{
      t = w.top;
      if(t && t !== w && t.__iemcp && typeof t.__iemcp.open == 'number'){
        t.__iemcp.open += d;
        if(t.__iemcp.open < 0){ t.__iemcp.open = 0; }
      }
    }catch(e2){}
  };
  function wrapDialog(name){
    var orig;
    try{
      orig = w[name];
      if(!orig || (typeof orig != 'function' && typeof orig != 'object')){ return; }
      w[name] = function(a, b){
        var n = arguments.length, r, done = false, msg = n > 0 ? str(a) : '';
        var level = name + '#' + st.sid + '.' + (st.n = st.n + 1);
        push('dialog-open', level, msg);
        st.bump(1);
        try{
          if(n >= 2){ r = orig(a, b); }
          else if(n == 1){ r = orig(a); }
          else { r = orig(); }
          done = true;
        }finally{
          st.bump(-1);
          push('dialog', level, name == 'alert' ? msg : msg + ' -> ' + (done ? str(r) : '(threw)'));
        }
        return r;
      };
    }catch(e){}
  }

  // One level of dialogArguments: string, number and boolean own properties as name=value.
  function summarize(a){
    var out = [], k, v, t, own;
    if(a === null || a === undefined){ return ''; }
    t = typeof a;
    if(t == 'string' || t == 'number' || t == 'boolean'){ return cut(str(a), 200); }
    if(t != 'object'){ return ''; }
    try{
      for(k in a){
        if(out.length >= 20){ break; }
        try{
          own = a.hasOwnProperty ? a.hasOwnProperty(k) : true;
          if(!own){ continue; }
          v = a[k];
          t = typeof v;
          if(t == 'string' || t == 'number' || t == 'boolean'){ out.push(k + '=' + cut(str(v), 200)); }
        }catch(e1){}
      }
    }catch(e2){}
    return out.join(', ');
  }

  function wrapModal(){
    var orig;
    try{
      orig = w.showModalDialog;
      if(!orig || (typeof orig != 'function' && typeof orig != 'object')){ return; }
      w.showModalDialog = function(url, args, features){
        var n = arguments.length, r, done = false, summary = '';
        try{ summary = n >= 2 ? summarize(args) : ''; }catch(e){}
        try{
          if(n >= 3){ r = orig(url, args, features); }
          else if(n == 2){ r = orig(url, args); }
          else if(n == 1){ r = orig(url); }
          else { r = orig(); }
          done = true;
        }finally{
          push('modal', '', 'url=' + (n > 0 ? str(url) : '') + ' features=' + (n >= 3 ? str(features) : '') +
               ' args=[' + summary + '] return=' + (done ? str(r) : '(threw)'));
        }
        return r;
      };
    }catch(e){}
  }

  function wrapOpen(){
    var orig;
    try{
      orig = w.open;
      if(!orig || (typeof orig != 'function' && typeof orig != 'object')){ return; }
      w.open = function(url, name, features, replace){
        var n = arguments.length;
        push('open', '', 'url=' + (n > 0 ? str(url) : '') + ' name=' + (n > 1 ? str(name) : '') +
             ' features=' + (n > 2 ? str(features) : ''));
        if(n >= 4){ return orig(url, name, features, replace); }
        if(n == 3){ return orig(url, name, features); }
        if(n == 2){ return orig(url, name); }
        if(n == 1){ return orig(url); }
        return orig();
      };
    }catch(e){}
  }

  // --- console: only methods the page already has (all of them with the stub flag) -------------------
  function noop(){}
  // Quirk: IE 9 and 10 host methods may throw "Invalid calling object" when called detached, so
  // the original is kept as another property of the console and called as a method of it.
  function wrapConsoleMethod(c, name){
    var alias = '__iemcp_' + name;
    var orig = c[name];
    if(!orig || (typeof orig != 'function' && typeof orig != 'object')){ return; }
    c[alias] = orig;
    c[name] = function(a, b, c3, d, e, f){
      var n = arguments.length, parts = [], i;
      try{
        for(i = 0; i < n; i++){ parts.push(str(arguments[i])); }
        push('console', name, parts.join(' '));
      }catch(x){}
      if(n == 0){ return c[alias](); }
      if(n == 1){ return c[alias](a); }
      if(n == 2){ return c[alias](a, b); }
      if(n == 3){ return c[alias](a, b, c3); }
      if(n == 4){ return c[alias](a, b, c3, d); }
      if(n == 5){ return c[alias](a, b, c3, d, e); }
      return c[alias](a, b, c3, d, e, f);  // more than six arguments: the extra ones are not forwarded
    };
  }
  function wrapConsole(){
    var c, i, name;
    try{
      if(w.__iemcp_stub && typeof w.console == 'undefined'){ w.console = {}; }
      c = w.console;
      if(!c){ return; }
      for(i = 0; i < CON.length; i++){
        name = CON[i];
        try{
          if(w.__iemcp_stub && typeof c[name] == 'undefined'){ c[name] = noop; }
          wrapConsoleMethod(c, name);
        }catch(e1){}
      }
    }catch(e){}
  }

  // --- native XMLHttpRequest: https pages, or when the proxy does not see the traffic ------------------
  // The prototype's open/send are replaced and the originals stay on the prototype under another
  // name, so they are called as methods of the request object. IE7 has no XMLHttpRequest
  // prototype: nothing is wrapped there. The request's own readystatechange handler is not touched:
  // a listener observes the end of the request instead.
  function xhrRecord(x, info){
    var status = '', len = '';
    if(info.done){ return; }
    info.done = true;
    try{ status = str(x.status); }catch(e1){}
    try{ len = str(x.responseText.length); }catch(e2){}
    push('xhr', '', info.m + ' ' + info.u + ' status=' + status + ' resp_len=' + len +
         (info.b ? ' body=' + info.b : ''));
  }
  function wrapXhr(){
    var P;
    try{
      if(!(w.location.protocol == 'https:' || w.__iemcp_wrap_xhr)){ return; }
      if(typeof w.XMLHttpRequest == 'undefined'){ return; }
      P = w.XMLHttpRequest.prototype;
      if(!P || typeof P.open == 'undefined' || typeof P.send == 'undefined'){ return; }
      if(P.__iemcp_open){ return; }
      P.__iemcp_open = P.open;
      P.__iemcp_send = P.send;
      P.open = function(m, u, a, user, pw){
        var n = arguments.length;
        try{ this.__iemcp_x = { m: str(m), u: str(u).replace(/ /g, '%20'), a: n >= 3 ? a : true, b: '' }; }catch(e){}
        if(n >= 5){ return this.__iemcp_open(m, u, a, user, pw); }
        if(n == 4){ return this.__iemcp_open(m, u, a, user); }
        if(n == 3){ return this.__iemcp_open(m, u, a); }
        if(n == 2){ return this.__iemcp_open(m, u); }
        if(n == 1){ return this.__iemcp_open(m); }
        return this.__iemcp_open();
      };
      P.send = function(body){
        var x = this, n = arguments.length, info = null, r;
        try{
          info = x.__iemcp_x;
          if(info){
            if(n > 0 && body !== null && body !== undefined){
              info.b = typeof body == 'string' ? cut(body, 500) : '[' + typeof body + ']';
            }
            if(info.a !== false && x.addEventListener){
              x.addEventListener('readystatechange', function(){
                try{ if(x.readyState == 4){ xhrRecord(x, info); } }catch(e){}
              }, false);
            }
          }
        }catch(e){}
        try{
          r = n > 0 ? x.__iemcp_send(body) : x.__iemcp_send();
        }finally{
          try{ if(info && info.a === false){ xhrRecord(x, info); } }catch(e2){}
        }
        return r;
      };
    }catch(e){}
  }

  // --- hand-over -----------------------------------------------------------------------------------------
  // Empties the queue; every record gets the frame path of this document in front.
  st.take = function(prefix){
    var q = st.q, out = [], i;
    st.q = [];
    for(i = 0; i < q.length; i++){ out.push(prefix + '\x1f' + q[i]); }
    return out.join('\x1e');
  };
  st.sync = function(prefix){
    st.chain();
    return st.take(prefix);
  };

  wrapDialog('alert');
  wrapDialog('confirm');
  wrapDialog('prompt');
  wrapModal();
  wrapOpen();
  wrapConsole();
  wrapXhr();
})();
