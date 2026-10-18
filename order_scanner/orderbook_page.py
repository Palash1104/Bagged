"""The Orderbook view: one row per company, its reported order book over time.

Served at /orderbook. It reuses the orders page's stylesheet (tokens, glass,
table, theme) so the two views read as one product; only what is new here --
the top-gainers strip, sparklines, the column menu, pagination and the
history chart -- is styled below.

Charts follow the dataviz rules the rest of the dashboard uses: one series
per chart in the accent hue, the latest bar in full colour and earlier ones
stepped back, a decline marked in the status red AND with a down arrow in the
text (colour is never the only cue), thin columns with a 2px gap and rounded
tops on a square baseline, and every value a tooltip shows also present in the
statements table under each company.
"""
from __future__ import annotations

EXTRA_CSS = r"""
/* ---------------------------------------------------------------- orderbook */
.obbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:14px 0 0;padding:10px 12px}
.field{display:inline-flex;align-items:center;gap:8px;font-size:12.5px;color:var(--ink-2);white-space:nowrap}
.field input{width:92px;background:var(--glass-2);color:var(--ink);border:1px solid var(--rule);
  border-radius:10px;padding:6px 10px;font-size:13px;font-variant-numeric:tabular-nums}
.field input:focus{outline:none;border-color:var(--accent);
  box-shadow:0 0 0 3px color-mix(in oklab,var(--accent) 22%,transparent)}
.field small{color:var(--ink-3)}
.warn{display:inline-flex;gap:7px;align-items:center;font-size:12px;color:var(--ink-2)}
.warn b{color:var(--ink);font-weight:650}
.warn i{width:16px;height:16px;flex:none;display:grid;place-items:center;border-radius:50%;
  font-style:normal;font-size:11px;font-weight:700;color:#fff;background:var(--medium)}

.secthead{display:flex;align-items:baseline;gap:10px;margin:22px 2px 10px}
.secthead h2{font-size:14px;margin:0;font-weight:650;letter-spacing:-.01em}
.secthead span{font-size:12px;color:var(--ink-3)}
.gainers{display:flex;gap:12px;overflow-x:auto;padding:2px 2px 12px;scroll-snap-type:x proximity}
.gainers::-webkit-scrollbar{height:8px}
.gainers::-webkit-scrollbar-thumb{background:var(--rule);border-radius:8px}
.gcard{flex:0 0 228px;scroll-snap-align:start;padding:13px 15px 12px;cursor:pointer;text-align:left;
  color:inherit;font:inherit;transition:transform .2s cubic-bezier(.2,.8,.2,1),box-shadow .2s}
.gcard:hover{transform:translateY(-2px);box-shadow:var(--deep)}
.gcard:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.gtop{display:flex;justify-content:space-between;align-items:baseline}
.grank{font-size:11.5px;color:var(--ink-3);font-weight:600}
.gpct{font-size:17px;font-weight:650;letter-spacing:-.01em}
.gname{font-weight:600;margin-top:4px;font-size:13.5px}
.gsym{font-size:11px;color:var(--ink-3)}
.gspark{margin:10px 0 6px}
.gval{font-size:12px;color:var(--ink-2);font-variant-numeric:tabular-nums}
.gempty{font-size:12.5px;color:var(--ink-3);padding:6px 4px 14px}

.tabletools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:12px 20px 10px 24px;
  border-bottom:1px solid var(--line)}
.tabletools .hint{font-size:12px;color:var(--ink-3)}
.menuwrap{position:relative}
.toolbtn{appearance:none;border:1px solid var(--line);background:var(--glass-2);color:var(--ink-2);
  font-size:12px;font-weight:600;letter-spacing:.03em;padding:5px 11px;border-radius:9px;cursor:pointer;
  display:inline-flex;gap:7px;align-items:center}
.toolbtn:hover{color:var(--ink);background:var(--glass-hi)}
.toolbtn:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.menu{position:absolute;z-index:40;top:calc(100% + 6px);left:0;min-width:210px;padding:8px;
  border-radius:12px;background:var(--panel);border:1px solid var(--edge);box-shadow:var(--deep);
  -webkit-backdrop-filter:blur(24px);backdrop-filter:blur(24px)}
.menu label{display:flex;gap:9px;align-items:center;padding:5px 8px;border-radius:8px;font-size:12.5px;
  color:var(--ink-2);cursor:pointer}
.menu label:hover{background:var(--hover);color:var(--ink)}
.obsearch{margin-left:auto}

.cosub{display:block;font-size:11px;color:var(--ink-3);margin-top:1px}
.up{color:var(--ink)} .down{color:var(--ink)}
.arrow{font-size:10px;margin-right:3px}
.arrow.u{color:var(--accent)} .arrow.d{color:var(--high)}
.tag2{display:inline-block;margin-left:6px;font-size:10px;font-weight:650;letter-spacing:.04em;
  padding:0 6px;border-radius:5px;border:1px solid var(--line);color:var(--ink-2);vertical-align:1px}
.chk{color:var(--medium);cursor:help;margin-left:5px;font-weight:700}
.histcell{display:flex;align-items:center;gap:10px;justify-content:flex-end}
.histtxt{font-size:11.5px;line-height:1.25;text-align:right;font-variant-numeric:tabular-nums;min-width:58px}
.histtxt b{display:block;font-weight:600;font-size:12px}
.spark rect{transition:opacity .15s}
.pager{display:flex;gap:12px;align-items:center;justify-content:flex-end;padding:12px 24px;
  border-top:1px solid var(--line);font-size:12.5px;color:var(--ink-2)}
.pager select{background:var(--glass-2);color:var(--ink);border:1px solid var(--rule);border-radius:8px;
  padding:3px 6px;font:inherit}
.pager button{appearance:none;border:1px solid var(--line);background:var(--glass-2);color:var(--ink-2);
  border-radius:8px;width:30px;height:28px;cursor:pointer;font-size:14px}
.pager button:disabled{opacity:.4;cursor:default}
.pager button:not(:disabled):hover{color:var(--ink);background:var(--glass-hi)}

/* detail: history chart + every statement */
.obchart{position:relative;margin:16px 0 4px}
.obchart svg{display:block;max-width:100%;height:auto;overflow:visible}
.obchart .tick{font-size:10.5px;fill:var(--ink-3)}
.obchart .grid{stroke:var(--line);stroke-width:1}
.obchart .cap{font-size:11px;fill:var(--ink);font-weight:600}
.obchart .bar{cursor:default;outline:none}
.obchart .bar:hover rect.fill,.obchart .bar:focus rect.fill{filter:brightness(1.12)}
.obchart .bar:focus-visible rect.hit{stroke:var(--accent);stroke-width:1.5}
.tip{position:absolute;pointer-events:none;z-index:20;padding:7px 10px;border-radius:9px;font-size:12px;
  background:var(--panel);border:1px solid var(--edge);box-shadow:var(--lift);white-space:nowrap;
  -webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);transform:translate(-50%,-100%)}
.tip b{display:block;font-size:13px;font-weight:650}
.tip span{color:var(--ink-3)}
.quote{max-width:62ch;color:var(--ink-2);font-size:12px;line-height:1.45}
table.inner td.quote{white-space:normal}
.basis{display:block;font-size:10.5px;color:var(--ink-3)}
.firstrun{padding:28px 28px 32px;font-size:13px;color:var(--ink-2);line-height:1.6}
.firstrun code{background:var(--glass-2);border:1px solid var(--line);border-radius:6px;padding:1px 6px;
  font-size:12px}
@media(max-width:760px){.obsearch{margin-left:0;flex:1 1 100%}.gcard{flex-basis:200px}}
"""

BODY = r"""
<div class="scene" aria-hidden="true">
  <div class="orb o1"></div><div class="orb o2"></div><div class="orb o3"></div>
  <div class="floor"></div>
</div>

<div class="shell">
  <div class="top"><div class="topin glass">
    <div class="brand">
      <span class="logo" aria-hidden="true"><span class="cube"><i></i><i></i><i></i><i></i><i></i><i></i></span></span>
      <div><h1>Order Scanner</h1><div class="tag">Reported order books</div></div>
    </div>
    <nav class="seg views" aria-label="View">
      <a href="/">Orders</a><a href="/orderbook" aria-current="page">Order book</a>
    </nav>
    <span class="spacer"></span>
    <span class="since" id="since"></span>
    <div class="seg" id="theme" role="group" aria-label="Colour theme">
      <button type="button" data-t="system" aria-pressed="true">Auto</button>
      <button type="button" data-t="light" aria-pressed="false">Light</button>
      <button type="button" data-t="dark" aria-pressed="false">Dark</button>
    </div>
  </div></div>

  <div class="obbar glass">
    <div class="chips" id="period" role="group" aria-label="Period">
      <button type="button" data-p="3m" aria-pressed="false">3M</button>
      <button type="button" data-p="6m" aria-pressed="false">6M</button>
      <button type="button" data-p="1y" aria-pressed="true">1Y</button>
      <button type="button" data-p="all" aria-pressed="false">All</button>
    </div>
    <label class="field">Min order book <input id="minbook" type="number" min="0" step="50" placeholder="0"><small>₹ Cr</small></label>
    <label class="field">Min market cap <input id="minmcap" type="number" min="0" step="100" placeholder="0"><small>₹ Cr</small></label>
    <label class="switch" title="Companies that have only ever stated a segment's or a subsidiary's book">
      <input type="checkbox" id="segs" checked><span class="track"></span>Segment-only books</label>
    <span class="spacer"></span>
    <span class="warn"><i aria-hidden="true">!</i><span><b>Read automatically from filings</b> — may contain errors. Verify against the source PDF.</span></span>
  </div>

  <div class="secthead"><h2 id="gtitle">Top gainers — last 1 year</h2><span id="gsub"></span></div>
  <div class="gainers" id="gainers" aria-label="Largest order book growth"></div>

  <div class="card glass" id="card">
    <div class="tabletools">
      <div class="menuwrap">
        <button class="toolbtn" id="colbtn" type="button" aria-haspopup="true" aria-expanded="false">
          <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><path d="M2.5 3h3v10h-3zM6.5 3h3v10h-3zM10.5 3h3v10h-3z" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>COLUMNS</button>
        <div class="menu" id="colmenu" hidden></div>
      </div>
      <span class="hint" id="hint">All companies · sorted by latest update — click “Growth” to rank by momentum.</span>
      <label class="search obsearch">
        <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><circle cx="7" cy="7" r="4.6" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="m10.5 10.5 3.2 3.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
        <input id="q" placeholder="Search companies" aria-label="Search company" autocomplete="off">
      </label>
    </div>
    <div class="scroll"><table>
      <thead><tr id="headrow"></tr></thead><tbody id="tb"></tbody>
    </table></div>
    <div class="empty" id="empty" hidden>No companies match these filters.</div>
    <div class="firstrun" id="firstrun" hidden>
      No order books on record yet. Read them from the filings already stored, then from the
      results releases and investor presentations:<br>
      <code>python -m order_scanner.cli orderbook recheck</code>&nbsp;
      <code>python -m order_scanner.cli orderbook fetch</code>&nbsp;
      <code>python -m order_scanner.cli orderbook meta</code>
    </div>
    <div class="pager" id="pager">
      <label>Rows per page <select id="perpage"><option>50</option><option selected>100</option><option>250</option></select></label>
      <span id="pageinfo"></span>
      <button type="button" id="prev" aria-label="Previous page">‹</button>
      <button type="button" id="next" aria-label="Next page">›</button>
    </div>
  </div>
</div>
"""

SCRIPT = r"""
const $=id=>document.getElementById(id);
const esc=s=>(s??'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const MON=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

/* ---- theme: same key and behaviour as the orders page ---- */
const THEME_KEY='order-scanner-theme';
function applyTheme(t){
  if(t==='light'||t==='dark') document.documentElement.dataset.theme=t;
  else delete document.documentElement.dataset.theme;
  theme.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.t===(t||'system'))));
}
let savedTheme='system'; try{savedTheme=localStorage.getItem(THEME_KEY)||'system'}catch(e){}
applyTheme(savedTheme);
theme.addEventListener('click',e=>{const b=e.target.closest('button'); if(!b) return;
  applyTheme(b.dataset.t); try{localStorage.setItem(THEME_KEY,b.dataset.t)}catch(e){}});
const setBarH=()=>document.documentElement.style.setProperty('--barh',document.querySelector('.top').offsetHeight+'px');
addEventListener('resize',setBarH); setBarH();

/* ---- formatting ---- */
const crs=v=>{if(v==null) return '—'; const c=v/1e7;
  return '₹'+c.toLocaleString('en-IN',{maximumFractionDigits:c>=1000?0:c>=100?1:2})+' Cr';};
const compact=v=>{if(v==null) return '—'; if(!v) return '0'; const c=v/1e7;
  return c>=1e5?(c/1e5).toFixed(1)+'L cr':c>=1e3?(c/1e3).toFixed(1)+'K cr':c.toFixed(c>=100?0:1)+' cr';};
const pct=v=>v==null?'—':(v>=0?'':'−')+Math.abs(v*100).toLocaleString('en-IN',{maximumFractionDigits:Math.abs(v)<.1?1:0})+'%';
const dlong=s=>{const [y,m,d]=(s||'').slice(0,10).split('-'); return d?`${+d} ${MON[+m-1]} ${y}`:'—';};
const span=days=>days==null?'':days<45?days+' days':days<400?Math.round(days/30.4)+' mo':(days/365).toFixed(1)+' yr';
const BASIS={stated:'',quarter:'quarter end, inferred',year:'year end, inferred',filed:'as of the filing date'};
const KIND={presentation:'Investor presentation',results:'Results release',update:'Order book update',
  transcript:'Call transcript',press_release:'Press release',order_win:'Order win release',other:'Filing'};
const arrow=g=>g==null?'':g>=0?'<span class="arrow u" aria-hidden="true">▲</span>':'<span class="arrow d" aria-hidden="true">▼</span>';
const flagsOf=r=>{try{return JSON.parse(r.flags||'[]')}catch(e){return []}};

/* ---- sparkline: one series; earlier bars stepped back, the latest in full
   colour (status red if it fell). 2px gaps, rounded tops, square baseline. ---- */
function colPath(x,y,w,h,r){r=Math.min(r,w/2,h);
  return `M${x},${y+h}V${y+r}Q${x},${y} ${x+r},${y}H${x+w-r}Q${x+w},${y} ${x+w},${y+r}V${y+h}Z`;}
function spark(hist,W=96,H=28){
  if(!hist||!hist.length) return '';
  const n=hist.length, gap=2, bw=Math.min(12,(W-gap*(n-1))/n), max=Math.max(...hist.map(p=>p.v))||1;
  const down=n>1&&hist[n-1].v<hist[n-2].v;
  const x0=W-(bw*n+gap*(n-1));
  return `<svg class="spark" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
    aria-label="Order book history, ${n} point${n>1?'s':''}">`+hist.map((p,i)=>{
      const h=Math.max(2,(p.v/max)*(H-1)), x=x0+i*(bw+gap), last=i===n-1;
      const fill=last?(down?'var(--high)':'var(--accent)'):'color-mix(in oklab,var(--accent) 38%,transparent)';
      return `<path d="${colPath(x,H-h,bw,h,2)}" fill="${fill}"><title>${dlong(p.as_of)}: ${crs(p.v)}</title></path>`;
    }).join('')+'</svg>';
}

/* ---- columns: visibility persisted per browser ---- */
const COLS=[
  {k:'name',label:'Company',fixed:true},
  {k:'growth',label:'Growth',num:true},
  {k:'order_book',label:'Order book',num:true},
  {k:'revenue',label:'Revenue',num:true},
  {k:'book_to_revenue',label:'Book / revenue',num:true},
  {k:'market_cap',label:'Market cap',num:true},
  {k:'as_of',label:'As of'},
  {k:'last_updated',label:'Last updated'},
  {k:'n_points',label:'Order book history',num:true},
  {k:'wins_value',label:'Order wins',num:true},
];
const COLS_KEY='bagged-orderbook-cols';
let hidden=new Set(); try{hidden=new Set(JSON.parse(localStorage.getItem(COLS_KEY)||'[]'))}catch(e){}
const visible=()=>COLS.filter(c=>c.fixed||!hidden.has(c.k));
function buildColMenu(){
  colmenu.innerHTML=COLS.filter(c=>!c.fixed).map(c=>`<label><input type="checkbox" data-k="${c.k}"
    ${hidden.has(c.k)?'':'checked'}> ${esc(c.label)}</label>`).join('');
}
colbtn.addEventListener('click',e=>{e.stopPropagation();
  const open=colmenu.hidden; colmenu.hidden=!open; colbtn.setAttribute('aria-expanded',String(open));});
colmenu.addEventListener('click',e=>e.stopPropagation());
colmenu.addEventListener('change',e=>{const k=e.target.dataset.k; if(!k) return;
  e.target.checked?hidden.delete(k):hidden.add(k);
  try{localStorage.setItem(COLS_KEY,JSON.stringify([...hidden]))}catch(e){}
  render();});
document.addEventListener('click',()=>{colmenu.hidden=true;colbtn.setAttribute('aria-expanded','false')});

/* ---- state ---- */
let all=[],rows=[],period='1y',sortK='last_updated',sortD=-1,page=0;
const PLABEL={'3m':'3 months','6m':'6 months','1y':'1 year','all':'all time'};

async function load(){
  $('card').classList.add('busy');
  try{
    const d=await (await fetch('/api/orderbook?period='+period)).json();
    all=d.rows||[];
    $('firstrun').hidden=all.length>0;
    apply();
  }finally{$('card').classList.remove('busy')}
}

/* filters are remembered per browser, like the column choice */
const FILT_KEY='bagged-orderbook-filters';
try{const f=JSON.parse(localStorage.getItem(FILT_KEY)||'{}');
  if(f.minbook!=null) minbook.value=f.minbook; if(f.minmcap!=null) minmcap.value=f.minmcap;
  if(f.segs!=null) segs.checked=!!f.segs;}catch(e){}
let noMcap=0;
function apply(){
  try{localStorage.setItem(FILT_KEY,JSON.stringify({minbook:minbook.value,minmcap:minmcap.value,segs:segs.checked}))}catch(e){}
  const mb=(+minbook.value||0)*1e7, mm=(+minmcap.value||0)*1e7, qv=q.value.trim().toLowerCase();
  const base=all.filter(r=>(r.order_book||0)>=mb
    && (segs.checked||r.scope==='total')
    && (!qv||(r.name||'').toLowerCase().includes(qv)||(r.nse_symbol||'').toLowerCase().includes(qv)
        ||(r.bse_code||'').includes(qv)));
  rows=base.filter(r=>!mm||(r.market_cap||0)>=mm);
  noMcap=mm?base.filter(r=>!r.market_cap).length:0;
  gainers();
  page=0; render();
}

function gainers(){
  const ranked=rows.filter(r=>r.growth!=null&&!r.growth_suspect);
  const g=ranked.sort((a,b)=>b.growth-a.growth).slice(0,12);
  const held=rows.filter(r=>r.growth_suspect).length;
  $('gtitle').textContent='Top gainers — last '+PLABEL[period];
  $('gsub').textContent=g.length?`${ranked.length} companies with statements at least two months apart`
    +(held?` · ${held} with a jump of 4× or more left out — marked ! in the table`:''):'';
  $('gainers').innerHTML=g.length?g.map((r,i)=>`<button type="button" class="gcard glass" data-cid="${esc(r.company_id)}"
      title="from ${esc(crs(r.history.find(p=>p.as_of===r.growth_from)?.v))} on ${esc(dlong(r.growth_from))} to ${esc(crs(r.order_book))} on ${esc(dlong(r.as_of))}">
      <div class="gtop"><span class="grank">#${i+1}</span><span class="gpct">${arrow(r.growth)}${pct(r.growth)}</span></div>
      <div class="gname clip">${esc(r.name)}</div>
      <div class="gsym">${esc(r.nse_symbol?'NSE: '+r.nse_symbol:'BSE: '+(r.bse_code||''))} · over ${esc(span(r.growth_days))}</div>
      <div class="gspark">${spark(r.history,198,40)}</div>
      <div class="gval">${esc(crs(r.order_book))}${r.scope!=='total'?' · segment':''}</div></button>`).join('')
    :`<div class="gempty">Growth needs two order-book statements inside the period. Run <b>orderbook fetch</b> to read
      the quarterly results releases and presentations — and <b>orderbook history</b> to reach back a year.</div>`;
}
$('gainers').addEventListener('click',e=>{const c=e.target.closest('.gcard'); if(!c) return;
  const i=rows.findIndex(r=>r.company_id===c.dataset.cid); if(i<0) return;
  page=Math.floor(i/per()); render();
  const tr=document.querySelector(`tr.row[data-cid="${CSS.escape(c.dataset.cid)}"]`);
  if(tr){tr.scrollIntoView({behavior:'smooth',block:'center'}); const b=tr.querySelector('.cotoggle'); if(b&&b.getAttribute('aria-expanded')!=='true') toggle(b);}
});

const per=()=>+perpage.value||100;

function cell(c,r){
  const fl=flagsOf(r);
  switch(c.k){
  case 'name':{
    const ids=[r.nse_symbol?'NSE: '+r.nse_symbol:'', r.bse_code?'BSE: '+r.bse_code:''].filter(Boolean).join(' · ');
    return `<td><div class="cowrap"><button class="cotoggle" type="button" aria-expanded="false"
      title="every order book statement on record"><span class="chev"><svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M6 3.5 10.5 8 6 12.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
      <span class="cotext"><span class="co">${esc(r.name)}</span><span class="cosub">${esc(ids)}</span></span></button></div></td>`;}
  case 'growth':{
    const chk=r.growth_suspect?`<span class="chk" title="The book moved 4× or more between two statements: more often a misread or a change of scope than growth. Check the statements (click the company).">!</span>`:'';
    return `<td class="num" title="${r.growth==null?'needs two order book statements at least two months apart in the period':esc('since '+dlong(r.growth_from)+' ('+span(r.growth_days)+')')}">
      ${r.growth==null?'<span class="muted">—</span>':`<span class="val">${arrow(r.growth)}${pct(r.growth)}${chk}</span><span class="minor">${esc(span(r.growth_days))}</span>`}</td>`;}
  case 'order_book':
    return `<td class="num"><span class="val">${esc(crs(r.order_book))}${fl.length?`<span class="chk" title="${esc(fl.join('; '))}">!</span>`:''}</span>
      <span class="minor">${[r.is_approx?'approx.':'',r.scope!=='total'?'segment / subsidiary':''].filter(Boolean).join(' · ')||'&nbsp;'}</span></td>`;
  case 'revenue':
    return `<td class="num"><span class="val">${esc(crs(r.revenue))}</span><span class="minor">${esc(r.revenue_label||'')||'&nbsp;'}</span></td>`;
  case 'book_to_revenue':
    return `<td class="num">${r.book_to_revenue==null?'<span class="muted">—</span>':`<span class="val">${r.book_to_revenue.toFixed(2)}x</span>`}</td>`;
  case 'market_cap':
    return `<td class="num">${r.market_cap?esc(crs(r.market_cap)):'<span class="muted">—</span>'}</td>`;
  case 'as_of':
    return `<td class="nowrap">${esc(dlong(r.as_of))}${BASIS[r.as_of_basis]?`<span class="basis">${esc(BASIS[r.as_of_basis])}</span>`:''}</td>`;
  case 'last_updated':
    return `<td class="nowrap">${esc(dlong(r.last_updated))}</td>`;
  case 'n_points':{
    const h=r.history, n=h.length, first=h[0]?.v, chg=n>1&&first?h[n-1].v/first-1:null;
    return `<td><div class="histcell">${spark(h)}<span class="histtxt"><b>${esc(compact(r.order_book))}</b>
      ${chg==null?'<span class="muted">1 point</span>':`${arrow(chg)}${pct(chg)}`}</span></div></td>`;}
  case 'wins_value':
    return `<td class="num" title="order wins Bagged recorded in the last ${esc(PLABEL[period])}">${r.wins_count?
      `<span class="val">${esc(crs(r.wins_value))}</span><span class="minor">${r.wins_count} win${r.wins_count>1?'s':''}</span>`:'<span class="muted">—</span>'}</td>`;
  }
  return '<td></td>';
}

function render(){
  const cols=visible();
  const PSHORT={'3m':'3M','6m':'6M','1y':'1Y','all':'all'};
  headrow.innerHTML=cols.map(c=>`<th data-k="${c.k}" class="${c.num?'num':''}"
    ${sortK===c.k?`aria-sort="${sortD<0?'descending':'ascending'}"`:''}>${esc(c.k==='growth'?`Growth (${PSHORT[period]})`:c.label)}</th>`).join('');
  headrow.querySelectorAll('th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;
    sortD=(sortK===k)?-sortD:-1; sortK=k; page=0; render();
    hint.textContent=k==='growth'?'Ranked by order book growth over '+PLABEL[period]+'.':'All companies · sorted by '+(COLS.find(c=>c.k===k)||{}).label.toLowerCase()+'.';});
  rows.sort((a,b)=>{const x=a[sortK],y=b[sortK];
    if(x==null&&y==null)return 0; if(x==null)return 1; if(y==null)return -1;
    return (x>y?1:x<y?-1:0)*sortD;});
  const n=rows.length, p=per(), pages=Math.max(1,Math.ceil(n/p));
  page=Math.min(page,pages-1);
  const slice=rows.slice(page*p,page*p+p);
  tb.innerHTML=slice.map(r=>`<tr class="row t-STORE" data-cid="${esc(r.company_id)}">${cols.map(c=>cell(c,r)).join('')}</tr>
    <tr class="det" hidden><td colspan="${cols.length}"><div class="stage"><div class="detbox"></div></div></td></tr>`).join('');
  empty.hidden=n>0||!all.length;
  pageinfo.textContent=(n?`${page*p+1}–${Math.min(n,page*p+p)} of ${n}`:'0 of 0')
    +(noMcap?` · ${noMcap} hidden with no market cap on record`:'');
  prev.disabled=page===0; next.disabled=page>=pages-1;
}
prev.onclick=()=>{page--;render()}; next.onclick=()=>{page++;render()};
perpage.onchange=()=>{page=0;render()};

/* ---- detail: history chart + every statement, with its evidence ---- */
const cache={};
function chart(series,W){
  // one column per as-of date (whole-company book where stated). Drawn at the
  // panel's real pixel width so text and bar widths stay at their specs.
  W=Math.max(320,Math.round(W||720));
  const H=190,L=54,R=12,T=18,B=30, n=series.length;
  if(!n) return '';
  const max=Math.max(...series.map(s=>s.v)), nice=niceMax(max), iw=W-L-R, ih=H-T-B;
  const slot=iw/n, bw=Math.min(24,slot*0.62);
  const ticks=[0,.5,1].map(f=>nice*f);
  let g=ticks.map(t=>{const y=T+ih-(t/nice)*ih;
    return `<line class="grid" x1="${L}" x2="${W-R}" y1="${y}" y2="${y}"/><text class="tick" x="${L-8}" y="${y+3.5}" text-anchor="end">${esc(compact(t))}</text>`}).join('');
  const every=Math.max(1,Math.ceil(n/8));
  g+=series.map((s,i)=>{const h=Math.max(2,(s.v/nice)*ih), x=L+i*slot+(slot-bw)/2, y=T+ih-h, last=i===n-1;
    const down=last&&n>1&&s.v<series[n-2].v;
    const fill=last?(down?'var(--high)':'var(--accent)'):'color-mix(in oklab,var(--accent) 45%,transparent)';
    const lab=(i%every===0||last)?`<text class="tick" x="${x+bw/2}" y="${H-10}" text-anchor="middle">${esc(dlong(s.as_of).replace(/^\d+ /,''))}</text>`:'';
    const cap=last?`<text class="cap" x="${x+bw/2}" y="${y-6}" text-anchor="middle">${esc(compact(s.v))}</text>`:'';
    return `<g class="bar" tabindex="0" data-i="${i}" role="img" aria-label="${esc(dlong(s.as_of)+': '+crs(s.v))}">
      <rect class="hit" x="${L+i*slot}" y="${T}" width="${slot}" height="${ih}" fill="transparent"/>
      <path class="fill" d="${colPath(x,y,bw,h,4)}" fill="${fill}"/>${cap}${lab}</g>`}).join('');
  return `<div class="obchart"><svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">${g}</svg><div class="tip" hidden></div></div>`;
}
function niceMax(v){if(!v) return 1; const p=Math.pow(10,Math.floor(Math.log10(v))), m=v/p;
  return (m<=1?1:m<=2?2:m<=2.5?2.5:m<=5?5:10)*p;}
function wireChart(box,series){
  const wrap=box.querySelector('.obchart'); if(!wrap) return;
  const tip=wrap.querySelector('.tip');
  const show=(el)=>{const s=series[+el.dataset.i], prev=series[+el.dataset.i-1];
    tip.textContent='';
    const b=document.createElement('b'); b.textContent=crs(s.v); tip.appendChild(b);
    const sp=document.createElement('span');
    sp.textContent=dlong(s.as_of)+(prev?` · ${s.v>=prev.v?'▲':'▼'} ${pct(s.v/prev.v-1)} vs previous`:'');
    tip.appendChild(sp);
    const r=el.querySelector('rect.hit').getBoundingClientRect(), w=wrap.getBoundingClientRect(),
          f=el.querySelector('path.fill').getBoundingClientRect();
    tip.style.left=(r.left-w.left+r.width/2)+'px'; tip.style.top=(f.top-w.top-8)+'px'; tip.hidden=false;};
  wrap.querySelectorAll('.bar').forEach(el=>{
    el.addEventListener('pointerenter',()=>show(el)); el.addEventListener('focus',()=>show(el));
    el.addEventListener('pointerleave',()=>tip.hidden=true); el.addEventListener('blur',()=>tip.hidden=true);});
}
function renderDetail(d,row,W){
  const c=d.company||{}, f=d.fundamentals||{}, ss=d.snapshots||[];
  const fact=(k,v)=>`<div class="fact"><span>${esc(k)}</span><b>${v}</b></div>`;
  const scr=(c.nse_symbol||c.bse_code)?`https://www.screener.in/company/${encodeURIComponent(c.nse_symbol||c.bse_code)}/`:'';
  const head=`<div class="dethead"><div><div class="dtitle">${esc(c.name||row.name)}${scr?` <a class="extlink" href="${scr}" target="_blank" rel="noopener">screener.in ↗</a>`:''}</div>
    <div class="dids">${esc([c.nse_symbol?'NSE: '+c.nse_symbol:'',c.bse_code?'BSE: '+c.bse_code:'',c.industry||''].filter(Boolean).join(' · '))}</div></div>
    <div class="facts">${fact('Order book',esc(crs(row.order_book)))}${fact('As of',esc(dlong(row.as_of)))}
    ${fact('Book / revenue',row.book_to_revenue==null?'—':row.book_to_revenue.toFixed(2)+'x')}
    ${fact('Order wins, '+PLABEL[period],row.wins_count?esc(crs(row.wins_value))+` <span class="muted" style="font-weight:500">· ${row.wins_count}</span>`:'—')}</div></div>`;
  const series=row.history.map(h=>({as_of:h.as_of,v:h.v}));
  const note=`<p class="detnote">${row.scope==='total'?'Whole-company order book':'Segment or subsidiary book — the company has not stated a whole-company figure'}, one bar per quarter (the best statement in it)${row.n_points>12?' (latest 12)':''}. Every statement it was read from is listed below.</p>`;
  const body=ss.map(s=>{let fl=[];try{fl=JSON.parse(s.flags||'[]')}catch(e){}
    return `<tr><td class="nowrap">${esc(dlong(s.as_of))}${BASIS[s.as_of_basis]?`<span class="basis">${esc(BASIS[s.as_of_basis])}</span>`:''}</td>
    <td class="num">${esc(crs(s.order_book_inr))}${s.is_approx?'<span class="minor">approx.</span>':''}${fl.length?`<span class="chk" title="${esc(fl.join('; '))}">!</span>`:''}</td>
    <td class="nowrap">${esc(s.scope==='total'?'Company':s.scope==='segment'?'Segment':'Subsidiary')}</td>
    <td class="nowrap">${esc(KIND[s.source_kind]||'Filing')}<span class="basis">${esc((s.exchange||'')+' · filed '+dlong(s.filed_at))}</span></td>
    <td class="quote" title="${esc(s.notes||'')}">“${esc(s.quote||'')}”</td>
    <td class="nowrap">${s.pdf_url?`<a class="pdfchip" href="/api/annpdf/${encodeURIComponent(s.ann_id)}" target="_blank" rel="noopener">PDF</a>`:''}</td></tr>`}).join('');
  return head+chart(series,W)+note+`<table class="inner"><thead><tr><th>As of</th><th class="num">Order book</th><th>Scope</th>
    <th>Source</th><th>Stated as</th><th>Filing</th></tr></thead><tbody>${body}</tbody></table>`;
}
async function toggle(btn){
  const tr=btn.closest('tr'), det=tr.nextElementSibling, cid=tr.dataset.cid;
  const opening=det.hidden; det.hidden=!opening; tr.classList.toggle('open',opening);
  btn.setAttribute('aria-expanded',String(opening));
  if(!opening||det.dataset.loaded) return;
  det.dataset.loaded='1';
  const box=det.querySelector('.detbox'), row=rows.find(r=>r.company_id===cid);
  box.innerHTML='<p class="detnote" style="margin:0">Loading…</p>';
  try{
    if(!cache[cid]) cache[cid]=await (await fetch('/api/orderbook/'+encodeURIComponent(cid))).json();
    box.innerHTML=renderDetail(cache[cid],row,box.clientWidth-42);
    wireChart(box,row.history);
  }catch(e){det.dataset.loaded=''; box.innerHTML='<p class="detnote" style="margin:0">Could not load this company’s order books.</p>';}
}
tb.addEventListener('click',e=>{const b=e.target.closest('.cotoggle'); if(b) toggle(b);});

/* ---- controls ---- */
$('period').addEventListener('click',e=>{const b=e.target.closest('button'); if(!b) return;
  period=b.dataset.p;
  $('period').querySelectorAll('button').forEach(o=>o.setAttribute('aria-pressed',String(o===b)));
  load();});
['minbook','minmcap','q','segs'].forEach(id=>{const el=$(id);
  el.addEventListener(el.type==='checkbox'?'change':'input',()=>{clearTimeout(window._t);window._t=setTimeout(apply,200)});});

fetch('/api/stats').then(r=>r.json()).then(s=>{const last=(s.runs||[])[0];
  since.textContent=last?'Last scan '+(last.finished_at||'').slice(0,16).replace('T',' '):'No scans yet'; setBarH();}).catch(()=>{});
buildColMenu(); load();
"""


def render(css: str) -> str:
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
            "<title>Order book · Order Scanner</title>\n<style>" + css + EXTRA_CSS +
            "</style></head><body>" + BODY + "<script>" + SCRIPT + "</script>\n</body></html>")
