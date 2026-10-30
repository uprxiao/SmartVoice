const path=require('node:path');
const fs=require('node:fs');
const {createRequire}=require('node:module');
const fromVite=createRequire(require.resolve('vite'));
const esbuild=require(fromVite.resolve('esbuild'));
const root=path.resolve(__dirname,'../../..');
if(!process.env.SVI_REMAINING_MODULE_DIR)throw Error('Separate disposable module directory required; never write a test entry into the product out/main');
const output=path.resolve(process.env.SVI_REMAINING_MODULE_DIR);
esbuild.buildSync({absWorkingDir:root,tsconfig:path.join(root,'tsconfig.node.json'),entryPoints:[path.join(root,'native/windows/fixtures/remaining-modules.ts')],
  outfile:path.join(output,'out/main/remaining-modules.cjs'),bundle:true,platform:'node',
  format:'cjs',target:'node22',external:['electron'],sourcemap:false});
for(const folder of ['preload','renderer'])fs.cpSync(path.join(root,'out',folder),path.join(output,'out',folder),{recursive:true});
fs.mkdirSync(path.join(output,'resources'),{recursive:true});
fs.copyFileSync(path.join(root,'resources/icon.png'),path.join(output,'resources/icon.png'));
