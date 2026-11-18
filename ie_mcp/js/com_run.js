// One request to a document over COM (ie_mcp/backends.py strips these comment lines and fills the
// three placeholders written between at signs below with ES3 literals, in a single pass).
// The request body is turned into a function with the Function constructor, so it runs in the
// global scope of the window this script runs in, whatever the document mode: eval of a
// function expression returns undefined in documentMode 7, and window.eval does not exist on
// a frame window there. A syntax error is raised by the constructor, inside the try: it is
// reported as an error and never reaches the page's own onerror handler.
// The result goes to the expando __iemcp_r and the error text to __iemcp_e (empty on success) of
// the window the target placeholder names (the top window; a frame writes to window.top). They
// are the only two expandos this leaves; everything else stays local to the function below.
(function(){
  var target = @TARGET@, fn, r, m;
  target.__iemcp_e = 'the script did not finish';
  function conv(v){
    var t = typeof v;
    if(v === null || v === undefined || t == 'string' || t == 'number' || t == 'boolean'){ return v; }
    try{ if(typeof v.join == 'function' && typeof v.length == 'number'){ return v.join('\x1e'); } }catch(e1){}
    try{ return String(v); }catch(e2){ return ''; }
  }
  try{
    fn = new Function(@FUNC@);
    r = fn(@ARGS@);
    target.__iemcp_r = conv(r);
    target.__iemcp_e = '';
  }catch(x){
    m = String((x && (x.message || x.description)) || x);
    target.__iemcp_r = null;
    target.__iemcp_e = m || 'error';
  }
})();
