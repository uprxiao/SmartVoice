import { appendFileSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { parseArgs } from 'node:util';
import { appVersion, isMain, root, run } from './common.mjs';
import { nextTag, parseVersion } from './version.mjs';
import { releasePolicy } from './policy.mjs';

export function releaseNotes(tag, packageVersion, changelog) {
  if (!tag?.startsWith('v')) throw new Error('Release tag must start with v');
  const version = tag.slice(1);
  parseVersion(version);
  if (version !== packageVersion) {
    throw new Error(`Tag ${tag} does not match desktop version ${packageVersion}`);
  }
  const lines = changelog.split(/\r?\n/);
  const matches = lines.flatMap((line, i) => (line === `## ${version}` ? [i] : []));
  if (matches.length !== 1) {
    throw new Error(`CHANGELOG must contain exactly one "## ${version}" section`);
  }
  const start = matches[0] + 1;
  const next = lines.findIndex((line, i) => i >= start && line.startsWith('## '));
  const notes = lines.slice(start, next < 0 ? undefined : next).join('\n').trim();
  if (!notes) throw new Error(`CHANGELOG section for ${version} is empty`);
  return `${notes}\n`;
}

export function verifyTagCommit(tag, cwd = root, mainRef = 'origin/main') {
  parseVersion(tag.startsWith('v') ? tag.slice(1) : '');
  const git = (args) => run('git', args, { cwd, stdio: 'pipe' });
  if (git(['cat-file', '-t', `refs/tags/${tag}`]) !== 'tag') {
    throw new Error('Release requires an annotated tag');
  }
  const commit = git(['rev-parse', '--verify', `refs/tags/${tag}^{commit}`]);
  if (git(['rev-parse', 'HEAD']) !== commit) {
    throw new Error('Checkout does not match the release tag commit');
  }
  git(['rev-parse', '--verify', mainRef]);
  try {
    git(['merge-base', '--is-ancestor', commit, mainRef]);
  } catch {
    throw new Error(`Tagged commit must already be merged into ${mainRef}`);
  }
  const expected = nextTag(
    git(['tag', '--list'])
      .split('\n')
      .filter((other) => other && other !== tag)
  );
  if (tag !== expected) {
    throw new Error(`Release tag must be the next version: expected ${expected}, got ${tag}`);
  }
  return commit;
}

if (isMain(import.meta.url)) {
  const { values } = parseArgs({
    options: {
      tag: { type: 'string' },
      git: { type: 'boolean' },
      notes: { type: 'string' },
    },
  });
  const tag = values.tag ?? process.env.SVI_RELEASE_TAG;
  const notes = releaseNotes(
    tag,
    appVersion(),
    readFileSync(join(root, 'CHANGELOG.md'), 'utf8')
  );
  if (values.git) verifyTagCommit(tag);
  const policy = releasePolicy();
  if (process.env.GITHUB_OUTPUT) {
    appendFileSync(
      process.env.GITHUB_OUTPUT,
      `channel=${policy.channel}\nrequire_signing=${policy.requireMacSigning}\n`
    );
  }
  if (values.notes) writeFileSync(values.notes, notes);
  console.log(`Release metadata verified: ${tag} (${policy.channel})`);
}
