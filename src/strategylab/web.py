# -*- coding: utf-8 -*-
"""量化回测 Web 服务（零依赖，仅用 Python 标准库）。

浏览器打开 http://localhost:8000 后：
  - 标的输入框支持「联网实时模糊搜索」
  - 选择「评估起止日期」
  - 选择「策略」
  - 点「运行回测」→ 后端跑同一套 engine，返回「策略对比」排版仪表盘

v2.0 新增（批量扫描 + 数据仓库化）：
  - /production  数据生产页（批量提交 + 进度 + 板块总览 + 历史批次）
  - /analysis    分析查询页（排名表 + 点行进详情）
  - /api/batch*  批次提交 / 进度 / 取消 / 列表 / 全市场预热
  - /api/rank    排名查询（命中复用 + 分页 + 排序）
  - /api/sector-status  跨批次板块状态总览
  - 顶部导航栏（四页共存）；/history 支持 ?run_id= 直接打开单 run 详情

实现：标准库 http.server + ThreadingHTTPServer。
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
from .engine import batch_runner
from .engine.storage import repository as storage_repo
from .engine.storage.repository import compute_params_hash
from .settings import get_data_dir

logger = logging.getLogger(__name__)

PORT = int(os.environ.get("PORT", "8000"))


# --------------------------------------------------------------------------
# 顶部导航（所有页面统一注入）
# --------------------------------------------------------------------------
NAV_HTML = """
<nav class="topnav">
  <span class="brand">📊 Strategy Lab</span>
  <a href="/">首页 / 快速回测</a>
  <a href="/production">数据生产</a>
  <a href="/analysis">分析查询</a>
  <a href="/history">回测历史</a>
</nav>
"""

NAV_CSS = """
.topnav { display:flex; align-items:center; gap:6px; padding:10px 18px; background:#0f172a; border-bottom:1px solid #334155; position:sticky; top:0; z-index:200; flex-wrap:wrap; }
.topnav .brand { color:#fbbf24; font-weight:700; margin-right:12px; font-size:15px; }
.topnav a { color:#cbd5e1; text-decoration:none; padding:7px 13px; border-radius:7px; font-size:13px; }
.topnav a:hover { background:#1e293b; color:#fff; }
"""


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
    """跑回测，写 index.html（渲染视图）到 data/ 目录，返回其 HTML 文本。"""
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
# 页面：首页 / 快速回测
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
  window._stockMap = _stockMap;
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
  .sidebar .rval.pos {{ color: #f87171; }}
  .sidebar .rval.neg {{ color: #4ade80; }}
  .sidebar .empty {{ color: #64748b; font-size: 11px; }}
  .sidebar .gtime {{ color:#64748b; font-size:11px; margin:0 0 6px; padding-left:2px; }}
  .main {{ flex: 1; min-width: 0; }}
  .chip-panel {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; max-height: 220px; min-height: 40px; overflow-y: auto; border: 1px dashed #334155; border-radius: 8px; padding: 8px; }}
  .chip-panel .chip {{ font-size: 12px; padding: 4px 10px; border-radius: 14px; cursor: pointer; border: 1px solid #475569; background: #0f172a; color: #94a3b8; transition: .15s; user-select: none; }}
  .chip-panel .chip:hover {{ border-color: #1f6feb; color: #e2e8f0; }}
  .chip-panel .chip.on {{ background: #1f6feb; border-color: #1f6feb; color: #fff; }}
  .chip-panel .empty {{ color: #64748b; font-size: 12px; padding: 6px 0; }}
{extra_css}
{NAV_CSS}</style>
</head>
<body>
{NAV_HTML}
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
    html = """<!doctype html>
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
  .pos { color:#f87171; }
  .neg { color:#4ade80; }
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
    <p class="sub" style="margin-top:18px"><a href="/">← 返回新建回测</a> · <a href="/production">数据生产</a> · <a href="/analysis">分析查询</a></p>
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
      +'<div class="'+(r.total_return_pct>=0?'pos':'neg')+'">'+fmt(r.total_return_pct,'%')+'</div>'
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
    return (
        html.replace("<body>", "<body>\n" + NAV_HTML, 1)
        .replace("</style>", NAV_CSS + "\n</style>", 1)
    )


def build_production_html() -> str:
    """数据生产页 /production：表单 + 进度 + 板块总览 + 历史批次。无日期选择器。"""
    strat_opts = "\n".join(
        f'          <option value="{fname}">{name}</option>' for name, fname in list_strategy_options()
    )
    import json as _json, pathlib
    _sec_path = pathlib.Path("data/sectors.json")
    _sectors = _json.loads(_sec_path.read_text(encoding="utf-8")) if _sec_path.exists() else []
    sector_opts = "\n".join(
        f'            <option value="{s["code"]}">{s["name"]}</option>' for s in _sectors
    )

    template = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>数据生产 · Strategy Lab</title>
<style>
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
         background:linear-gradient(160deg,#0f172a,#1e293b); color:#e2e8f0; min-height:100vh; }
  .wrap { max-width:880px; margin:0 auto; padding:28px 20px 64px; }
  h1 { font-size:24px; margin:0 0 4px; }
  .sub { color:#94a3b8; margin:0 0 18px; font-size:14px; }
  .card { background:#1e293b; border:1px solid #334155; border-radius:14px; padding:22px 22px; margin-bottom:18px;
          box-shadow:0 10px 30px rgba(0,0,0,.35); }
  label { display:block; font-size:13px; color:#cbd5e1; margin:16px 0 8px; font-weight:600; }
  input, select, textarea { width:100%; padding:11px 13px; border-radius:9px; border:1px solid #475569;
          background:#0f172a; color:#e2e8f0; font-size:14px; outline:none; }
  input:focus, select:focus, textarea:focus { border-color:#1f6feb; }
  button { margin-top:16px; padding:12px 16px; border:0; border-radius:10px; cursor:pointer;
           background:linear-gradient(90deg,#1f6feb,#3b82f6); color:#fff; font-size:14px; font-weight:700; }
  button:hover { filter:brightness(1.08); }
  button.ghost { background:#334155; font-weight:500; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .hint { font-size:12px; color:#64748b; margin-top:6px; }
  .scope-row { display:flex; gap:18px; align-items:center; margin:10px 0; }
  .scope-row label { margin:0; font-weight:500; }
  .prog { margin-top:14px; }
  .bar-bg { height:18px; background:#0f172a; border-radius:9px; overflow:hidden; border:1px solid #334155; }
  .bar-fg { height:100%; width:0%; background:linear-gradient(90deg,#22c55e,#3b82f6); transition:width .4s; }
  .stats { display:flex; gap:18px; flex-wrap:wrap; margin-top:12px; font-size:13px; }
  .stats b { color:#fbbf24; }
  .sectors { display:flex; flex-wrap:wrap; gap:6px 14px; margin-top:10px; }
  .sec { font-size:12px; color:#cbd5e1; }
  .btab { width:100%; border-collapse:collapse; font-size:12px; margin-top:8px; }
  .btab th, .btab td { border-bottom:1px solid #1e293b; padding:7px 8px; text-align:left; }
  .btab th { color:#93c5fd; }
  .empty { color:#64748b; font-size:13px; padding:8px 0; }
  details { background:#1e293b; border:1px solid #334155; border-radius:12px; padding:12px 16px; margin-bottom:18px; }
  summary { cursor:pointer; font-weight:600; color:#e2e8f0; }
  code { background:#0f172a; padding:1px 6px; border-radius:5px; color:#93c5fd; font-size:12px; }
  .note { background:#0f172a; border:1px solid #334155; border-radius:9px; padding:10px 12px; margin-top:12px; font-size:13px; color:#cbd5e1; }
__NAV_CSS__</style>
</head>
<body>
__NAV_HTML__
<div class="wrap">
  <h1>🏭 数据生产</h1>
  <p class="sub">选策略 + 选范围（板块 / 自定义池 / 全市场），一键批量扫描。回测区间固定 <code>2020-01-01 ~ 今天</code>。</p>

  <div class="card">
    <label for="strategy">策略</label>
    <select id="strategy">
__STRAT_OPTS__
    </select>

    <div class="scope-row">
      <label><input type="radio" name="scope" value="sector" checked> 按板块</label>
      <label><input type="radio" name="scope" value="pool"> 自定义池</label>
      <label><input type="radio" name="scope" value="all_market"> 全市场</label>
    </div>

    <div id="scope-sector">
      <label for="sector-multi">选择板块（点击选中，按住 Ctrl 可多选）</label>
      <select id="sector-multi" multiple size="8">
__SECTOR_OPTS__
      </select>
      <div class="hint">所选板块的全部成分股将纳入本次批量扫描；跨板块去重。若未看到板块列表，请确认服务已正确启动。</div>
    </div>

    <div id="scope-pool" style="display:none">
      <label for="pool-symbols">自定义池（代码逗号/空格分隔，如 <code>600216.SH,000001.SZ</code>）</label>
      <textarea id="pool-symbols" rows="4" placeholder="600216.SH, 000001.SZ"></textarea>
    </div>

    <button type="button" onclick="updateData()">💾 更新数据源</button>
    <button type="button" class="ghost" onclick="runBacktest()">🚀 回测</button>
  </div>

  <div class="card" id="batch-info" style="display:none">
    <h3 style="margin:0 0 4px">批次进度</h3>
    <div class="hint">批次 ID：<code id="batch-id"></code> · 命中复用 <b id="batch-hit">0</b> / 共 <b id="batch-total">0</b> 只</div>
    <div class="prog">
      <div class="bar-bg"><div class="bar-fg" id="prog-bar"></div></div>
      <div class="stats">
        <span>进度：<b id="prog-pct">0%</b></span>
        <span>已完成：<b id="prog-done">0</b></span>
        <span>失败：<b id="prog-failed">0</b></span>
        <span>跳过(复用)：<b id="prog-skipped">0</b></span>
        <span>总数：<b id="prog-total">0</b></span>
        <span>当前标的：<b id="prog-current">—</b></span>
        <span>状态：<b id="prog-status">—</b></span>
        <span>预计剩余：<b id="prog-eta">—</b></span>
      </div>
    </div>
    <button type="button" class="ghost" id="cancel-btn" onclick="cancelBatch()">取消批次</button>
    <div class="note" id="prog-done-msg" style="display:none">
      ✅ 批次已完成。前往 <a href="/analysis">分析查询页</a> 查看收益排名。
    </div>
    <div class="note" id="reinit-note" style="display:none">
      ⚠️ 该批次因服务重启被标记为 <b>interrupted</b>。重新提交相同策略+范围即可自动复用已完成部分。
    </div>
  </div>

  <div class="card">
    <h3 style="margin:0 0 6px">板块完成总览</h3>
    <div class="hint">跨批次（策略×板块维度）：✅ 已完成 · ⏳ 进行中/部分 · — 未跑</div>
    <div class="sectors" id="sector-overview"><span class="empty">加载中…</span></div>
  </div>

  <details>
    <summary>📦 历史批次（点击展开）</summary>
    <div id="batch-history"><span class="empty">加载中…</span></div>
  </details>
</div>
<script>
var curBatchId=null, pollTimer=null, curBatchType='backtest';
function fmtEta(s){ if(s==null) return '—'; s=Math.round(s); var m=Math.floor(s/60); var sec=s%60; return (m>0?m+'分':'')+sec+'秒'; }
function switchScope(){
  var scope=document.querySelector('input[name=scope]:checked').value;
  document.getElementById('scope-sector').style.display = scope==='sector'?'block':'none';
  document.getElementById('scope-pool').style.display = scope==='pool'?'block':'none';
}
Array.prototype.forEach.call(document.querySelectorAll('input[name=scope]'),function(r){ r.addEventListener('change',switchScope); });

function buildScopeForm(){
  var fd=new FormData();
  fd.append('strategy', document.getElementById('strategy').value);
  var scope=document.querySelector('input[name=scope]:checked').value;
  if(scope==='sector'){
    var sel=document.getElementById('sector-multi');
    var codes=Array.prototype.map.call(sel.selectedOptions,function(o){return o.value;});
    if(!codes.length){ alert('请至少选择一个板块'); return null; }
    fd.append('scope_type','sector'); fd.append('scope_value', codes.join(','));
  } else if(scope==='pool'){
    var sym=document.getElementById('pool-symbols').value.trim();
    if(!sym){ alert('请填写自定义池标的'); return null; }
    fd.append('scope_type','pool'); fd.append('symbols', sym);
  } else if(scope==='all_market'){
    fd.append('scope_type','all_market'); fd.append('scope_value','ALL');
  } else { return null; }
  return fd;
}
function updateData(){
  var fd=buildScopeForm(); if(!fd) return;
  fetch('/api/data/update',{method:'POST',body:fd}).then(function(r){return r.json();}).then(function(d){
    if(d.error){ alert(d.error); return; }
    beginBatch(d);
  }).catch(function(e){ alert('更新失败: '+e); });
}
function runBacktest(){
  var fd=buildScopeForm(); if(!fd) return;
  startBatch(fd);
}
function startBatch(fd){
  fetch('/api/batch',{method:'POST',body:fd}).then(function(r){return r.json();}).then(function(d){
    if(d.error){ alert(d.error); return; }
    beginBatch(d);
  }).catch(function(e){ alert('提交失败: '+e); });
}
function beginBatch(d){
  curBatchId=d.batch_id;
  curBatchType=d.batch_type||'backtest';
  document.getElementById('batch-info').style.display='block';
  document.getElementById('batch-id').textContent=d.batch_id;
  document.getElementById('batch-total').textContent=d.total_count;
  document.getElementById('batch-hit').textContent=d.hit_count;
  document.getElementById('prog-done-msg').style.display='none';
  document.getElementById('reinit-note').style.display='none';
  poll(); pollTimer=setInterval(poll,2000);
}
function poll(){
  if(!curBatchId) return;
  fetch('/api/batch/'+curBatchId+'/progress').then(function(r){return r.json();}).then(function(p){
    var done=p.done, failed=p.failed, skipped=p.skipped, total=p.total;
    var finished=done+failed+skipped;
    var pct = total>0 ? Math.round(finished/total*100) : 0;
    document.getElementById('prog-bar').style.width=pct+'%';
    document.getElementById('prog-pct').textContent=pct+'%';
    document.getElementById('prog-done').textContent=done;
    document.getElementById('prog-failed').textContent=failed;
    document.getElementById('prog-skipped').textContent=skipped;
    document.getElementById('prog-total').textContent=total;
    document.getElementById('prog-current').textContent=p.current_symbol||'—';
    document.getElementById('prog-status').textContent=p.status;
    document.getElementById('prog-eta').textContent=fmtEta(p.eta_seconds);
    if(['done','cancelled','interrupted'].indexOf(p.status)>=0){
      clearInterval(pollTimer); pollTimer=null;
      var dm=document.getElementById('prog-done-msg');
      if(curBatchType==='data'){
        dm.innerHTML='✅ 行情已更新，可前往 <a href="/analysis">分析查询页</a> 或重新『回测』。';
      } else {
        dm.innerHTML='✅ 批次已完成。前往 <a href="/analysis">分析查询页</a> 查看收益排名。';
      }
      dm.style.display='block';
      if(p.status==='interrupted'){ document.getElementById('reinit-note').style.display='block'; }
      loadBatches();
    }
  }).catch(function(){});
}
var _cancelling=false;
function cancelBatch(){
  if(!curBatchId || _cancelling) return;
  _cancelling=true;
  var btn=document.getElementById('cancel-btn');
  if(btn){ btn.disabled=true; btn.textContent='取消中...'; }
  clearInterval(pollTimer); pollTimer=null;
  fetch('/api/batch/'+curBatchId+'/cancel',{method:'POST'}).then(function(){
    var st=document.getElementById('prog-status'); if(st){ st.textContent='cancelling'; }
    var dm=document.getElementById('prog-done-msg');
    if(dm){ dm.innerHTML='✅ 取消请求已发送，正在停止中...'; dm.style.display='block'; }
  }).catch(function(){
    _cancelling=false;
    if(btn){ btn.disabled=false; btn.textContent='取消批次'; }
  });
}
function loadSectorStatus(){
  var strat=document.getElementById('strategy').value;
  fetch('/api/sector-status?strategy='+encodeURIComponent(strat)).then(function(r){return r.json();}).then(function(d){
    var box=document.getElementById('sector-overview');
    var secs=d.sectors||[];
    if(!secs.length){ box.innerHTML='<span class="empty">暂无板块数据</span>'; return; }
    box.innerHTML=secs.map(function(s){
      var icon = s.status==='done'?'✅':(s.status==='partial'?'⏳':'—');
      return '<span class="sec" title="'+s.name+'：done='+s.done+'/total='+s.total+'">'+icon+' '+s.name+'</span>';
    }).join('');
  }).catch(function(){ document.getElementById('sector-overview').innerHTML='<span class="empty">加载失败</span>'; });
}
function loadBatches(){
  fetch('/api/batch').then(function(r){return r.json();}).then(function(d){
    var box=document.getElementById('batch-history');
    var bs=d.batches||[];
    if(!bs.length){ box.innerHTML='<span class="empty">暂无批次</span>'; return; }
    var h='<table class="btab"><tr><th>批次</th><th>策略</th><th>范围</th><th>状态</th><th>完成</th><th>创建</th></tr>';
    h+=bs.map(function(b){
      return '<tr><td>'+b.batch_id.slice(0,8)+'</td><td>'+b.strategy_name+'</td><td>'+b.scope_type+'</td><td>'+b.status+'</td><td>'+b.done_count+'/'+b.total_count+'</td><td>'+((b.created_at||'').slice(0,19))+'</td></tr>';
    }).join('')+'</table>';
    box.innerHTML=h;
  }).catch(function(){ document.getElementById('batch-history').innerHTML='<span class="empty">加载失败</span>'; });
}
document.getElementById('strategy').addEventListener('change', loadSectorStatus);
window.addEventListener('DOMContentLoaded', function(){ switchScope(); loadSectorStatus(); loadBatches(); });
</script>
</body>
</html>"""
    return (
        template.replace("__STRAT_OPTS__", strat_opts)
        .replace("__SECTOR_OPTS__", sector_opts)
        .replace("__NAV_HTML__", NAV_HTML)
        .replace("__NAV_CSS__", NAV_CSS)
    )


def build_analysis_html() -> str:
    """分析查询页 /analysis：表单 + 排名表 + 点行进详情。无日期选择器。"""
    strat_opts = "\n".join(
        f'          <option value="{fname}">{name}</option>' for name, fname in list_strategy_options()
    )
    import json as _json, pathlib
    _sec_path = pathlib.Path("data/sectors.json")
    _sectors = _json.loads(_sec_path.read_text(encoding="utf-8")) if _sec_path.exists() else []
    scope_opts = '<option value="">全部已跑过的</option>\n' + "\n".join(
        f'          <option value="{s["code"]}">{s["name"]}</option>' for s in _sectors
    )

    template = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>分析查询 · Strategy Lab</title>
<style>
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
         background:linear-gradient(160deg,#0f172a,#1e293b); color:#e2e8f0; min-height:100vh; }
  .wrap { max-width:980px; margin:0 auto; padding:28px 20px 64px; }
  h1 { font-size:24px; margin:0 0 4px; }
  .sub { color:#94a3b8; margin:0 0 18px; font-size:14px; }
  .card { background:#1e293b; border:1px solid #334155; border-radius:14px; padding:22px 22px; margin-bottom:18px;
          box-shadow:0 10px 30px rgba(0,0,0,.35); }
  label { display:block; font-size:13px; color:#cbd5e1; margin:14px 0 8px; font-weight:600; }
  input, select, textarea { width:100%; padding:11px 13px; border-radius:9px; border:1px solid #475569;
          background:#0f172a; color:#e2e8f0; font-size:14px; outline:none; }
  input:focus, select:focus, textarea:focus { border-color:#1f6feb; }
  button { margin-top:14px; padding:10px 16px; border:0; border-radius:9px; cursor:pointer;
           background:linear-gradient(90deg,#1f6feb,#3b82f6); color:#fff; font-size:14px; font-weight:700; }
  button:hover { filter:brightness(1.08); }
  button.ghost { background:#334155; font-weight:500; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .hint { font-size:12px; color:#64748b; margin-top:6px; }
  .summary { font-size:14px; margin-bottom:10px; }
  .summary a { color:#93c5fd; }
  .rtab { width:100%; border-collapse:collapse; font-size:13px; }
  .rtab th, .rtab td { border-bottom:1px solid #1e293b; padding:10px 10px; text-align:left; cursor:pointer; }
  .rtab th { color:#93c5fd; cursor:default; }
  .rtab th.sortable { cursor:pointer; user-select:none; white-space:nowrap; }
  .rtab th.sortable:hover { color:#bfdbfe; text-decoration:underline; }
  .rtab th.sorted { color:#fbbf24; }
  .rtab tr:hover { background:#172033; }
  .rtab .pos { color:#f87171; }
  .rtab .neg { color:#4ade80; }
  .stale { background:#7c2d12; color:#fdba74; font-size:11px; padding:2px 7px; border-radius:5px; }
  .pager { display:flex; gap:12px; align-items:center; margin-top:14px; }
  .empty { color:#64748b; font-size:13px; padding:20px 0; text-align:center; }
  code { background:#0f172a; padding:1px 6px; border-radius:5px; color:#93c5fd; font-size:12px; }
__NAV_CSS__</style>
</head>
<body>
__NAV_HTML__
<div class="wrap">
  <h1>🔍 分析查询</h1>
  <p class="sub">选策略 + 选范围，查看已落库回测的收益排名（按总收益率降序）。点行查看单标的详情仪表盘。</p>

  <div class="card">
    <label for="strategy">策略</label>
    <select id="strategy">
__STRAT_OPTS__
    </select>
    <label for="scope-sel">范围</label>
    <select id="scope-sel">
__SCOPE_OPTS__
    </select>
    <label for="pool-symbols">自定义池（可选，代码逗号/空格分隔；填写后优先于上方范围）</label>
    <textarea id="pool-symbols" rows="3" placeholder="600216.SH, 000001.SZ"></textarea>
    <button type="button" onclick="loadRank(1)">查询排名</button>
    <button type="button" class="ghost" onclick="exportCsv()">⬇ 导出 CSV</button>
  </div>

  <div class="card">
    <div class="summary" id="rank-summary">—</div>
    <div id="rank-table"><span class="empty">请先查询</span></div>
    <div class="pager" id="rank-pager"></div>
  </div>
</div>
<script>
var curStrategy='', curScope='', curSymbols='';
var curSortBy='total_return_pct', curSortDir='desc';
function loadRank(page){
  if(page==null||isNaN(page)||page<1) page=1;
  curStrategy=document.getElementById('strategy').value;
  curScope=document.getElementById('scope-sel').value;
  curSymbols=document.getElementById('pool-symbols').value.trim();
  var qs='strategy='+encodeURIComponent(curStrategy)
    +'&page='+page+'&size=50'
    +'&sort_by='+encodeURIComponent(curSortBy)+'&order='+encodeURIComponent(curSortDir);
  if(curScope) qs+='&scope='+encodeURIComponent(curScope);
  if(curSymbols) qs+='&symbols='+encodeURIComponent(curSymbols);
  fetch('/api/rank?'+qs).then(function(r){return r.json();}).then(function(d){
    var items=d.items||[], total=d.total||0, miss=d.miss_count, warmup=d.warmup||null;
    var stratSel=document.getElementById('strategy');
    var stratName=(stratSel && stratSel.selectedIndex>=0 ? stratSel.options[stratSel.selectedIndex].text : '') || curStrategy;
    document.getElementById('rank-summary').innerHTML = '共 <b>'+total+'</b> 只已跑过'
      + (miss!=null ? '，还有 <b>'+miss+'</b> 只未跑（<a href="/production">去数据生产页发起</a>）' : '');
    var box=document.getElementById('rank-table');
    var pager=document.getElementById('rank-pager');
    if(!items || !items.length){
      if(warmup && warmup.in_progress){
        box.innerHTML='<div class="empty">「'+stratName+'」的预热仍在进行中（已完成 '+warmup.done+'/'+warmup.total+'），数据入库后刷新本页即可查看；若预热已结束仍无数据，请到「<a href="/production">数据生产</a>」页重新发起全市场预热。</div>';
      } else {
        box.innerHTML='<div class="empty">暂无已跑过的回测（请先在数据生产页发起）</div>';
      }
      pager.innerHTML=''; return;
    }
    var h='<table class="rtab">'+headerHtml();
    h+=items.map(rowHtml).join('');
    h+='</table>';
    box.innerHTML=h;
    var pages=Math.max(1, Math.ceil(total/50));
    pager.innerHTML='<button type="button" onclick="loadRank('+(page-1)+')" '+(page<=1?'disabled':'')+'>上一页</button>'
      +' <span>第 '+page+' / '+pages+' 页</span> '
      +'<button type="button" onclick="loadRank('+(page+1)+')" '+(page>=pages?'disabled':'')+'>下一页</button>';
  }).catch(function(){ document.getElementById('rank-table').innerHTML='<div class="empty">加载失败</div>'; });
}
function sortBy(col){
  if(col===curSortBy){ curSortDir=(curSortDir==='desc'?'asc':'desc'); }
  else { curSortBy=col; curSortDir=(col==='symbol_name'?'asc':'desc'); }
  loadRank(1);
}
function headerHtml(){
  var cols=[['symbol_name','股票名称',true],['total_return_pct','总收益率',true],['max_drawdown_pct','最大回撤',true],['sharpe','夏普',true],['win_rate_pct','胜率',true],['last_buy_date','最近买入日期',true],['','数据时效',false]];
  return '<tr>'+cols.map(function(c){
    var key=c[0], label=c[1], sortable=c[2];
    if(!sortable) return '<th>'+label+'</th>';
    var ind=(key===curSortBy)?(curSortDir==='asc'?' ▲':' ▼'):'';
    var cls=' class="sortable'+(key===curSortBy?' sorted':'')+'"';
    return '<th'+cls+' data-col="'+key+'" onclick="sortBy(this.dataset.col)">'+label+ind+'</th>';
  }).join('')+'</tr>';
}
function rowHtml(it){
  var tr=it.total_return_pct==null?'—':(it.total_return_pct>=0?'+':'')+Number(it.total_return_pct).toFixed(2)+'%';
  var cls=(it.total_return_pct!=null && it.total_return_pct>=0)?'pos':'neg';
  var dd=it.max_drawdown_pct==null?'—':Number(it.max_drawdown_pct).toFixed(2)+'%';
  var sh=it.sharpe==null?'—':Number(it.sharpe).toFixed(2);
  var wr=it.win_rate_pct==null?'—':Number(it.win_rate_pct).toFixed(2)+'%';
  var lbd=it.last_buy_date==null?'—':it.last_buy_date;
  var stale=it.stale?'<span class="stale">数据较旧，建议点「更新数据源」刷新行情后再重跑</span>':'';
  return '<tr data-rid="'+it.run_id+'" onclick="openRun(this.dataset.rid)"><td>'+it.symbol_name+'</td><td class="'+cls+'">'+tr+'</td><td>'+dd+'</td><td>'+sh+'</td><td>'+wr+'</td><td>'+lbd+'</td><td>'+stale+'</td></tr>';
}
function openRun(rid){ window.open('/history?run_id='+rid); }
function exportCsv(){
  var qs='strategy='+encodeURIComponent(curStrategy)+'&export=csv'
    +'&sort_by='+encodeURIComponent(curSortBy)+'&order='+encodeURIComponent(curSortDir);
  if(curScope) qs+='&scope='+encodeURIComponent(curScope);
  if(curSymbols) qs+='&symbols='+encodeURIComponent(curSymbols);
  window.open('/api/rank?'+qs);
}
document.getElementById('strategy').addEventListener('change',function(){ loadRank(1); });
document.getElementById('scope-sel').addEventListener('change',function(){ loadRank(1); });
document.getElementById('pool-symbols').addEventListener('change',function(){ loadRank(1); });
window.addEventListener('DOMContentLoaded', function(){ loadRank(1); });
</script>
</body>
</html>"""
    return (
        template.replace("__STRAT_OPTS__", strat_opts)
        .replace("__SCOPE_OPTS__", scope_opts)
        .replace("__NAV_HTML__", NAV_HTML)
        .replace("__NAV_CSS__", NAV_CSS)
    )


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
        self.send_header("Cache-Control", "no-store")
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

    def _send_csv(self, csv_text: str, filename: str):
        body = ("\ufeff" + csv_text).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header(
            "Content-Disposition", f'attachment; filename="{filename}"'
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_404(self):
        self._send_html("<h1>404</h1><p><a href='/'>返回首页</a></p>", status=404)

    # ---- API 辅助 ----
    def _api_sector_stocks(self):
        import json as _json
        from pathlib import Path

        q = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(q)
        code = (params.get("code", [""])[0] or "").strip()
        if not code:
            self._send_json({"stocks": [], "error": "missing code"})
            return
        cache_path = "data/sector_stocks.json"
        cache = {}
        try:
            if Path(cache_path).exists():
                with open(cache_path, "r", encoding="utf-8") as fh:
                    cache = _json.load(fh)
        except Exception:
            pass
        stocks = cache.get(code, [])
        if not stocks:
            stocks = [{"code": "--", "name": "板块数据暂未缓存，请先通过管理端拉取"}]
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
        params_hash = (params.get("params_hash", [""])[0] or "").strip() or None
        limit_raw = (params.get("limit", [""])[0] or "").strip()
        limit = int(limit_raw) if limit_raw and limit_raw.isdigit() else None
        runs = storage_repo.list_runs(
            symbol=symbol, strategy=strategy, params_hash=params_hash, limit=limit
        )
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

    def _api_rank(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        strategy = (params.get("strategy", [""])[0] or "").strip()
        scope = (params.get("scope", [""])[0] or "").strip() or None
        ph = (params.get("params_hash", [""])[0] or "").strip() or None
        export = (params.get("export", [""])[0] or "").strip()
        sort_by = (params.get("sort_by", [""])[0] or "").strip() or None
        order_raw = (params.get("order", ["desc"])[0] or "desc").strip()
        order = order_raw if order_raw in ("asc", "desc") else "desc"
        try:
            page = int((params.get("page", ["1"])[0] or "1"))
        except ValueError:
            page = 1
        try:
            size = int((params.get("size", ["50"])[0] or "50"))
        except ValueError:
            size = 50
        if not strategy:
            self._send_json({"error": "missing strategy"}, status=400)
            return
        try:
            cfg = load_strategy_by_arg(strategy)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"策略加载失败: {e}"}, status=400)
            return
        strategy_name = cfg.get("name", "")
        if not ph:
            ph = compute_params_hash(cfg)
        symbols_raw = (params.get("symbols", [""])[0] or "").strip()
        symbols = (
            [s.strip() for s in re.split(r"[,\s]+", symbols_raw) if s.strip()]
            if symbols_raw
            else None
        )
        if export == "csv":
            data = storage_repo.rank_runs(
                strategy_name, ph, scope=scope, symbols=symbols, page=1, size=100000,
                sort_by=sort_by, order=order,
            )
            self._send_csv(_ranks_to_csv(data["items"]), f"rank_{strategy_name}.csv")
            return
        data = storage_repo.rank_runs(
            strategy_name, ph, scope=scope, symbols=symbols, page=page, size=size,
            sort_by=sort_by, order=order,
        )
        if data["total"] == 0:
            latest = storage_repo.latest_batch_for_strategy(strategy_name, ph)
            if latest is not None and (
                latest["status"] == "running"
                or (
                    latest["status"] in ("done", "cancelled", "interrupted")
                    and latest["done_count"] < latest["total_count"]
                )
            ):
                data["warmup"] = {
                    "in_progress": True,
                    "status": latest["status"],
                    "done": latest["done_count"],
                    "total": latest["total_count"],
                }
            else:
                data["warmup"] = None
        self._send_json(data)

    def _api_sector_status(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        strategy = (params.get("strategy", [""])[0] or "").strip()
        ph = (params.get("params_hash", [""])[0] or "").strip() or None
        if not strategy:
            self._send_json({"sectors": []})
            return
        try:
            cfg = load_strategy_by_arg(strategy)
        except Exception:  # noqa: BLE001
            self._send_json({"sectors": []})
            return
        strategy_name = cfg.get("name", "")
        if not ph:
            ph = compute_params_hash(cfg)
        sectors = storage_repo.sector_status(strategy_name, ph)
        self._send_json({"sectors": sectors})

    def _api_batch_list(self):
        self._send_json({"batches": storage_repo.list_batches()})

    def _api_batch_progress(self, batch_id: str):
        self._send_json(batch_runner.get_progress(batch_id))

    def _api_batch_sector_overview(self, batch_id: str):
        self._send_json({"sectors": storage_repo.batch_sector_status(batch_id)})

    def _api_submit_batch(self):
        data = self._parse_post_body()
        strategy = (data.get("strategy", ["kdj_macd_dual_entry"])[0] or "kdj_macd_dual_entry").strip() or "kdj_macd_dual_entry"
        scope_type = (data.get("scope_type", ["sector"])[0] or "sector").strip()
        scope_value = (data.get("scope_value", [""])[0] or "").strip()
        symbols_raw = (data.get("symbols", [""])[0] or "").strip()
        try:
            cfg = load_strategy_by_arg(strategy)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"策略加载失败: {e}"}, status=400)
            return
        strategy_name = cfg.get("name", "")
        ph = compute_params_hash(cfg)
        out_dir = get_data_dir()

        items: list[dict] = []
        if scope_type == "sector":
            codes = [c.strip() for c in scope_value.split(",") if c.strip()]
            stocks = storage_repo.get_sector_stocks()
            seen: set[str] = set()
            for code in codes:
                for st in stocks.get(code, []):
                    sym = st["code"]
                    if sym in seen:
                        continue
                    seen.add(sym)
                    items.append(
                        {"symbol": sym, "symbol_name": st.get("name", sym), "sector_code": code}
                    )
        elif scope_type == "pool":
            syms = [s.strip() for s in re.split(r"[,\s]+", symbols_raw) if s.strip()]
            for s in syms:
                items.append({"symbol": s, "symbol_name": s, "sector_code": None})
        elif scope_type == "all_market":
            items = _expand_all_market()
        else:
            self._send_json({"error": "未知 scope_type"}, status=400)
            return

        if not items:
            self._send_json({"error": "没有可执行的标的（请检查范围选择）"}, status=400)
            return

        batch_id, hit_count = batch_runner.submit_batch(
            cfg, strategy_name, ph, scope_type, scope_value, str(out_dir), items
        )
        self._send_json(
            {
                "batch_id": batch_id,
                "total_count": len(items),
                "hit_count": hit_count,
                "strategy_name": strategy_name,
                "params_hash": ph,
            }
        )

    def _api_warmup_all(self):
        data = self._parse_post_body()
        strategy = (data.get("strategy", ["kdj_macd_dual_entry"])[0] or "kdj_macd_dual_entry").strip() or "kdj_macd_dual_entry"
        try:
            cfg = load_strategy_by_arg(strategy)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"策略加载失败: {e}"}, status=400)
            return
        strategy_name = cfg.get("name", "")
        ph = compute_params_hash(cfg)
        out_dir = get_data_dir()
        items = _expand_all_market()
        if not items:
            self._send_json({"error": "无成分股数据（请先拉取板块缓存）"}, status=400)
            return
        batch_id, hit_count = batch_runner.submit_batch(
            cfg, strategy_name, ph, "all_market", "ALL", str(out_dir), items
        )
        self._send_json(
            {
                "batch_id": batch_id,
                "total_count": len(items),
                "hit_count": hit_count,
                "strategy_name": strategy_name,
                "params_hash": ph,
            }
        )

    def _api_data_update(self):
        """POST /api/data/update：更新数据源（data-only 批次，不回测、不落库 run）。"""
        data = self._parse_post_body()
        scope_type = (data.get("scope_type", ["sector"])[0] or "sector").strip()
        scope_value = (data.get("scope_value", [""])[0] or "").strip()
        symbols_raw = (data.get("symbols", [""])[0] or "").strip()

        items: list[dict] = []
        if scope_type == "sector":
            codes = [c.strip() for c in scope_value.split(",") if c.strip()]
            stocks = storage_repo.get_sector_stocks()
            seen: set[str] = set()
            for code in codes:
                for st in stocks.get(code, []):
                    sym = st["code"]
                    if sym in seen:
                        continue
                    seen.add(sym)
                    items.append(
                        {"symbol": sym, "symbol_name": st.get("name", sym), "sector_code": code}
                    )
        elif scope_type == "pool":
            syms = [s.strip() for s in re.split(r"[,\s]+", symbols_raw) if s.strip()]
            for s in syms:
                items.append({"symbol": s, "symbol_name": s, "sector_code": None})
        elif scope_type == "all_market":
            items = _expand_all_market()
        else:
            self._send_json({"error": "未知 scope_type"}, status=400)
            return

        if not items:
            self._send_json({"error": "没有可更新的标的（请检查范围选择）"}, status=400)
            return

        out_dir = get_data_dir()
        batch_id, hit_count = batch_runner.submit_data_batch(
            scope_type, scope_value, str(out_dir), items
        )
        self._send_json(
            {
                "batch_id": batch_id,
                "total_count": len(items),
                "hit_count": hit_count,
                "batch_type": "data",
                "strategy_name": "行情更新",
                "params_hash": "__data__",
            }
        )

    def _api_cancel_batch(self, batch_id: str):
        batch_runner.cancel_batch(batch_id)
        self._send_json({"ok": True})

    def _render_single_run(self, run_id: str):
        try:
            report = build_compare_from_runs([run_id])
        except Exception as e:  # noqa: BLE001
            self._send_html(error_page(f"{type(e).__name__}: {e}"), status=500)
            return
        import tempfile as _tf

        fd, tmp = _tf.mkstemp(suffix=".html")
        os.close(fd)
        rendered = render_dashboard(report, output_path=tmp)
        html = Path(rendered).read_text(encoding="utf-8")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        self._send_html(inject_back_button(html))

    # ---- 路由 ----
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_html(build_form_html())
        elif path == "/production":
            self._send_html(build_production_html())
        elif path == "/analysis":
            self._send_html(build_analysis_html())
        elif path.startswith("/api/sector-status"):
            self._api_sector_status()
        elif path.startswith("/api/sector-stocks"):
            self._api_sector_stocks()
        elif path.startswith("/api/search"):
            self._api_search()
        elif path.startswith("/api/runs/"):
            self._api_run_detail(self.path[len("/api/runs/"):].split("?")[0])
        elif path.startswith("/api/runs"):
            self._api_runs()
        elif path.startswith("/api/rank"):
            self._api_rank()
        elif path.startswith("/api/batch/"):
            rest = path[len("/api/batch/"):]
            if "/progress" in rest:
                self._api_batch_progress(rest.split("/")[0])
            elif "/sector-overview" in rest:
                self._api_batch_sector_overview(rest.split("/")[0])
            else:
                self._send_404()
        elif path == "/api/batch":
            self._api_batch_list()
        elif path == "/history":
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            run_id = (params.get("run_id", [""])[0] or "").strip()
            if run_id:
                self._render_single_run(run_id)
            else:
                self._send_html(build_history_html())
        else:
            self._send_404()

    def _handle_run(self):
        data = self._parse_post_body()
        symbols_raw = (data.get("symbols", [""])[0] or "").strip()
        symbols = [s for s in re.split(r"[,\s]+", symbols_raw) if s]
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
        raw = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        if "application/json" in ctype:
            data = json.loads(raw)
            run_ids = list(data.get("run_ids", []))
        else:
            data = self._parse_post_body(raw=raw, ctype=ctype)
            run_ids = data.get("run_ids", [])
        if len(run_ids) < 1:
            raise ValueError("请至少选择 1 个回测结果进行对比。")
        logger.info("[compare] run_ids=%s", run_ids)
        report = build_compare_from_runs(run_ids)
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

    def _parse_post_body(self, raw: bytes | None = None, ctype: str | None = None) -> dict[str, list[str]]:
        """统一解析 POST body，兼容 urlencoded 与 multipart/form-data 两种编码。

        返回结构与 ``urllib.parse.parse_qs`` 一致：``dict[str, list[str]]``，
        以便现有 ``data.get(key, [default])[0]`` 用法无需改动。
        """
        if raw is None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
        if ctype is None:
            ctype = self.headers.get("Content-Type", "")
        ctype_lower = ctype.lower()
        if "application/x-www-form-urlencoded" in ctype_lower:
            try:
                return urllib.parse.parse_qs(raw.decode("utf-8"))
            except UnicodeDecodeError:
                return {}
        if "multipart/form-data" in ctype_lower:
            return self._parse_multipart(raw, ctype)
        # 兜底：尝试按 urlencoded 解析
        try:
            return urllib.parse.parse_qs(raw.decode("utf-8"))
        except UnicodeDecodeError:
            return {}

    def _parse_multipart(self, raw: bytes, ctype: str) -> dict[str, list[str]]:
        """解析 multipart/form-data 纯文本表单字段（无文件上传）。

        本应用所有字段均为简单文本，故只做最小实现。
        禁止使用已移除的 ``cgi`` 模块（Python 3.13+ 不可用）。
        """
        m = re.search(r'boundary=("?)([^";]+)\1', ctype)
        if not m:
            return {}
        boundary = m.group(2).encode("utf-8")
        result: dict[str, list[str]] = {}
        for part in raw.split(b"--" + boundary):
            if not part or part in (b"--", b"--\r\n"):
                continue
            # 段首可能带 \r\n（首个分隔符之后的部分）
            if part.startswith(b"\r\n"):
                part = part[2:]
            # 头部与内容体以 \r\n\r\n 分隔
            if b"\r\n\r\n" not in part:
                continue
            head, _, body = part.partition(b"\r\n\r\n")
            name_m = re.search(r'(?:^|;)\s*name="([^"]*)"', head.decode("utf-8", "replace"))
            if not name_m:
                # 无 name 的字段（如仅含 filename 的文件域）直接跳过
                continue
            name = name_m.group(1)
            # 内容体结尾通常带 \r\n，需去除
            if body.endswith(b"\r\n"):
                body = body[:-2]
            result.setdefault(name, []).append(body.decode("utf-8", "replace"))
        return result

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/run":
            try:
                html = self._handle_run()
                self._send_html(inject_back_button(html))
            except Exception as e:  # noqa: BLE001
                import traceback
                logger.error("[error] %s", traceback.format_exc())
                self._send_html(error_page(f"{type(e).__name__}: {e}"), status=500)
        elif path == "/compare":
            try:
                html = self._handle_compare()
                self._send_html(inject_back_button(html))
            except Exception as e:  # noqa: BLE001
                import traceback
                logger.error("[error] %s", traceback.format_exc())
                self._send_html(error_page(f"{type(e).__name__}: {e}"), status=500)
        elif path == "/api/batch/warmup-all":
            try:
                self._api_warmup_all()
            except Exception as e:  # noqa: BLE001
                import traceback
                logger.error("[error] %s", traceback.format_exc())
                self._send_json({"error": str(e)}, status=500)
        elif path == "/api/data/update":
            try:
                self._api_data_update()
            except Exception as e:  # noqa: BLE001
                import traceback
                logger.error("[error] %s", traceback.format_exc())
                self._send_json({"error": str(e)}, status=500)
        elif path == "/api/batch":
            try:
                self._api_submit_batch()
            except Exception as e:  # noqa: BLE001
                import traceback
                logger.error("[error] %s", traceback.format_exc())
                self._send_json({"error": str(e)}, status=500)
        elif path.startswith("/api/batch/"):
            rest = path[len("/api/batch/"):]
            if "/cancel" in rest:
                try:
                    self._api_cancel_batch(rest.split("/")[0])
                except Exception as e:  # noqa: BLE001
                    self._send_json({"error": str(e)}, status=500)
            else:
                self._send_404()
        else:
            self._send_404()

    def log_message(self, fmt, *args):  # 静默默认访问日志
        return


def _ranks_to_csv(items: list[dict]) -> str:
    """把排名 items 转为 CSV 文本（含 BOM 供 Excel）。"""
    cols = ["run_id", "symbol", "symbol_name", "total_return_pct", "max_drawdown_pct", "sharpe", "last_buy_date", "end", "stale"]
    lines = [",".join(cols)]
    for it in items:
        row = [
            it.get("run_id", ""),
            it.get("symbol", ""),
            it.get("symbol_name", ""),
            "" if it.get("total_return_pct") is None else f"{it['total_return_pct']:.2f}",
            "" if it.get("max_drawdown_pct") is None else f"{it['max_drawdown_pct']:.2f}",
            "" if it.get("sharpe") is None else f"{it['sharpe']:.2f}",
            it.get("last_buy_date", "") or "",
            it.get("end", "") or "",
            "1" if it.get("stale") else "0",
        ]
        # CSV 简单转义（名称含逗号/引号时包裹）
        row = [f'"{c}"' if ("," in str(c) or '"' in str(c)) else str(c) for c in row]
        lines.append(",".join(row))
    return "\n".join(lines)


def _expand_all_market() -> list[dict]:
    """展开全部 31 板块成分股（去重，带 sector_code）。"""
    stocks = storage_repo.get_sector_stocks()
    items: list[dict] = []
    seen: set[str] = set()
    for code, lst in stocks.items():
        for st in lst:
            sym = st["code"]
            if sym in seen:
                continue
            seen.add(sym)
            items.append(
                {"symbol": sym, "symbol_name": st.get("name", sym), "sector_code": code}
            )
    return items


def main():
    from .engine.storage import db

    db.init_db()  # 触发 v1→v2 迁移（幂等）
    batch_runner.init_batch_runner()  # 重启处理：running → interrupted

    # 允许地址重用，避免服务快速重启时 TIME_WAIT 端口无法绑定
    ThreadingHTTPServer.allow_reuse_address = True
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
