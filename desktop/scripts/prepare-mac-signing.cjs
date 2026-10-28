const { execFileSync } = require('node:child_process');
const { lstatSync, realpathSync } = require('node:fs');
const { isAbsolute, join, relative } = require('node:path');

const removable = new Set(['com.apple.FinderInfo', 'com.apple.ResourceFork']);

// Only the new application artifact is touched. In particular, quarantine and
// provenance are preserved; this is not an xattr -c or a Gatekeeper bypass.
function cleanSigningMetadata(appPath) {
  if (lstatSync(appPath).isSymbolicLink()) throw new Error('Signing app must not be a symlink');
  const root = realpathSync(appPath);
  const read = () => execFileSync('/usr/bin/xattr', ['-r', root], { encoding: 'utf8' });
  let removed = 0;
  for (const line of read().split('\n')) {
    const split = line.lastIndexOf(': ');
    if (split < 0) continue;
    const file = line.slice(0, split), attribute = line.slice(split + 2);
    if (!removable.has(attribute)) continue;
    const within = relative(root, file);
    if (isAbsolute(within) || within === '..' || within.startsWith('../') || lstatSync(file).isSymbolicLink()) {
      throw new Error('Signing metadata path escaped application');
    }
    const actual = relative(root, realpathSync(file));
    if (isAbsolute(actual) || actual === '..' || actual.startsWith('../')) {
      throw new Error('Signing metadata path escaped application');
    }
    execFileSync('/usr/bin/xattr', ['-d', attribute, file], { stdio: 'pipe' });
    removed++;
  }
  if (read().split('\n').some(line => removable.has(line.slice(line.lastIndexOf(': ') + 2)))) {
    throw new Error('Signing metadata reappeared; build in a local temporary directory');
  }
  return removed;
}

module.exports = async function prepareMacSigning(context) {
  if (context.electronPlatformName !== 'darwin') return;
  cleanSigningMetadata(join(context.appOutDir, 'SmartVoiceInput.app'));
};
module.exports.cleanSigningMetadata = cleanSigningMetadata;
