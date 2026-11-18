// Lists the frame and iframe elements of the current document as a record/field delimited
// string (separators are the escapes x1e and x1f below): index, name, id, src per frame.
// Never structured data: IE7 and quirks mode have no window JSON object. The index is the
// position in document order, which is the index the driver's frame switch uses. src is the
// attribute as written (getAttribute with flag 2; older modes would resolve it otherwise).
function clean(s){ return String(s==null ? '' : s).replace(/[\r\n\t]+/g,' ').replace(/\x1f/g,' ').replace(/\x1e/g,' '); }
var out = [], n = 0, all = null;
try{ all = document.getElementsByTagName('*'); }catch(e){ all = null; }
if(all){
  for(var i=0;i<all.length;i++){
    var el = all[i], tag = '';
    try{ tag = String(el.tagName).toUpperCase(); }catch(e){ continue; }
    if(tag != 'FRAME' && tag != 'IFRAME') continue;
    var name = '', id = '', src = '';
    try{ name = el.name; }catch(e){}
    try{ id = el.id; }catch(e){}
    try{ src = el.getAttribute('src', 2); }catch(e){ try{ src = el.src; }catch(e2){} }
    out.push(n + '\x1f' + clean(name) + '\x1f' + clean(id) + '\x1f' + clean(src));
    n++;
  }
}
return out.join('\x1e');
