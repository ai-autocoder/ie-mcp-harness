// App-agnostic readiness probe. Run from the top document; it recurses into every
// same-origin frame and reports (a) whether every document.readyState is 'complete'
// and (b) content-size metrics used to detect when the DOM has stopped changing.
// Cross-origin frames are skipped (counted, not read). No app-specific assumptions.
// Returns a field delimited string (separator is the escape x1f below), not structured
// data: pages in IE7/quirks document mode have no window JSON object.
function walk(w, acc){
  try{
    var d = w.document;
    acc.docs++;
    if(d.readyState !== 'complete') acc.ready = false;
    var bt = '';
    try{ bt = d.body ? d.body.innerText : ''; }catch(e){ bt = ''; }
    acc.len += bt.length;
    try{ acc.els += d.getElementsByTagName('*').length; }catch(e){}
    if(bt.length > acc.maxLen){ acc.maxLen = bt.length; acc.sample = bt.substring(0,300); }
  }catch(e){ acc.crossOrigin++; }
  try{ for(var i=0;i<w.frames.length;i++) walk(w.frames[i], acc); }catch(e){}
  return acc;
}
var a = walk(window, {docs:0, ready:true, len:0, els:0, crossOrigin:0, maxLen:0, sample:''});
return [a.ready, a.len, a.els, a.docs, a.crossOrigin, a.sample].join('\x1f');
