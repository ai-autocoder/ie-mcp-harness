var sel=arguments[0], mode=arguments[1], val=arguments[2];
var opts=sel.options, idx=-1;
for(var i=0;i<opts.length;i++){
  if(mode=='label' && opts[i].text==val){ idx=i; break; }
  if(mode=='value' && opts[i].value==val){ idx=i; break; }
  if(mode=='index' && i==parseInt(val,10)){ idx=i; break; }
}
if(idx<0) return 'NOTFOUND';
sel.selectedIndex=idx;
try{ if(sel.fireEvent){ sel.fireEvent('onchange'); }
     else { var e=document.createEvent('HTMLEvents'); e.initEvent('change',true,true); sel.dispatchEvent(e); } }catch(e){}
return 'OK:'+opts[idx].text;
