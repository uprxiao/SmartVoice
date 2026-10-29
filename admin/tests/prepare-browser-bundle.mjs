// Freeze concrete TC-WIN-35 parameter cases before driving the owned browser.
import { readFile, writeFile, mkdir, copyFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { resolve, dirname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const [environmentFile, output] = process.argv.slice(2);
if (!environmentFile || !output) throw new Error('Usage: node prepare-browser-bundle.mjs ENVIRONMENT_JSON NEW_BUNDLE_DIRECTORY');
const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const bundle = resolve(output);
const env = JSON.parse(await readFile(resolve(environmentFile), 'utf8'));
if (env.schema !== 'svi-admin-browser-environment-v1' || env.source_root !== repo) throw new Error('Explicit owned fixture/source root mismatch');
const fixture = JSON.parse(await readFile(resolve(env.run_dir, 'fixtures.json'), 'utf8'));
fixture.expected.chars = 42;
fixture.oracle += ' BR-01 counts 14 Unicode letters/numbers per fixed result, excluding two punctuation marks; expected total 42.';
await mkdir(bundle, { recursive: false });
const sha = data => createHash('sha256').update(data).digest('hex');
const save = async (name, data) => writeFile(resolve(bundle, name), JSON.stringify(data, null, 2), { flag: 'wx' });
const fixtureText = JSON.stringify(fixture, null, 2);
await writeFile(resolve(bundle, 'fixtures.json'), fixtureText, { flag: 'wx' });
await copyFile(resolve(repo, 'admin/tests/browser-live-suite.mjs'), resolve(bundle, 'suite.mjs'));
const definitions = [
  ['WIN35-01', '普通账号不能登录管理后台', ['正常输入普通虚构账号', '点击登录', '关联实际登录响应与页面'], { httpStatus: 403, adminVisible: false }],
  ['WIN35-02', '三种回执和重复请求不美化统计', ['通过登录表单登录管理员', '读取概览和区间质量卡', '与冻结样本/数据库独立预期核对'], { sessions: 3, results: 3, failedSessions: 0, inserted: 1, insertionRate: '33.3%', chars: 42, learning: 0 }],
  ['WIN35-03', '按用户明细与筛选独立正确', ['读取两行真实用户统计', '搜索第一位用户', '用键盘选择用户', '核对仅该用户且概览仍全量'], { allUserRows: 2, selectedUserSessions: 2, overviewSessions: 3 }],
  ['WIN35-04', '无匹配用户不伪造数据或更换筛选', ['搜索不存在邮箱', '等待空提示', '核对保留原已选用户统计'], { searchOptionsEmpty: true, selectedUserSessions: 2 }],
  ['WIN35-09', '空日期范围与请求失败分开', ['用真实日期编辑片段输入2001-01-01', '等待本次空范围完成', '核对成功零值及空态', '通过导航恢复默认区间'], { date: '2001-01-01', sessions: 0, inserted: 0, insertionRate: '0.0%' }],
  ['WIN35-05', '用户页读取同一真实数据库', ['点击用户管理', '核对三位虚构账号与实际行数'], { rows: 3, accounts: [fixture.admin_email, ...fixture.users.map(u => u.email)] }],
  ['WIN35-06', '真实服务中断不显示假零值且可恢复', ['停止仅本轮所属服务', '点击刷新概览', '核对错误及不可用值', 'finally恢复同数据库同密钥服务', '点击刷新核对原计数'], { offlineValue: '—', errorVisible: true, recoveredSessions: 3, recoveredInserted: 1 }],
  ['WIN35-07', '刷新需要重登且不重复统计', ['真实硬刷新', '核对登录页及无持久认证', '正常重登', '核对会话仍为3'], { loginRequired: true, persistentAuth: false, sessions: 3 }],
  ['WIN35-08', '退出实际撤销本轮登录', ['点击退出登录', '关联本次logout响应', '核对页面回到登录且无后台导航'], { httpStatus: 204, adminVisible: false }],
];
const plan = {
  schema: 'source-driven-ui-test-cases-v1', scope: 'TC-WIN-35 Admin真实数据库/HTTP/WS和构建页面联调；模型/PCM明确为虚构夹具',
  authorization: { mode: 'auto', basis: '用户已批准v0.0.2全部无需配合的开发/联调/回归与分批push，并要求夜间自动继续。仅本轮新建隔离库和虚构账号。' },
  baseline: { sourceRevision: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repo, encoding: 'utf8' }).trim(), fixtureRevision: sha(fixtureText) },
  cases: definitions.map(([id, name, steps, expected]) => ({ id, name, steps, expected, level: 'real-ui',
    requirement: 'TC-WIN-35 / WIN-06,11；BR-01/鉴权和统计契约',
    preconditions: ['隔离真实PostgreSQL/HTTP/WS夹具已准备', '实际构建的Admin静态资源', '三个一秒会话及重复claim/receipt已入库并独立读回'],
    data: { actors: fixture.users.map(u => u.email), audio: 'synthetic PCM; ASR/LLM mock providers only' },
    fixtureRefs: ['/expected', '/users', '/sessions'],
    oracle: { kind: 'predeclared-dataset-and-raw-database', reference: 'fixtures.json#/expected' },
    ...(id === 'WIN35-04' ? { dependsOn: ['WIN35-03'] } : {}),
  })),
};
await save('scope.json', { inputs: [{ kind: 'source', reference: 'docs/04-test/test-cases-windows-v002.md#TC-WIN-35' },
  { kind: 'route', reference: fixture.origin + '/admin/' }] });
await save('cases.json', plan);
const all = plan.cases.map(c => c.id);
const traces = [
  ['prd', 'docs/04-test/test-cases-windows-v002.md', '| TC-WIN-35 /', all],
  ['stats-rule', 'docs/02-prd/business-rules.md', 'Unicode 类别 L', ['WIN35-02']],
  ['login', 'admin/src/views/LoginView.tsx', "pair.user.role !== 'admin'", ['WIN35-01', 'WIN35-02', 'WIN35-07']],
  ['overview', 'admin/src/views/OverviewView.tsx', '插入成功率', ['WIN35-02', 'WIN35-09', 'WIN35-06']],
  ['usage', 'admin/src/views/UsageTable.tsx', '所选日期和用户没有使用记录', ['WIN35-03', 'WIN35-04', 'WIN35-09']],
  ['users', 'admin/src/views/UsersView.tsx', 'api.users', ['WIN35-05']],
  ['session', 'admin/src/App.tsx', 'onAuthInvalidated', ['WIN35-07', 'WIN35-08']],
  ['backend', 'server/app/api/admin.py', 'require_admin', all],
];
await save('source-map.json', { rules: await Promise.all(traces.map(async ([id, file, anchor, caseIds]) => ({
  id, file: relative(bundle, resolve(repo, file)), anchor, caseIds, sha256: sha(await readFile(resolve(repo, file))),
}))) });
await save('script-map.json', { suiteFile: 'suite.mjs', suiteSha256: sha(await readFile(resolve(bundle, 'suite.mjs'))),
  casesSha256: sha(await readFile(resolve(bundle, 'cases.json'))),
  cases: plan.cases.map(c => ({ id: c.id, status: 'implemented', handler: c.id, level: c.level })),
});
console.log(JSON.stringify({ bundle, cases: plan.cases.length, fixtureSha256: sha(fixtureText), status: 'frozen-before-ui' }));
