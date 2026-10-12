const fs=require('fs'),vm=require('vm');
// User contract: consumption uses real snapshots, missing Genius remains pending.
const script=fs.readFileSync('index.html','utf8').split('<script>')[1].split('</script>')[0];
new vm.Script(script);
const nodes=new Map(),document={getElementById(id){if(!nodes.has(id))nodes.set(id,{value:'',classList:{toggle(){},add(){},remove(){}},addEventListener(){},textContent:'',innerHTML:''});return nodes.get(id)}};
document.getElementById('ccDays').value='7';
const ctx=vm.createContext({document,Intl,Date,console});
vm.runInContext(script.split('const tick')[0],ctx);
vm.runInContext(`
const day=new Intl.DateTimeFormat('en-CA',{timeZone:TZ,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
ccData={generatedAt:new Date().toISOString(),machines:[{machineId:'aleks-mbp',state:'fresh',dataUpdatedAt:new Date().toISOString()},{machineId:'genius',state:'pending',dataUpdatedAt:null}],rows:[{machineId:'aleks-mbp',app:'codex',date:day,model:'gpt-test',inputTokens:10,cacheReadTokens:20,cacheCreationTokens:0,outputTokens:3,totalTokens:33,reportedInLatest:true}]};
renderCc();
if($('ccTotal').textContent!=='33'||!$('ccStatus').innerHTML.includes('Genius')||!$('ccStatus').innerHTML.includes('Pending'))throw Error('Real partial coverage missing');
$('ccMachine').value='genius';renderCc();
if($('ccTotal').textContent!=='—'||!$('ccRows').innerHTML.includes('Waiting'))throw Error('Pending machine represented as zero');
$('ccMachine').value='';$('ccApp').value='claude';renderCc();if($('ccTotal').textContent!=='0')throw Error('App filter failed');
$('ccApp').value='';$('ccModel').value='other';renderCc();if($('ccTotal').textContent!=='0')throw Error('Model filter failed');
$('ccModel').value='';ccData.machines[0].state='stale';renderCc();if($('ccTotal').textContent!=='33'||!$('ccStatus').innerHTML.includes('Stale'))throw Error('Stale readings not labelled');
`,ctx);
console.log('PASS: UI syntax, pending without zero, partial coverage, machine/app/model filters, stale retained data');
