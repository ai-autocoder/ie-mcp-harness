// Returns the grid as a record/field delimited string (separators are the escapes
// x1e and x1f below), never as structured data: IE7 and quirks mode have no window
// JSON object. Picks the table with the most rows that have two or more cells and skips
// single-cell wrapper/layout tables.
var sel = arguments[0];
function clean(s){ return (s||'').replace(/[\r\n\t]+/g,' ').replace(/\x1f/g,' ').replace(/\x1e/g,' ').replace(/^ +| +$/g,''); }
var table = null;
if(sel){ try{ table = document.getElementById(sel); }catch(e){} }
if(!table){
  var tabs = document.getElementsByTagName('table'), bestScore=-1;
  for(var i=0;i<tabs.length;i++){
    var rws = tabs[i].rows, multi=0, maxc=0;
    for(var r=0;r<rws.length;r++){ var n=rws[r].cells.length; if(n>=2) multi++; if(n>maxc) maxc=n; }
    var score = multi*1000 + maxc;
    if(multi>0 && score>bestScore){ bestScore=score; table=tabs[i]; }
  }
}
if(!table) return '';
var out=[];
var rws=table.rows;
for(var r=0;r<rws.length;r++){
  var cells=rws[r].cells; if(cells.length<2) continue;
  var row=[];
  for(var c=0;c<cells.length;c++){ row.push(clean(cells[c].innerText)); }
  out.push(row.join('\x1f'));
}
return out.join('\x1e');
