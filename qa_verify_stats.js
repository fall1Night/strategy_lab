// QA 独立验证：从 web.py 抽出的 isNumVal + computeStrategyStats 纯逻辑（与源码逐字一致）
function isNumVal(v){ return v!==null && v!==undefined && !isNaN(Number(v)); }
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

function deepEq(a, b){
  return JSON.stringify(a) === JSON.stringify(b);
}

var results = [];
function check(name, got, expected){
  var ok = deepEq(got, expected);
  results.push({name:name, ok:ok, got:got, expected:expected});
  console.log((ok?'PASS':'FAIL') + ' | ' + name);
  console.log('   got     : ' + JSON.stringify(got));
  if(!ok) console.log('   expected: ' + JSON.stringify(expected));
}

// 用例 A：混合
var A = [{win_rate_pct:60, max_drawdown_pct:-8, sharpe:1.5},
         {win_rate_pct:40, max_drawdown_pct:-12, sharpe:0.8},
         {win_rate_pct:55, max_drawdown_pct:-18, sharpe:2.0},
         {win_rate_pct:null, max_drawdown_pct:-25, sharpe:null}];
check('A_mixed',
  computeStrategyStats(A),
  {winRate:{num:2, den:3}, drawdown:{num10:1, num15:2, num20:3, den:4}, sharpe:{num:2, den:3}});

// 用例 B：全空 + 边界
var B = [{win_rate_pct:null, max_drawdown_pct:null, sharpe:null},
         {win_rate_pct:50, max_drawdown_pct:10, sharpe:1}];
check('B_empty_boundary',
  computeStrategyStats(B),
  {winRate:{num:0, den:1}, drawdown:{num10:1, num15:1, num20:1, den:1}, sharpe:{num:0, den:1}});

// 用例 C：空数组
check('C_empty_array', computeStrategyStats([]),
  {winRate:{num:0, den:0}, drawdown:{num10:0, num15:0, num20:0, den:0}, sharpe:{num:0, den:0}});

// 用例 D：正数回撤 12 → abs 后按 12 处理（num10 不加、num15 加）
check('D_positive_drawdown',
  computeStrategyStats([{win_rate_pct:60, max_drawdown_pct:12, sharpe:1.2}]),
  {winRate:{num:1, den:1}, drawdown:{num10:0, num15:1, num20:1, den:1}, sharpe:{num:1, den:1}});

// 附加：NaN 值应被排除（isNumVal 语义）
check('E_NaN_excluded',
  computeStrategyStats([{win_rate_pct:NaN, max_drawdown_pct:5, sharpe:2}]),
  {winRate:{num:0, den:0}, drawdown:{num10:1, num15:1, num20:1, den:1}, sharpe:{num:1, den:1}});

// 附加：字符串数字（若 API 返回字符串）—— 记录行为，供参考
check('F_string_numeric',
  computeStrategyStats([{win_rate_pct:'60', max_drawdown_pct:'8', sharpe:'1.5'}]),
  {winRate:{num:1, den:1}, drawdown:{num10:1, num15:1, num20:1, den:1}, sharpe:{num:1, den:1}});

var failed = results.filter(function(r){ return !r.ok; });
console.log('\n===== SUMMARY =====');
console.log('total=' + results.length + ' passed=' + (results.length - failed.length) + ' failed=' + failed.length);
process.exit(failed.length ? 1 : 0);
