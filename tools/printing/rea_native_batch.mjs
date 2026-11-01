// Run a focused REA/Ghidra batch with local, explicitly supplied prerequisites.
// The original binary is inspected, never loaded as executable target code.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const request = JSON.parse(await fs.readFile(process.argv[2], 'utf8'));
const reaRoot = process.env.REA_PACKAGE_ROOT || path.join(process.env.LOCALAPPDATA, 'Programs/REA/node_modules/rea-agents');
const require = createRequire(path.join(reaRoot, 'package.json'));
const {Client} = require('@modelcontextprotocol/client');
const {StdioClientTransport, getDefaultEnvironment} = require('@modelcontextprotocol/client/stdio');
const output = path.resolve(root, request.output_directory);
if (!output.startsWith(path.join(root, 'analysis') + path.sep)) throw new Error('Output must stay in analysis');
await fs.mkdir(output, {recursive:true});
const client = new Client({name:'printing-native-analysis',version:'1.0.0'}, {capabilities:{}});
const transport = new StdioClientTransport({
  command:process.execPath, args:[path.join(reaRoot,'scripts/rea.mjs'),'mcp'], cwd:root,
  env:{...getDefaultEnvironment(), ...process.env,
    GHIDRA_INSTALL_DIR:path.join(root,'.runtime/ghidra_12.1.4_PUBLIC'),
    JAVA_HOME:path.join(root,'.runtime/jdk-21.0.13+9')}, stderr:'pipe'});
let errors='';
transport.stderr?.on('data', c=>{errors=(errors+c.toString()).slice(-8000);});
function unpack(result) {
  if(result.structuredContent) return result.structuredContent;
  const block=result.content?.find(c=>c.type==='text');
  return block ? JSON.parse(block.text) : result;
}
try {
  await client.connect(transport);
  let i=0;
  for(const call of [{name:'open_binary',arguments:{path:request.path,provider_id:'ghidra'}},...request.calls]) {
    const result=await client.callTool(call,undefined,{timeout:450000});
    const value=unpack(result);
    await fs.writeFile(path.join(output,String(i++).padStart(3,'0')+'-'+call.name+'.json'),JSON.stringify(value,null,2));
    console.log(JSON.stringify({call:call.name,arguments:call.arguments,evidence_id:value.evidence_id,
      provider:value.provider,result:value.normalized_result??value.result??value,error:value.error}).slice(0,18000));
    if(result.isError || value.error) throw new Error('REA returned an analysis failure');
  }
  await client.callTool({name:'export_evidence_bundle',arguments:{path:path.join(output,'evidence.json'),overwrite:true}},undefined,{timeout:60000});
} catch(error) {
  console.error(error.message);if(errors) console.error(errors);process.exitCode=1;
} finally {
  try{await client.callTool({name:'close_binary',arguments:{}},undefined,{timeout:60000});}catch{}
  await client.close();
}
