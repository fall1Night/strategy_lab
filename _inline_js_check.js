
var curStrategy='', curWinRateMin='', curProfitFactorMin='', curNameKw='';
// —— 组合排序（多键排序）核心状态：curSortRules 为 [{key,dir}, ...]，最多 3 条 ——
var SORT_FIELDS=[['symbol_name','股票名称'],['total_return_pct','总收益率'],['max_drawdown_pct','最大回撤'],['sharpe','夏普'],['win_rate_pct','胜率'],['profit_factor','盈亏比'],['last_open_date','最近建仓'],['last_t_buy_date','最近做T']];
var SORT_WHITELIST={'symbol_name':1,'total_return_pct':1,'max_drawdown_pct':1,'sharpe':1,'win_rate_pct':1,'profit_factor':1,'last_open_date':1,'last_t_buy_date':1};
var DEFAULT_SORT_RULES=[{key:'total_return_pct', dir:'desc'}];
var SORT_STORAGE_KEY='strategylab.rankSort.v1';

// 从 localStorage 读取持久化的排序组合；校验非法/越界则回退默认
function loadSortRules(){
  try{
    var raw=localStorage.getItem(SORT_STORAGE_KEY);
    if(!raw) return DEFAULT_SORT_RULES.slice();
    var arr=JSON.parse(raw);
    if(!Array.isArray(arr)) return DEFAULT_SORT_RULES.slice();
    var out=[];
    for(var i=0;i<arr.length && out.length<3;i++){
      var r=arr[i];
      if(r && SORT_WHITELIST[r.key] && (r.dir==='asc'||r.dir==='desc')){
        out.push({key:r.key, dir:r.dir});
      }
    }
    if(out.length<1 || out.length>3) return DEFAULT_SORT_RULES.slice();
    return out;
  }catch(e){ return DEFAULT_SORT_RULES.slice(); }
}
function saveSortRules(){
  try{ localStorage.setItem(SORT_STORAGE_KEY, JSON.stringify(curSortRules)); }catch(e){}
}
var curSortRules=loadSortRules();

// —— 排序面板：用 panelRules 作为面板内工作副本，apply 时才写入 curSortRules ——
var panelRules=[];
function toggleSortPanel(){
  var p=document.getElementById('sort-panel');
  if(p.style.display==='none' || !p.style.display){ openSortPanel(); } else { p.style.display='none'; }
}
function openSortPanel(){
  panelRules=curSortRules.map(function(r){ return {key:r.key, dir:r.dir}; });
  renderSortRules();
  document.getElementById('sort-panel').style.display='block';
}
function cancelSort(){ document.getElementById('sort-panel').style.display='none'; }
function applySort(){
  if(panelRules.length<1) panelRules=DEFAULT_SORT_RULES.slice();
  curSortRules=panelRules.slice(); saveSortRules();
  document.getElementById('sort-panel').style.display='none'; loadRank(1);
}
function resetSort(){
  curSortRules=DEFAULT_SORT_RULES.slice(); saveSortRules();
  document.getElementById('sort-panel').style.display='none'; loadRank(1);
}
function addSortKey(){
  if(panelRules.length>=3) return;
  var used={}; for(var i=0;i<panelRules.length;i++) used[panelRules[i].key]=1;
  var nk='symbol_name';
  for(var j=0;j<SORT_FIELDS.length;j++){ if(!used[SORT_FIELDS[j][0]]){ nk=SORT_FIELDS[j][0]; break; } }
  panelRules.push({key:nk, dir:(nk==='symbol_name'?'asc':'desc')});
  renderSortRules();
}
function moveSortKey(idx, delta){
  var ni=idx+delta; if(ni<0 || ni>=panelRules.length) return;
  var t=panelRules[idx]; panelRules[idx]=panelRules[ni]; panelRules[ni]=t;
  renderSortRules();
}
function delSortKey(idx){ panelRules.splice(idx,1); renderSortRules(); }
function renderSortRules(){
  var box=document.getElementById('sort-rules');
  if(!panelRules.length){
    box.innerHTML='<div class="hint">暂无排序键，点击下方按钮添加。</div>';
  } else {
    box.innerHTML=panelRules.map(function(r,i){
      var optF=SORT_FIELDS.map(function(f){ return '<option value="'+f[0]+'"'+(f[0]===r.key?' selected':'')+'>'+f[1]+'</option>'; }).join('');
      var optD='<option value="desc"'+(r.dir==='desc'?' selected':'')+'>降序 ▼</option>'
              +'<option value="asc"'+(r.dir==='asc'?' selected':'')+'>升序 ▲</option>';
      var upDisabled=(i===0)?'disabled':'';
      var dnDisabled=(i===panelRules.length-1)?'disabled':'';
      return '<div class="sort-rule">'
        +'<span class="pri">'+(i+1)+'</span>'
        +'<select onchange="panelRules['+i+'].key=this.value">'+optF+'</select>'
        +'<select onchange="panelRules['+i+'].dir=this.value">'+optD+'</select>'
        +'<button type="button" class="move" '+upDisabled+' onclick="moveSortKey('+i+',-1)">↑</button>'
        +'<button type="button" class="move" '+dnDisabled+' onclick="moveSortKey('+i+',1)">↓</button>'
        +'<button type="button" class="del" onclick="delSortKey('+i+')">✕</button>'
        +'</div>';
    }).join('');
  }
  var addBtn=document.getElementById('add-sort-key');
  if(addBtn) addBtn.disabled=(panelRules.length>=3);
}
function loadRank(page){
  if(page==null||isNaN(page)||page<1) page=1;
  curStrategy=document.getElementById('strategy').value;
  curWinRateMin=document.getElementById('win-rate-min').value.trim();
  curProfitFactorMin=document.getElementById('profit-factor-min').value.trim();
  curNameKw=document.getElementById('name-kw').value.trim();
  var qs='strategy='+encodeURIComponent(curStrategy)
    +'&page='+page+'&size=50'
    +'&sort_by='+encodeURIComponent(curSortRules.map(function(r){return r.key;}).join(','))
    +'&order='+encodeURIComponent(curSortRules.map(function(r){return r.dir;}).join(','));
  if(curWinRateMin!=='') qs+='&win_rate_min='+encodeURIComponent(curWinRateMin);
  if(curProfitFactorMin!=='') qs+='&profit_factor_min='+encodeURIComponent(curProfitFactorMin);
  if(curNameKw) qs+='&name_kw='+encodeURIComponent(curNameKw);
  fetch('/api/rank?'+qs).then(function(r){return r.json();}).then(function(d){
    var items=d.items||[], total=d.total||0, miss=d.miss_count, warmup=d.warmup||null;
    var stratSel=document.getElementById('strategy');
    var stratName=(stratSel && stratSel.selectedIndex>=0 ? stratSel.options[stratSel.selectedIndex].text : '') || curStrategy;
    document.getElementById('rank-summary').innerHTML = '共 <b>'+total+'</b> 只已跑过'
      + (miss!=null ? '，还有 <b>'+miss+'</b> 只未跑（<a href="/production">去数据生产页发起</a>）' : '');
    loadStats();
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
    // 表头固定：外层 .table-scroll 提供滚动容器，表头 thead th 吸顶
    var h='<div class="table-scroll"><table class="rtab">'+headerHtml();
    h+='<tbody>'+items.map(rowHtml).join('')+'</tbody>';
    h+='</table></div>';
    box.innerHTML=h;
    var pages=Math.max(1, Math.ceil(total/50));
    pager.innerHTML='<button type="button" onclick="loadRank('+(page-1)+')" '+(page<=1?'disabled':'')+'>上一页</button>'
      +' <span>第 '+page+' / '+pages+' 页</span> '
      +'<button type="button" onclick="loadRank('+(page+1)+')" '+(page>=pages?'disabled':'')+'>下一页</button>';
  }).catch(function(){ document.getElementById('rank-table').innerHTML='<div class="empty">加载失败</div>'; });
}
// —— 策略统计分析（基于 /api/rank 全量翻页拉取后在前端聚合；无新增后端 API）——
var STATS_PAGE_SIZE = 500;  // 与后端 rank_runs size 上限一致（repository 强制 max(1, min(500, size))）
var statsSeq = 0;           // 请求序号：并发/过期请求直接丢弃，避免旧结果覆盖新统计

// 数值有效性：非 null/undefined/NaN 才算有效（计入分母）
function isNumVal(v){ return v!==null && v!==undefined && !isNaN(Number(v)); }

// 单个统计项：百分比(1位小数) + 分子/分母 + 进度条 + 有效数据提示；分母为 0 时显示 "-"
function statItemHtml(label, num, den){
  var pct = '-', frac = '-', w = 0, hint = '无有效数据';
  if(den > 0){
    pct = (num/den*100).toFixed(1) + '%';
    frac = num + '/' + den;
    w = Math.max(0, Math.min(100, num/den*100));
    hint = '基于 ' + den + ' 个有效数据';
  }
  return '<div class="stat-item">'
    + '<div class="row"><span class="lbl">' + label + '</span>'
    + '<span class="val">' + pct + ' <span class="frac">(' + frac + ')</span></span></div>'
    + '<div class="bar"><div class="fill" style="width:' + w.toFixed(1) + '%"></div></div>'
    + '<div class="hint">' + hint + '</div>'
    + '</div>';
}

// 纯聚合计算（与 DOM 无关，便于独立验证）：胜率>50% / |回撤|≤10·15·20 / 夏普>1
function computeStrategyStats(items){
  var st = { winRate:{num:0, den:0}, drawdown:{num10:0, num15:0, num20:0, den:0}, sharpe:{num:0, den:0} };
  for(var i=0;i<items.length;i++){
    var it = items[i] || {};
    var wr = it.win_rate_pct;
    if(isNumVal(wr)){ st.winRate.den++; if(Number(wr) > 50) st.winRate.num++; }
    var dd = it.max_drawdown_pct;
    if(isNumVal(dd)){
      st.drawdown.den++;
      var a = Math.abs(Number(dd));  // DB 中 max_drawdown_pct 为正数（回撤幅度），abs 同时兼容两种符号
      if(a <= 10) st.drawdown.num10++;
      if(a <= 15) st.drawdown.num15++;
      if(a <= 20) st.drawdown.num20++;
    }
    var sh = it.sharpe;
    if(isNumVal(sh)){ st.sharpe.den++; if(Number(sh) > 1) st.sharpe.num++; }
  }
  return st;
}

// 渲染统计卡：items 为空 → 暂无回测数据；truncated 表示分页未取全
function renderStrategyStats(items, total, truncated){
  var box = document.getElementById('stats-body');
  var meta = document.getElementById('stats-meta');
  if(!box) return;
  if(!items || !items.length){
    if(meta) meta.innerHTML = '查询命中 <b>0</b> 只已跑股票';
    box.innerHTML = '<div class="empty">暂无回测数据</div>';
    return;
  }
  if(meta){
    meta.innerHTML = '查询命中 <b>' + total + '</b> 只已跑股票'
      + (truncated ? '（分页拉取未取全，统计基于已加载部分）' : '');
  }
  var st = computeStrategyStats(items);
  box.innerHTML =
      '<div class="stat-block"><h3>🏆 胜率分布</h3>'
    + statItemHtml('胜率 &gt; 50%', st.winRate.num, st.winRate.den)
    + '</div>'
    + '<div class="stat-block"><h3>📉 最大回撤分布（|最大回撤| 阈值）</h3>'
    + statItemHtml('|最大回撤| ≤ 10%', st.drawdown.num10, st.drawdown.den)
    + statItemHtml('|最大回撤| ≤ 15%', st.drawdown.num15, st.drawdown.den)
    + statItemHtml('|最大回撤| ≤ 20%', st.drawdown.num20, st.drawdown.den)
    + '</div>'
    + '<div class="stat-block"><h3>⚡ 夏普比分布</h3>'
    + statItemHtml('夏普比 &gt; 1', st.sharpe.num, st.sharpe.den)
    + '</div>';
}

// 全量拉取统计：与 loadRank 同查询条件，size=500 翻页直到取满 total（优先全量统计）
function loadStats(){
  var box = document.getElementById('stats-body');
  if(!box) return;
  var mySeq = ++statsSeq;
  box.innerHTML = '<span class="empty">统计计算中…</span>';
  var qs = 'strategy=' + encodeURIComponent(curStrategy) + '&size=' + STATS_PAGE_SIZE;
  if(curWinRateMin!=='') qs += '&win_rate_min=' + encodeURIComponent(curWinRateMin);
  if(curProfitFactorMin!=='') qs += '&profit_factor_min=' + encodeURIComponent(curProfitFactorMin);
  if(curNameKw) qs += '&name_kw=' + encodeURIComponent(curNameKw);
  var all = [];
  var page = 1;
  var next = function(){
    fetch('/api/rank?' + qs + '&page=' + page).then(function(r){ return r.json(); }).then(function(d){
      if(mySeq !== statsSeq) return;  // 过期请求丢弃
      var items = d.items || [];
      var total = d.total || 0;
      all = all.concat(items);
      if(!items.length || all.length >= total || page >= 200){
        renderStrategyStats(all, total, all.length < total);
        return;
      }
      page++;
      next();
    }).catch(function(){
      if(mySeq !== statsSeq) return;
      if(all.length){
        renderStrategyStats(all, all.length, true);
      } else {
        box.innerHTML = '<span class="empty">统计加载失败</span>';
      }
    });
  };
  next();
}
function sortBy(col){
  // 便捷单键排序（向后兼容）：与当前唯一规则相同则翻转方向；否则以该列重置为唯一规则
  if(curSortRules.length===1 && curSortRules[0].key===col){
    curSortRules[0].dir=(curSortRules[0].dir==='desc'?'asc':'desc');
  } else {
    curSortRules=[{key:col, dir:(col==='symbol_name'?'asc':'desc')}];
  }
  saveSortRules();
  loadRank(1);
}
function headerHtml(){
  var cols=[['symbol_name','股票名称',true],['total_return_pct','总收益率',true],['max_drawdown_pct','最大回撤',true],['sharpe','夏普',true],['win_rate_pct','胜率',true],['profit_factor','盈亏比',true],['last_open_date','最近建仓',true],['last_t_buy_date','最近做T',true],['','数据时效',false]];
  // 构建 key -> {priority, dir} 索引，用于在表头标注组合排序优先级
  var sortIdx={};
  for(var i=0;i<curSortRules.length;i++){ sortIdx[curSortRules[i].key]={pri:i+1, dir:curSortRules[i].dir}; }
  return '<thead><tr>'+cols.map(function(c){
    var key=c[0], label=c[1], sortable=c[2];
    if(!sortable) return '<th>'+label+'</th>';
    var info=sortIdx[key];
    var ind=info?(' '+info.pri+(info.dir==='asc'?'▲':'▼')):'';
    var cls=' class="sortable'+(info?' sorted':'')+'"';
    return '<th'+cls+' data-col="'+key+'" onclick="sortBy(this.dataset.col)">'+label+ind+'</th>';
  }).join('')+'</tr></thead>';
}
function rowHtml(it){
  var tr=it.total_return_pct==null?'—':(it.total_return_pct>=0?'+':'')+Number(it.total_return_pct).toFixed(2)+'%';
  var cls=(it.total_return_pct!=null && it.total_return_pct>=0)?'pos':'neg';
  var dd=it.max_drawdown_pct==null?'—':Number(it.max_drawdown_pct).toFixed(2)+'%';
  var sh=it.sharpe==null?'—':Number(it.sharpe).toFixed(2);
  var wr=it.win_rate_pct==null?'—':Number(it.win_rate_pct).toFixed(2)+'%';
  var pf=it.profit_factor==null?'—':Number(it.profit_factor).toFixed(2);
  var lod=it.last_open_date==null?'—':it.last_open_date;
  var ltd=it.last_t_buy_date==null?'—':it.last_t_buy_date;
  var stale=it.stale?'<span class="stale">数据较旧，建议点「更新数据源」刷新行情后再重跑</span>':'';
  return '<tr data-rid="'+it.run_id+'" onclick="openRun(this.dataset.rid)"><td>'+it.symbol_name+'</td><td class="'+cls+'">'+tr+'</td><td>'+dd+'</td><td>'+sh+'</td><td>'+wr+'</td><td>'+pf+'</td><td>'+lod+'</td><td>'+ltd+'</td><td>'+stale+'</td></tr>';
}
function openRun(rid){ window.open('/history?run_id='+rid); }
function exportCsv(){
  var qs='strategy='+encodeURIComponent(curStrategy)+'&export=csv'
    +'&sort_by='+encodeURIComponent(curSortRules.map(function(r){return r.key;}).join(','))
    +'&order='+encodeURIComponent(curSortRules.map(function(r){return r.dir;}).join(','));
  if(curWinRateMin!=='') qs+='&win_rate_min='+encodeURIComponent(curWinRateMin);
  if(curProfitFactorMin!=='') qs+='&profit_factor_min='+encodeURIComponent(curProfitFactorMin);
  if(curNameKw) qs+='&name_kw='+encodeURIComponent(curNameKw);
  window.open('/api/rank?'+qs);
}
function resetFilters(){
  document.getElementById('win-rate-min').value='';
  document.getElementById('profit-factor-min').value='';
  document.getElementById('name-kw').value='';
  curWinRateMin=''; curProfitFactorMin=''; curNameKw='';
  loadRank(1);
}
document.getElementById('strategy').addEventListener('change',function(){ loadRank(1); });
document.getElementById('win-rate-min').addEventListener('change',function(){ loadRank(1); });
document.getElementById('profit-factor-min').addEventListener('change',function(){ loadRank(1); });
document.getElementById('name-kw').addEventListener('change',function(){ loadRank(1); });
window.addEventListener('DOMContentLoaded', function(){ loadRank(1); });
