// Runs a request (the runner script, com_run.js, filled in) inside a frame of the top window
// (ie_mcp/backends.py strips these comment lines and fills the two placeholders written between
// at signs below with ES3 literals). The path is an array of numbers and names, the run text
// a string literal.
// Resolving a frame by index uses window.frames[n], the order the driver's frame switch uses; a
// name or id is looked up among the frame and iframe elements in document order.
// Reading w.document is what raises "Access is denied" for a cross-origin frame (or one that
// is still loading), so that case names the frame instead of failing inside the script.
// The runner has to execute in the frame's own global scope: execScript where the frame has it
// (documentMode 7-8 frames have no eval or Function visible from outside, and no execScript is
// visible on documentMode 11 windows), else eval. Any failure here is a frame problem.
(function(){
  var w = window, label = '', i, seg, m;
  var run=@RUN@;
  var path=@PATH@;
  window.__iemcp_e = 'the script did not finish';
  function child(win, wanted){
    var all, n, k, el, tag;
    if(typeof wanted == 'number'){ return win.frames[wanted]; }
    all = win.document.getElementsByTagName('*');
    n = 0;
    for(k = 0; k < all.length; k++){
      el = all[k];
      tag = String(el.tagName).toUpperCase();
      if(tag != 'FRAME' && tag != 'IFRAME'){ continue; }
      if(el.name == wanted || el.id == wanted){ return win.frames[n]; }
      n++;
    }
    return null;
  }
  try{
    for(i = 0; i < path.length; i++){
      seg = path[i];
      label = (label == '' ? '' : label + '/') + String(seg);
      w = child(w, seg);
      if(!w){ throw new Error('no such frame'); }
      w.document;
    }
    if(w.execScript){ w.execScript(run); }
    else if(w.eval){ w.eval(run); }
    else { throw new Error('script cannot run in this frame'); }
  }catch(x){
    m = String((x && (x.message || x.description)) || x);
    window.__iemcp_r = null;
    window.__iemcp_e = 'frame "' + label + '" cannot be used: ' + m;
  }
})();
