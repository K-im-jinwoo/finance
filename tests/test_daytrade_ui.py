"""Render the actual browser script with a small offline DOM; no network."""
from pathlib import Path
import json
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]


class DaytradeUiTests(unittest.TestCase):
    def test_money_spread_names_and_bounded_collapsible_history(self):
        source=(ROOT/'web/daytrade.js').read_text(encoding='utf-8')
        harness=r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const roots=new Map();
class Element {
 constructor(tag){this.tagName=tag;this.children=[];this.events={};this.value='';this.open=false;}
 set textContent(value){this.value=String(value);this.children=[];}
 get textContent(){return this.value+this.children.map(c=>c.textContent).join(' ');}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.value='';this.children=items;}
 setAttribute(){} addEventListener(type,fn){this.events[type]=fn;}
 hasChildNodes(){return this.children.length>0;}
}
const ctx=vm.createContext({document:{getElementById(id){if(!roots.has(id))roots.set(id,new Element('div'));return roots.get(id);},createElement:tag=>new Element(tag)},
 setInterval(){},fetch:()=>new Promise(()=>{})});
vm.runInContext(SOURCE,ctx);
const run=code=>vm.runInContext(code,ctx);
assert.equal(run("money('10003585.6384')"),'10,003,586원');
assert.equal(run("koreanUnit('10003585.6384')"),'1,000만 3,586원');
assert.equal(run("money('-1.5')"),'-2원');
assert.equal(run("money('3585.6',true)"),'+3,586원');
assert.equal(run('money(null)'),'확인 불가');
assert.equal(run("turnoverText('558040091500')"),'5,580억 원');
assert.equal(run("turnoverText('55800000')"),'5,580만 원');
assert.equal(run("spreadText('7.6775',false)"),'0.08% (7.7bps)');
assert.equal(run('spreadText(null,true)'),'호가 수신 대기');
const at=i=>new Date(Date.UTC(2026,10,16,0,0,i)).toISOString();
const selected=[{symbol:'005930',name:'005930',rank_turnover:1,rank_volume:2,turnover:'558040091500',spread_bps:'7.6775',selection_reasons:[]},
 {symbol:'114800',name:'114800',turnover:'55800000',spread_bps:null,selection_reasons:[]}];
const report={profile:'TEST',dataset_kind:'SYNTHETIC',mode:'OBSERVE_ONLY',paper_ready:true,sessions:{'2026-11-16':'OBSERVING'},symbol_names:{'005930':'삼성전자','114800':'KODEX 인버스'},
 current_risk:{phase:'OPEN',locks:['LOSS_LOCKED'],entry_allowed:false},account:{initial_capital:'10000000',cash:'10003585.6384',final_equity:'10003585.6384',net_pnl:'3585.6384',realized_pnl_net:'3585.6384'},
 scan_history:Array.from({length:301},(_,i)=>({at:at(i),selected,rejected:Array.from({length:95},()=>({symbol:'005930',reasons:['WATCH_LIMIT']}))})),
 fills:[{at:at(1),symbol:'114800',side:'BUY',quantity:1534,price:'1303.2606',fee:'400',tax:'0'}],
 signals:Array.from({length:80},(_,i)=>({at:at(i),symbol:'005930',action:'HOLD',score:0,reasons:[]})),risk_events:[],
 quality_events:Array.from({length:40},(_,i)=>({at:at(i),symbol:'005930',issues:['STALE_ORDERBOOK']})),decisions:[]};
ctx.fixture={present:true,report,markdown:'test'};run('render(fixture)');
const visit=(root,predicate)=>[...(predicate(root)?[root]:[]),...root.children.flatMap(c=>visit(c,predicate))];
const scans=roots.get('daytrade-scans'),signals=roots.get('daytrade-signals'),risk=roots.get('daytrade-risk');
assert.equal(visit(scans,e=>e.className==='daytrade-scan').length,1);
assert.ok(scans.textContent.includes('총 301회'));
assert.ok(scans.textContent.includes('삼성전자 (005930)'));
assert.ok(scans.textContent.includes('KODEX 인버스 (114800)'));
assert.ok(scans.textContent.includes('매수·매도 호가차'));
assert.ok(scans.textContent.includes('0.08% (7.7bps)'));
assert.ok(scans.textContent.includes('순위 외 제외'));
const disclosures=visit(scans,e=>e.tagName==='details');
assert.equal(disclosures.length,2);assert.ok(disclosures.every(d=>!d.open));
assert.equal(visit(signals,e=>e.className==='event').length,30);
assert.equal(visit(risk,e=>e.className==='event').length,15);
assert.ok(risk.textContent.includes('일손실 한도 잠금'));
assert.ok(roots.get('daytrade-fills').textContent.includes('KODEX 인버스 (114800)'));
assert.ok(roots.get('daytrade-metrics').textContent.includes('1,000만 3,586원'));
assert.ok(roots.get('daytrade-metrics').textContent.includes('10,003,586원'));
disclosures[0].open=true;disclosures[0].events.toggle();
assert.ok(visit(disclosures[0],e=>e.tagName==='td').length>=190);
run('render(fixture)');
assert.ok(visit(roots.get('daytrade-scans'),e=>e.tagName==='details')[0].open);
console.log('Display rounding, names, limits, lazy details and open-state preservation passed');
'''
        script='const SOURCE='+json.dumps(source,ensure_ascii=True)+';\n'+harness
        result=subprocess.run(['node','-'],input=script,text=True,capture_output=True,encoding='utf-8',timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
