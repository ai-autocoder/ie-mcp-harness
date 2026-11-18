// Helpers prepended to the body of every COM operation that acts on an element (ie_mcp/backends.py).
// They run in the target document, so they are ES3 (see CLAUDE.md): no JSON, no forEach, no trim.
// iemcpFind(by, value) is the locator of the COM backend: id, name, tag, class, link_text, and
// css only where querySelector exists (documentMode 8 or later). It returns null when nothing matches.
function iemcpFind(by, value){
  var list, all, i, el, text;
  if(by == 'id'){ return document.getElementById(value); }
  if(by == 'name'){ list = document.getElementsByName(value); return list.length ? list[0] : null; }
  if(by == 'tag'){ list = document.getElementsByTagName(value); return list.length ? list[0] : null; }
  if(by == 'css'){
    if(document.documentMode >= 8){ return document.querySelector(value); }
    throw new Error('css locators need documentMode 8 or later (this document: ' + document.documentMode + ')');
  }
  if(by == 'class'){
    all = document.getElementsByTagName('*');
    for(i = 0; i < all.length; i++){
      if((' ' + all[i].className + ' ').indexOf(' ' + value + ' ') >= 0){ return all[i]; }
    }
    return null;
  }
  if(by == 'link_text'){
    all = document.getElementsByTagName('a');
    for(i = 0; i < all.length; i++){
      el = all[i];
      text = String(el.innerText || el.textContent || '').replace(/^\s+|\s+$/g, '');
      if(text == value){ return el; }
    }
    return null;
  }
  throw new Error('unknown locator: ' + by);
}
// Resolves the element of a request (the first two arguments are the locator) or fails.
function iemcpElement(by, value){
  var el = iemcpFind(by, value);
  if(!el){ throw new Error('element not found: ' + by + '=' + value); }
  return el;
}
// Fires one event by name ('onclick', 'onchange', ...) the way the document's mode supports:
// fireEvent in the old models, dispatchEvent otherwise (documentMode 11 has no fireEvent).
// Synthetic: this is a script event, not mouse or keyboard input.
function iemcpFire(el, on){
  var type = on.substring(2), e, bubbles = (type != 'mouseenter' && type != 'mouseleave' && type != 'blur' && type != 'focus');
  try{
    if(el.fireEvent){ el.fireEvent(on); return true; }
    if(document.createEvent){
      e = document.createEvent('Events');
      e.initEvent(type, bubbles, true);
      el.dispatchEvent(e);
      return true;
    }
  }catch(x){}
  return false;
}
