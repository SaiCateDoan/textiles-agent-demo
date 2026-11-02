import {test} from 'node:test';
import assert from 'node:assert/strict';
import * as echarts from 'echarts';
import {prepareChartOption,chartDataTables} from './chartOptions.js';

const render=option=>{
  const chart=echarts.init(null,null,{renderer:'svg',ssr:true,width:800,height:440});
  try{chart.setOption(prepareChartOption({option}));return chart.renderToSVGString();}finally{chart.dispose();}
};

test('axis-free pie, numeric scatter, time series and multi-axis charts really render',()=>{
  const pie={series:[{type:'pie',radius:['40%','70%'],data:[{name:'棉',value:45},{name:'涤纶',value:35},{name:'粘胶',value:20}]}]};
  assert.ok(!('xAxis' in prepareChartOption({option:pie})));
  assert.ok(render(pie).includes('棉'));
  assert.ok(render({series:[{type:'scatter',data:[[150,5.1],[180,2.45]]}]}).includes('<path'));
  const timed={series:[{type:'line',data:[['2026-10-01',10],['2026-11-01',20]]}]};
  assert.equal(prepareChartOption({option:timed}).xAxis.type,'time');
  assert.ok(render(timed).includes('<svg'));
  const multi={dataset:{source:[['月份','产量','不良率'],['一月',100,2],['二月',120,1.8]]},xAxis:{type:'category'},yAxis:[{name:'产量_m'},{name:'不良率_%',position:'right'}],series:[{type:'bar'},{type:'line',yAxisIndex:1,encode:{x:'月份',y:'不良率'}}]};
  const image=render(multi);
  assert.ok(image.includes('不良率_%')&&image.includes('产量_m'));
});

test('missing Cartesian axes get defaults without changing specified coordinates',()=>{
  const p=prepareChartOption({option:{series:[{type:'line',data:[[150,5.1],[180,2.45]]}]}});
  assert.equal(p.xAxis.type,'value');
  const supplied={xAxis:{type:'time',min:'2026-01-01'},yAxis:[{min:100,max:200},{min:0,max:1}],series:[{type:'line',data:[]}]};
  const option=prepareChartOption({option:supplied});
  assert.equal(option.xAxis.min,'2026-01-01');
  assert.equal(option.yAxis.length,2);assert.equal(option.yAxis[1].max,1);
});

test('data inspection supports dataset rows and prose-derived named values',()=>{
  const tables=chartDataTables({dataset:{source:[['阶段','温度'],['预处理',60],['染色',80]]},series:[{type:'pie',name:'纤维',data:[{name:'棉',value:45},{name:'涤纶',value:35}]}]});
  assert.deepEqual(tables[0].headers,['阶段','温度']);
  assert.deepEqual(tables[0].rows,[['预处理',60],['染色',80]]);
  assert.deepEqual(tables[1].rows,[['棉',45],['涤纶',35]]);
});

test('saved legacy artifacts display through the same renderer and tooltip rules',()=>{
  const option=prepareChartOption({chart_type:'bar',group_by:'机台',metric:'不良率',data:[{name:'A',value:1.2}]});
  assert.deepEqual(option.series[0].data,[1.2]);
  assert.equal(option.tooltip.renderMode,'richText');
  const unsafe=prepareChartOption({option:{title:{link:'javascript:alert(1)'},tooltip:{renderMode:'html'},series:[{type:'pie',data:[{name:'<img onerror=alert(1)>',value:1}]}]}});
  assert.ok(!('link' in unsafe.title));
  assert.equal(unsafe.tooltip.renderMode,'richText');
});
