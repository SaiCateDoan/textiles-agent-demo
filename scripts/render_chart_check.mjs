// Offline rendering check for generated chart options. No browser or listener.
import {readFileSync,writeFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import {dirname,join} from 'node:path';
import {prepareChartOption} from '../frontend/src/chartOptions.js';
const require=createRequire(new URL('../frontend/package.json',import.meta.url));
const echarts=require('echarts');
const path=process.argv[2];
const artifacts=JSON.parse(readFileSync(path,'utf8'));
let count=0;
for(const [i,artifact] of artifacts.entries()){
  const chart=echarts.init(null,null,{renderer:'svg',ssr:true,width:800,height:440});
  try{
    chart.setOption(prepareChartOption(artifact));
    const svg=chart.renderToSVGString();
    if(!svg.includes('<svg')||svg.length<500)throw new Error('Chart did not render');
    writeFileSync(join(dirname(path),`chart-${i+1}.svg`),svg);
    count++;
  }finally{chart.dispose();}
}
console.log(JSON.stringify({rendered:count}));
