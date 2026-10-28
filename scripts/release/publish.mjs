import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { verifyArtifacts } from './artifacts.mjs';
import { appVersion, root, run } from './common.mjs';
import { releaseNotes, verifyTagCommit } from './metadata.mjs';
import { releasePolicy } from './policy.mjs';

const { values } = parseArgs({ options: { artifacts: { type: 'string' } } });
if (!values.artifacts) throw new Error('--artifacts is required');
const dir = resolve(values.artifacts);
const tag = process.env.SVI_RELEASE_TAG;
const version = appVersion();
const notes = releaseNotes(
  tag,
  version,
  readFileSync(join(root, 'CHANGELOG.md'), 'utf8')
);
const commit = verifyTagCommit(tag);
const policy = releasePolicy();
const { files, checksums } = verifyArtifacts(dir, version, commit, policy);
const repository = process.env.GITHUB_REPOSITORY;
if (!repository) throw new Error('GITHUB_REPOSITORY is required');

const gh = (args) => run('gh', [...args, '--repo', repository]);
const existing = spawnSync(
  'gh',
  ['release', 'view', tag, '--repo', repository, '--json', 'isDraft'],
  { encoding: 'utf8' }
);
if (existing.error) throw existing.error;
if (existing.status === 0 && JSON.parse(existing.stdout).isDraft !== true) {
  throw new Error(`${tag} is already public; refusing to replace release assets`);
}

const notesFile = join(dir, 'release-notes.md');
const sumsFile = join(dir, 'SHA256SUMS.txt');
writeFileSync(notesFile, notes);
writeFileSync(sumsFile, checksums);
if (existing.status !== 0) {
  gh([
    'release',
    'create',
    tag,
    '--verify-tag',
    '--draft',
    '--title',
    `SmartVoiceInput ${tag}`,
    '--notes-file',
    notesFile,
  ]);
} else {
  gh(['release', 'edit', tag, '--notes-file', notesFile]);
}
// Replacement is restricted to an existing draft, to support a failed upload retry.
gh(['release', 'upload', tag, ...files, sumsFile, '--clobber']);
gh([
  'release',
  'edit',
  tag,
  '--draft=false',
  `--prerelease=${policy.prerelease}`,
  `--latest=${!policy.prerelease}`,
]);
console.log(`Published ${tag} (${policy.channel}) with ${files.length} assets`);
