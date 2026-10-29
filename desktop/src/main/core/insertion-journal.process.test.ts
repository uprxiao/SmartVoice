import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { afterEach, expect, it } from 'vitest';
const owned: string[] = [];
afterEach(() => { for (const folder of owned.splice(0)) rmSync(folder, { recursive: true, force: true }); });

it('a hard process exit after the durable started mark recovers as uncertain and never grants replay', () => {
  const directory = mkdtempSync(join(tmpdir(), 'svi-journal-process-')); owned.push(directory);
  const intent = { user_id: '11111111-1111-4111-8111-111111111111', session_id: '22222222-2222-4222-8222-222222222222',
    result_id: '33333333-3333-4333-8333-333333333333', insertion_id: '44444444-4444-4444-8444-444444444444', text_hash: 'a'.repeat(64) };
  // Compile the actual source in fresh ordinary Node processes; no journal implementation double.
  const program = `
    const fs=require('node:fs'),ts=require('typescript');
    require.extensions['.ts']=(module,file)=>module._compile(ts.transpileModule(fs.readFileSync(file,'utf8'),
      {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,file);
    const [source,folder,raw,mode]=process.argv.slice(1),intent=JSON.parse(raw);
    const {InsertionJournal}=require(source),journal=new InsertionJournal(folder);
    if(mode==='start'){
      const mark=journal.begin(intent);if(!mark.created)throw new Error('unexpected prior mark');
      fs.writeSync(1,'ARMED\\n');process.kill(process.pid,'SIGKILL');
    }else{
      const recovered=journal.unconfirmed(intent.user_id),again=journal.begin(intent);
      fs.writeSync(1,JSON.stringify({recovered,again}));
    }`;
  const args = [resolve(__dirname, 'insertion-journal.ts'), directory, JSON.stringify(intent)];
  const stopped = spawnSync(process.execPath, ['-e', program, ...args, 'start'], { encoding: 'utf8', timeout: 10000 });
  expect(stopped.stdout, stopped.stderr).toBe('ARMED\n');
  const resumed = spawnSync(process.execPath, ['-e', program, ...args, 'recover'], { encoding: 'utf8', timeout: 10000 });
  expect(resumed.status, resumed.stderr).toBe(0);
  const value=JSON.parse(resumed.stdout);
  expect(value.recovered).toHaveLength(1);
  expect(value.recovered[0]).toMatchObject({phase:'unverified',error_code:'CLIENT_INTERRUPTED'});
  expect(value.again).toMatchObject({created:false,mark:{phase:'unverified'}});
}, 25000);
