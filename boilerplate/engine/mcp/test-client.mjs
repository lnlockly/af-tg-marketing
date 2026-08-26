// Quick smoke: spawn the leads-ops server, list its tools, call a couple read-only ones.
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const transport = new StdioClientTransport({
  command: 'node',
  args: [new URL('./server.mjs', import.meta.url).pathname],
  env: { ...process.env },
});
const client = new Client({ name: 'smoke', version: '0.0.1' }, { capabilities: {} });
await client.connect(transport);

const { tools } = await client.listTools();
console.log('TOOLS:', tools.map((t) => t.name).join(', '));

const list = await client.callTool({ name: 'account_list', arguments: { active_only: false } });
console.log('account_list →', list.content[0].text.slice(0, 300));

const eng = await client.callTool({ name: 'engine_control', arguments: { action: 'status' } });
console.log('engine_control status →', eng.content[0].text.slice(0, 200));

await client.close();
process.exit(0);
