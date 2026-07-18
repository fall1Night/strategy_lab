# -*- coding: utf-8 -*-
"""量化回测 Web 服务（零依赖，仅用 Python 标准库）。

浏览器打开 http://localhost:8000 后：
  - 标的输入框支持「联网实时模糊搜索」：输入名称/代码（如「浙江」「600216」）即弹出下拉候选，点击填入；可多选
  - 选择「评估起止日期」
  - 选择「策略」（下拉，读取 strategies/ 下所有 .toml）
  - 点「运行回测」→ 后端跑同一套 engine，返回「策略对比」排版仪表盘

实现：标准库 http.server + ThreadingHTTPServer。
  GET  /           表单页
  GET  /api/search 标的实时搜索（?q=关键词 → JSON {items:[{code,name,symbol}]}）
  POST /run        解析表单 → run_symbol + build_compare_dashboard → 返回自包含仪表盘 HTML
仪表盘 HTML 为自包含单文件（内联 JS / report-data / SVG），可直接渲染；
本服务在返回前注入一个浮层「← 新建回测」按钮，方便回到表单。
"""
from __future__ import annotations

import logging
import os
import re
import datetime
import urllib.parse
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine.config import load_strategy_by_arg, list_available_strategies
from .engine.backtest import run_symbol
from .engine.dashboard import build_compare_dashboard, build_compare_from_runs
from .engine.vendor.render_dashboard import render_dashboard
from .engine.search import search_a_stocks
from .engine.storage import repository as storage_repo
from .settings import get_data_dir

logger = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", "8000"))


# --------------------------------------------------------------------------
# 策略枚举
# --------------------------------------------------------------------------
def list_strategy_options():
    """返回 [(展示名, 别名), ...]，扫描内置资源 + 用户策略目录。"""
    return list_available_strategies()


def _resolve_strategy(arg: str | None) -> dict:
    """按参数加载策略配置（文件路径 / 别名 / 内置资源）。"""
    return load_strategy_by_arg(arg or "kdj_macd_dual_entry")


# --------------------------------------------------------------------------
# 回测执行（复用 engine）
# --------------------------------------------------------------------------
def run_backtest(symbols: list[str], start: str, end: str, strategy_arg: str,
                 names: list[str] | None = None) -> str:
    """跑回测，写 index.html（渲染视图）到 data/ 目录，返回其 HTML 文本。

    回测结果已落库（run_symbol 内部完成），index.html 仅作渲染视图。
    """
    import uuid

    cfg = _resolve_strategy(strategy_arg)
    out_dir = get_data_dir()
    batch_id = str(uuid.uuid4())
    name_list = names or []
    results = {}
    for idx, sym in enumerate(symbols):
        sym = sym.strip()
        stock_name = name_list[idx] if idx < len(name_list) and name_list[idx] else sym
        r = run_symbol(cfg, sym, stock_name, start, end, out_dir, batch_id=batch_id)
        results[r["prefix"]] = r
    out_html = out_dir / "index.html"
    build_compare_dashboard(results, cfg, out_html, start, end)
    return out_html.read_text(encoding="utf-8")


def inject_back_button(html: str) -> str:
    bar = (
        '<a href="/" '
        'style="position:fixed;top:10px;right:14px;z-index:99999;background:#1f6feb;'
        'color:#fff;padding:7px 14px;border-radius:7px;text-decoration:none;'
        'font:13px/1.4 system-ui,-apple-system,sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.25)">'
        '← 新建回测</a>'
    )
    if "</body>" in html:
        return html.replace("</body>", bar + "</body>", 1)
    return html + bar


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------
def build_form_html() -> str:
    today = datetime.date.today().strftime("%Y-%m-%d")
    strat_opts = "\n".join(
        f'      <option value="{fname}">{name}</option>' for name, fname in list_strategy_options()
    )
    import json as _json, pathlib
    _sec_path = pathlib.Path("data/sectors.json")
    _sectors = _json.loads(_sec_path.read_text(encoding="utf-8")) if _sec_path.exists() else []
    sector_opts = "\n".join(f'          <option value="{s["code"]}">{s["name"]}</option>' for s in _sectors)

    extra_css = """
  #symbols-wrap { position: relative; }
  #sym-suggest { position:absolute; left:0; right:0; top:100%; margin-top:5px; z-index:50;
          background:#0f172a; border:1px solid #334155; border-radius:9px; max-height:264px; overflow:auto;
          box-shadow:0 8px 24px rgba(0,0,0,.45); display:none; }
  .sg { display:flex; gap:10px; align-items:center; padding:9px 13px; cursor:pointer; border-bottom:1px solid #1e293b; }
  .sg:last-child { border-bottom:0; }
  .sg:hover { background:#172033; }
  .sg .c { color:#64748b; font-size:11px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; min-width:78px; text-align:right; }
  .sg .n { color:#e2e8f0; font-size:14px; flex:1; }
  .sg .n b, .sg .c b { color:#fbbf24; font-weight:700; }
"""
    autocomplete_js = """
<script>
(function(){
  var inp=document.getElementById('symbols');
  var box=document.getElementById('sym-suggest');
  var timer=null;
  var _stockMap = {};
  window._stockMap = _stockMap;  // 立即暴露，让板块 JS 可以直接读写
  function lastTok(v){ var p=v.split(/[,\\s]+/); return p[p.length-1]||''; }
  function hl(text,q){
    if(!q) return text;
    var i=(''+text).toLowerCase().indexOf((''+q).toLowerCase());
    if(i<0) return text;
    return text.slice(0,i)+'<b>'+text.slice(i,i+q.length)+'</b>'+text.slice(i+q.length);
  }
  function render(items,q){
    if(!items||!items.length){ box.style.display='none'; return; }
    box.innerHTML=items.map(function(it){
      _stockMap[it.name]=it.symbol; window._stockMap=_stockMap;
      return '<div class="sg" data-sym="'+it.symbol+'" data-name="'+it.name+'">'
        +'<span class="n">'+hl(it.name,q)+'</span>'
        +'<span class="c">'+it.symbol+'</span></div>';
    }).join('');
    box.style.display='block';
  }
  function search(q){
    if(!q){ box.style.display='none'; return; }
    fetch('/api/search?q='+encodeURIComponent(q)).then(function(r){return r.json();})
      .then(function(d){ render(d.items||[], q); })
      .catch(function(){ box.style.display='none'; });
  }
  inp.addEventListener('input', function(){
    clearTimeout(timer); var q=lastTok(inp.value).trim();
    timer=setTimeout(function(){ search(q); }, 200);
  });
  box.addEventListener('click', function(e){
    var el=e.target.closest('.sg'); if(!el) return;
    var sym=el.getAttribute('data-sym');
    var name=el.getAttribute('data-name')||sym;
    _stockMap[name]=sym;
    var arr=inp.value.split(/[,\\s]+/).filter(Boolean);
    if(!arr.length) arr.push(name); else arr[arr.length-1]=name;
    inp.value=arr.join(', ')+', ';
    if(typeof window.updateHidden==="function") window.updateHidden();
    box.style.display='none'; inp.focus();
  });
  document.addEventListener('click', function(e){
    if(box.style.display!=='none' && !box.contains(e.target) && e.target!==inp) box.style.display='none';
  });
  function updateHidden(){
    var names=inp.value.split(/[,\\s]+/).filter(Boolean);
    var syms=names.map(function(n){ return _stockMap[n]||n; });
    document.getElementById('real-symbols').value=syms.join(',');
    document.getElementById('real-names').value=names.join(',');
  }
  document.querySelector('form').addEventListener('submit',function(e){
    if(typeof window.updateHidden==="function") window.updateHidden();
    var v=document.getElementById('real-symbols').value.trim();
    if(!v){ e.preventDefault(); alert('请先选择至少一只股票'); return; }
  });
  window.updateHidden = updateHidden;
})();
</script>
"""

    sector_js = """
var _sectorSelections={};
function loadSectorStocks(code){
  var p=document.getElementById('sector-stocks');
  if(!code){ p.innerHTML=''; return; }
  p.innerHTML='<div class="empty">加载中…</div>';
  fetch('/api/sector-stocks?code='+encodeURIComponent(code))
    .then(function(r){return r.json();})
    .then(function(d){
      var s=d.stocks||[];
      if(!s.length||s[0].code=='--'){ p.innerHTML='<div class="empty">'+(s[0]?s[0].name:'暂无数据')+'</div>'; return; }
      var h=s.map(function(x){
        var c=_sectorSelections[x.code]?' on':'';
        return '<span class="chip'+c+'" data-code="'+x.code+'" data-name="'+x.name+'" onclick="toggleChip(this)">'+x.name+'</span>';
      }).join('');
      p.innerHTML=h;
    }).catch(function(){ p.innerHTML='<div class="empty">加载失败</div>'; });
}
function toggleChip(el){
  var c=el.getAttribute('data-code'),n=el.getAttribute('data-name');
  if(_sectorSelections[c]){ delete _sectorSelections[c]; el.classList.remove('on'); }
  else{
    if(Object.keys(_sectorSelections).length>=10){ alert('最多选择10个标的'); return; }
    _sectorSelections[c]=n; el.classList.add('on');
  }
  syncSectorSelections();
}
function syncSectorSelections(){
  var inp=document.getElementById('symbols');
  var sectorNames=Object.values(_sectorSelections);
  var ex=inp.value.split(/[,\\s]+/).filter(Boolean);
  var seen={}; sectorNames.forEach(function(n){ seen[n]=true; });
  var names=ex.filter(function(n){ return !seen[n]; }).concat(sectorNames);
  inp.value=names.join(', ')+', ';
  Object.keys(_sectorSelections).forEach(function(c){
    window._stockMap[_sectorSelections[c]]=c;
  });
  if(typeof window.updateHidden==="function") window.updateHidden();
}
document.getElementById('sector-sel').addEventListener('change',function(){
  Object.keys(_sectorSelections).forEach(function(k){delete _sectorSelections[k];});
  loadSectorStocks(this.value);
});
function clearSectorSelections(){
  Object.keys(_sectorSelections).forEach(function(k){delete _sectorSelections[k];});
  var inp=document.getElementById('symbols');
  inp.value='';
  document.getElementById('real-symbols').value='';
  document.getElementById('real-names').value='';
  var sel=document.getElementById('sector-sel');
  if(sel.value) loadSectorStocks(sel.value);
}
"""

    return f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>量化回测服务</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family: system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
         background: linear-gradient(160deg,#0f172a,#1e293b); color:#e2e8f0; min-height:100vh; }}
  .wrap {{ max-width: 760px; margin: 0 auto; padding: 48px 20px 64px; }}
  h1 {{ font-size: 26px; margin: 0 0 6px; font-weight: 700; letter-spacing:.5px; }}
  .sub {{ color:#94a3b8; margin: 0 0 28px; font-size: 14px; }}
  .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 14px; padding: 26px 24px;
          box-shadow: 0 10px 30px rgba(0,0,0,.35); }}
  label {{ display:block; font-size: 13px; color:#cbd5e1; margin: 18px 0 8px; font-weight:600; }}
  label:first-child {{ margin-top: 0; }}
  input, select, textarea {{ width:100%; padding: 11px 13px; border-radius:9px; border:1px solid #475569;
          background:#0f172a; color:#e2e8f0; font-size:14px; outline:none; }}
  input:focus, select:focus, textarea:focus {{ border-color:#1f6feb; }}
  .hint {{ font-size:12px; color:#64748b; margin-top:6px; }}
  .row {{ display:flex; gap:14px; }}
  .row > div {{ flex:1; }}
  button {{ margin-top: 26px; width:100%; padding: 13px; border:0; border-radius:10px; cursor:pointer;
           background: linear-gradient(90deg,#1f6feb,#3b82f6); color:#fff; font-size:15px; font-weight:700;
           letter-spacing:1px; transition:.15s; }}
  button:hover {{ filter:brightness(1.08); }}
  .foot {{ margin-top:22px; font-size:12px; color:#64748b; line-height:1.7; }}
  code {{ background:#0f172a; padding:1px 6px; border-radius:5px; color:#93c5fd; }}
  .date-shortcuts {{ margin-top:10px; display:flex; align-items:center; gap:6px; font-size:12px; }}
  .shortcut-label {{ color:#64748b; white-space:nowrap; }}
  .shortcut-btn {{ width:auto; padding:4px 14px; margin:0; font-size:12px; font-weight:500;
    background:#334155; border-radius:6px; cursor:pointer; letter-spacing:0; }}
  .shortcut-btn:hover {{ background:#475569; }}
  .shortcut-btn.active {{ background:#1f6feb; }}
  .layout {{ display: flex; min-height: 100vh; }}
  .sidebar {{ width: 260px; min-width: 260px; background: #1a2332; border-right: 1px solid #334155; padding: 18px 14px;
    overflow-y: auto; max-height: 100vh; position: sticky; top: 0; }}
  .sidebar h2 {{ font-size: 15px; margin: 0 0 10px; color: #e2e8f0; }}
  .sidebar .s-refresh {{ width: auto; padding: 4px 12px; margin: 0 0 14px; font-size: 11px; background: #334155; border-radius: 5px; cursor: pointer; border: 0; color: #cbd5e1; }}
  .sidebar .group {{ margin-bottom: 14px; }}
  .sidebar .gname {{ font-size: 13px; color: #93c5fd; font-weight: 600; margin-bottom: 3px; }}
  .sidebar .rec {{ font-size: 11px; color: #94a3b8; padding: 2px 6px; cursor: pointer; border-radius: 3px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: flex; justify-content: space-between; }}
  .sidebar .rec:hover {{ background: #2d3a4a; color: #e2e8f0; }}
  .sidebar .rval {{ font-size: 11px; }}
  .sidebar .rval.pos {{ color: #4ade80; }}
  .sidebar .rval.neg {{ color: #f87171; }}
  .sidebar .empty {{ color: #64748b; font-size: 11px; }}
  .sidebar .gtime {{ color:#64748b; font-size:11px; margin:0 0 6px; padding-left:2px; }}
  .main {{ flex: 1; min-width: 0; }}
  .chip-panel {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; max-height: 220px; min-height: 40px; overflow-y: auto; border: 1px dashed #334155; border-radius: 8px; padding: 8px; }}
  .chip-panel .chip {{ font-size: 12px; padding: 4px 10px; border-radius: 14px; cursor: pointer; border: 1px solid #475569; background: #0f172a; color: #94a3b8; transition: .15s; user-select: none; }}
  .chip-panel .chip:hover {{ border-color: #1f6feb; color: #e2e8f0; }}
  .chip-panel .chip.on {{ background: #1f6feb; border-color: #1f6feb; color: #fff; }}
  .chip-panel .empty {{ color: #64748b; font-size: 12px; padding: 6px 0; }}
{extra_css}</style>
</head>
<body>
<div class="layout">
  <aside class="sidebar">
    <h2>📋 回测历史</h2>
    <button class="s-refresh" onclick="loadSidebar()">刷新</button>
    <div id="sidebar-list"><div class="empty">加载中…</div></div>
  </aside>
  <div class="main">
  <div class="wrap">
    <h1>📈 量化回测服务</h1>
    <p class="sub">选择标的 · 选择日期 · 选择策略 → 一键回测</p>
    <div class="card">
      <form method="post" action="/run">
        <label for="symbols">标的（输入名称或代码搜索，空格/逗号分隔多选）</label>
        <div id="symbols-wrap">
          <input id="symbols" autocomplete="off" name=""
                 placeholder="输入股票名称搜索，点击结果填入"
                 value="">
          <input type="hidden" id="real-symbols" name="symbols" value="">
          <input type="hidden" id="real-names" name="names" value="">
          <div id="sym-suggest"></div>
        </div>
        <div class="hint">键入关键词实时搜索；点击候选填入名称，多标的逗号/空格分隔。</div>

        <label>按板块选股 <span style="font-weight:400;color:#64748b;">（选择板块后勾选标的，最多10个）</span></label>
        <select id="sector-sel">
          <option value="">-- 按板块选股 --</option>
{sector_opts}
        </select>
        <button type="button" id="clear-sector-btn" style="margin-left:8px;padding:4px 12px;font-size:12px;background:#475569;color:#cbd5e1;border:1px solid #64748b;border-radius:6px;cursor:pointer;" onclick="clearSectorSelections()">✕ 全清</button>
        <div id="sector-stocks" class="chip-panel"><span class="empty">👆 请先在上方选择一个板块</span></div>

        <div class="row">
          <div>
            <label for="start">评估起始日期</label>
            <input type="date" id="start" name="start" value="2023-07-18">
          </div>
          <div>
            <label for="end">评估结束日期</label>
            <input type="date" id="end" name="end" value="{today}">
          </div>
        </div>
        <div class="date-shortcuts">
          <span class="shortcut-label">快捷区间：</span>
          <button type="button" class="shortcut-btn" onclick="setDateRange(1,this)">最近 1 年</button>
          <button type="button" class="shortcut-btn" onclick="setDateRange(3,this)">最近 3 年</button>
          <button type="button" class="shortcut-btn" onclick="setDateRange(5,this)">最近 5 年</button>
        </div>

        <label for="strategy">策略</label>
        <select id="strategy" name="strategy">
{strat_opts}
        </select>
        <div class="hint">策略来自包内置 <code>strategylab.resources.strategies/</code>（默认「周线MACD + 日线KDJ 双入口做T」）；自定义策略放 <code>STRATEGALAB_STRATEGIES_DIR</code> 目录即可，无需改包。</div>

        <button type="submit">运行回测</button>
      </form>
    </div>
    <p class="foot">
      本服务复用本地回测引擎（周线MACD + 日线KDJ 双入口做T）。首次对某标的取数会联网拉取前复权行情并缓存，
      后续直接复用。<br>
      命令行等效用法：<code>strategylab --symbols 600216.SH 300765.SZ --start 2023-07-18 --end {today}</code>
    </p>
  </div>
  </div>
</div>
{autocomplete_js}
<script>
function fmt(v,s){{if(v==null)return'--';var p=v>=0?'+':'';return p+v.toFixed(2)+s;}}
function loadSidebar(){{fetch('/api/runs?limit=100').then(function(r){{return r.json();}}).then(function(d){{
  var runs=d.runs||[];
  var byStrat={{}};
  runs.forEach(function(r){{
    var sk=r.strategy_name||'未命名策略';
    if(!byStrat[sk]){{ byStrat[sk]={{runs:[],repStart:null,repEnd:null}}; }}
    byStrat[sk].runs.push(r);
    if(byStrat[sk].repStart===null){{ byStrat[sk].repStart=r.start; byStrat[sk].repEnd=r.end; }}
  }});
  var h='';
  var keys=Object.keys(byStrat).sort();
  if(!keys.length){{ h='<div class=empty>暂无历史</div>'; }}
  keys.forEach(function(sk){{
    var g=byStrat[sk];
    var time=(g.repStart&&g.repEnd)?(g.repStart+' ~ '+g.repEnd):'';
    h+='<div class="group"><div class="gname">'+sk+'</div>';
    if(time){{ h+='<div class="gtime">'+time+'</div>'; }}
    g.runs.forEach(function(r){{
      var nm=r.symbol_name||r.symbol||'—';
      var c=(r.total_return_pct||0)>=0?'pos':'neg';
      h+='<div class="rec" onclick="location.href=\\'/history\\'" title="'+(r.start||'')+' ~ '+(r.end||'')+'">'
        +'<span>'+nm+'</span>'
        +'<span class="rval '+c+'">'+fmt(r.total_return_pct,'%')+'</span></div>';
    }});
    h+='</div>';
  }});
  document.getElementById('sidebar-list').innerHTML=h;
}}).catch(function(){{document.getElementById('sidebar-list').innerHTML='<div class=empty>加载失败</div>';}});}}
window.addEventListener('DOMContentLoaded',loadSidebar);
{sector_js}

function setDateRange(years, btn) {{
  var now = new Date();
  var start = new Date(now);
  start.setFullYear(now.getFullYear() - years);
  document.getElementById('start').value = start.toISOString().slice(0, 10);
  document.getElementById('end').value = now.toISOString().slice(0, 10);
  document.querySelectorAll('.shortcut-btn').forEach(function(b){{ b.classList.remove('active'); }});
  btn.classList.add('active');
}}
</script>
</body>
</html>
"""


def build_history_html() -> str:
    """回测历史页：多选列表（调 /api/runs）+ 一键跨回测对比（POST /compare）。"""
    return """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>回测历史 · 跨回测对比</title>
<style>
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
         background:linear-gradient(160deg,#0f172a,#1e293b); color:#e2e8f0; min-height:100vh; }
  .wrap { max-width:1000px; margin:0 auto; padding:40px 20px 64px; }
  h1 { font-size:24px; margin:0 0 4px; }
  .sub { color:#94a3b8; margin:0 0 20px; font-size:14px; }
  .bar { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-bottom:16px; }
  input, button { padding:9px 12px; border-radius:9px; border:1px solid #475569; background:#0f172a; color:#e2e8f0; font-size:14px; }
  button { background:linear-gradient(90deg,#1f6feb,#3b82f6); border:0; font-weight:700; cursor:pointer; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .list { background:#1e293b; border:1px solid #334155; border-radius:12px; overflow:hidden; }
  .row { display:grid; grid-template-columns:34px 1.5fr 1.3fr 1.5fr 1fr 1fr; gap:10px; padding:11px 14px; border-bottom:1px solid #15203a; align-items:center; font-size:13px; }
  .row.head { background:#172033; color:#93c5fd; font-weight:700; }
  .row:hover { background:#172033; }
  .tag { color:#64748b; font-size:12px; }
  .empty { padding:30px; text-align:center; color:#64748b; }
  code { background:#0f172a; padding:1px 6px; border-radius:5px; color:#93c5fd; font-size:12px; }
  a { color:#93c5fd; }
</style>
</head>
<body>
  <div class="wrap">
    <h1>🗂️ 回测历史</h1>
    <p class="sub">勾选多个回测结果进行「跨回测对比」：权益曲线按各 run 起点 rebased 到 100 叠加，指标表保留原始 %。</p>
    <div class="bar">
      <input id="q-symbol" placeholder="按标的名称过滤（留空=全部）">
      <input id="q-limit" placeholder="条数" value="100" style="width:90px">
      <button id="btn-refresh">刷新</button>
      <button id="btn-compare" disabled>对比选中（<span id="cnt">0</span>）</button>
    </div>
    <div class="list" id="list">
      <div class="row head"><div></div><div>标的</div><div>策略</div><div>区间</div><div>总收益</div><div>Sharpe</div></div>
      <div class="empty">加载中…</div>
    </div>
    <p class="sub" style="margin-top:18px"><a href="/">← 返回新建回测</a></p>
  </div>
<script>
function fmt(v,s){ if(v===null||v===undefined||v==='') return '--'; return Number(v).toFixed(2)+(s||''); }
function load(){
  var sym=document.getElementById('q-symbol').value.trim();
  var lim=document.getElementById('q-limit').value.trim()||100;
  var qs='?limit='+encodeURIComponent(lim); if(sym) qs+='&symbol='+encodeURIComponent(sym);
  fetch('/api/runs'+qs).then(function(r){return r.json();}).then(function(d){render(d.runs||[]);})
    .catch(function(){ document.getElementById('list').innerHTML='<div class="row head"><div></div><div>标的</div><div>策略</div><div>区间</div><div>总收益</div><div>Sharpe</div></div><div class="empty">加载失败</div>'; });
}
function render(runs){
  var box=document.getElementById('list');
  if(!runs.length){ box.innerHTML='<div class="row head"><div></div><div>标的</div><div>策略</div><div>区间</div><div>总收益</div><div>Sharpe</div></div><div class="empty">暂无回测历史</div>'; return; }
  var html='<div class="row head"><div></div><div>标的</div><div>策略</div><div>区间</div><div>总收益</div><div>Sharpe</div></div>';
  runs.forEach(function(r){
    html+='<label class="row"><div><input type="checkbox" class="cb" value="'+r.run_id+'"></div>'
      +'<div>'+r.symbol_name+'</div>'
      +'<div>'+r.strategy_name+'</div>'
      +'<div class="tag">'+r.start+'~'+r.end+'</div>'
      +'<div>'+fmt(r.total_return_pct,'%')+'</div>'
      +'<div>'+fmt(r.sharpe)+'</div></label>';
  });
  box.innerHTML=html;
  Array.prototype.forEach.call(document.querySelectorAll('.cb'),function(cb){ cb.addEventListener('change',update); });
}
function update(){ var n=document.querySelectorAll('.cb:checked').length; document.getElementById('cnt').textContent=n; document.getElementById('btn-compare').disabled=n<1; }
function compare(){
  var ids=Array.prototype.map.call(document.querySelectorAll('.cb:checked'),function(cb){return cb.value;});
  if(!ids.length) return;
  var fd=new FormData(); ids.forEach(function(id){ fd.append('run_ids', id); });
  fetch('/compare',{method:'POST',body:fd}).then(function(r){return r.text();}).then(function(h){ document.open(); document.write(h); document.close(); });
}
document.getElementById('btn-refresh').addEventListener('click',load);
document.getElementById('btn-compare').addEventListener('click',compare);
load();
</script>
</body>
</html>"""


def error_page(msg: str) -> str:
    safe = (msg or "").replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>回测出错</title>
<style>body{{font-family:system-ui;padding:40px;color:#333;background:#f8fafc}}
.err{{background:#fff3f3;border:1px solid #ffd0d0;padding:16px;border-radius:8px;
white-space:pre-wrap;font-family:monospace;font-size:13px}} a{{color:#1f6feb}}</style></head>
<body><h2>⚠️ 回测出错</h2><div class="err">{safe}</div>
<p><a href="/">← 返回重新选择</a></p></body></html>"""


# --------------------------------------------------------------------------
# HTTP 处理
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send_html(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: dict, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_404(self):
        self._send_html("<h1>404</h1><p><a href='/'>返回首页</a></p>", status=404)

    def _api_sector_stocks(self):
        import json as _json
        q = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(q)
        code = (params.get("code", [""])[0] or "").strip()
        if not code:
            self._send_json({"stocks": [], "error": "missing code"})
            return
        # 读缓存
        cache_path = "data/sector_stocks.json"
        cache = {}
        try:
            from pathlib import Path
            if Path(cache_path).exists():
                with open(cache_path, "r", encoding="utf-8") as fh:
                    cache = _json.load(fh)
        except Exception:
            pass
        stocks = cache.get(code, [])
        if not stocks:
            stocks = [{"code":"--","name":"板块数据暂未缓存，请先通过管理端拉取"}]
        self._send_json({"stocks": stocks})

    def _api_search(self):
        q = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(q)
        kw = (params.get("q", [""])[0] or "").strip()
        items = search_a_stocks(kw, limit=30) if kw else []
        self._send_json({"items": items})

    def _api_runs(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        symbol = (params.get("symbol", [""])[0] or "").strip() or None
        strategy = (params.get("strategy", [""])[0] or "").strip() or None
        limit_raw = (params.get("limit", [""])[0] or "").strip()
        limit = int(limit_raw) if limit_raw and limit_raw.isdigit() else None
        runs = storage_repo.list_runs(symbol=symbol, strategy=strategy, limit=limit)
        self._send_json({"runs": runs, "count": len(runs)})

    def _api_run_detail(self, run_id: str):
        run_id = (run_id or "").strip()
        if not run_id:
            self._send_404()
            return
        run = storage_repo.get_run(run_id)
        if run is None:
            self._send_json({"error": "not found"}, status=404)
            return
        self._send_json(run)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_html(build_form_html())
        elif self.path.startswith("/api/sector-stocks"):
            self._api_sector_stocks()
        elif self.path.startswith("/api/search"):
            self._api_search()
        elif self.path.startswith("/api/runs/"):
            self._api_run_detail(self.path[len("/api/runs/"):])
        elif self.path.startswith("/api/runs"):
            self._api_runs()
        elif self.path == "/history":
            self._send_html(build_history_html())
        else:
            self._send_404()

    def _handle_run(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        data = urllib.parse.parse_qs(raw)
        symbols_raw = (data.get("symbols", [""])[0] or "").strip()
        symbols = [s for s in re.split(r"[,\s]+", symbols_raw) if s]
        # 防御：过滤掉含中文的「伪代码」（说明前端 _stockMap 映射失败）
        if any(re.search(r"[\u4e00-\u9fff]", s) for s in symbols):
            raise ValueError("标的代码包含中文，请通过搜索框或板块面板重新选股。提示：选中板块芯片后务必确认已点「运行回测」前页面刷新完毕。")
        names_raw = (data.get("names", [""])[0] or "").strip()
        names = [n for n in re.split(r"[,\s]+", names_raw) if n]
        start = (data.get("start", ["2023-07-18"])[0] or "2023-07-18").strip()
        end = (data.get("end", [""])[0] or "").strip() or datetime.date.today().strftime("%Y-%m-%d")
        strategy = (data.get("strategy", ["kdj_macd_dual_entry.toml"])[0] or "kdj_macd_dual_entry.toml").strip()
        if not symbols:
            raise ValueError("请至少输入一个标的（代码或名称）。")
        logger.info("[run] symbols=%s start=%s end=%s strategy=%s", symbols, start, end, strategy)
        html = run_backtest(symbols, start, end, strategy, names=names)
        return html

    def _handle_compare(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        ctype = self.headers.get("Content-Type", "")
        if "application/json" in ctype:
            data = json.loads(raw)
            run_ids = list(data.get("run_ids", []))
        else:
            data = urllib.parse.parse_qs(raw)
            run_ids = data.get("run_ids", [])
        if len(run_ids) < 1:
            raise ValueError("请至少选择 1 个回测结果进行对比。")
        logger.info("[compare] run_ids=%s", run_ids)
        report = build_compare_from_runs(run_ids)
        # render_dashboard 需要输出路径；写到系统临时文件后读回 HTML 字符串
        import tempfile as _tempfile

        fd, tmp_path = _tempfile.mkstemp(suffix=".html")
        os.close(fd)
        rendered = render_dashboard(report, output_path=tmp_path)
        html = Path(rendered).read_text(encoding="utf-8")
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return html

    def do_POST(self):
        if self.path == "/run":
            try:
                html = self._handle_run()
                self._send_html(inject_back_button(html))
            except Exception as e:  # noqa: BLE001
                import traceback
                logger.error("[error] %s", traceback.format_exc())
                self._send_html(error_page(f"{type(e).__name__}: {e}"), status=500)
        elif self.path == "/compare":
            try:
                html = self._handle_compare()
                self._send_html(inject_back_button(html))
            except Exception as e:  # noqa: BLE001
                import traceback
                logger.error("[error] %s", traceback.format_exc())
                self._send_html(error_page(f"{type(e).__name__}: {e}"), status=500)
        else:
            self._send_404()

    def log_message(self, fmt, *args):  # 静默默认访问日志
        return


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    logger.info("量化回测服务已启动 → http://localhost:%s (Ctrl+C 停止)", PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
