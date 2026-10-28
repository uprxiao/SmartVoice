import { isMain, run } from './common.mjs';

const TAG_PATTERN = /^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;

export function parseVersion(value) {
  const match = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.exec(value);
  if (!match) throw new Error(`Invalid version: ${value}`);
  const parts = match.slice(1).map(Number);
  if (!parts.every(Number.isSafeInteger) || parts[1] >= 100 || parts[2] >= 10) {
    throw new Error(
      `Version must follow AGENTS.md: middle < 100, last < 10 (${value})`
    );
  }
  return parts;
}

export function nextVersion(version) {
  let [first, middle, last] = parseVersion(version);
  if (++last === 10) {
    last = 0;
    middle += 1;
  }
  if (middle === 100) {
    middle = 0;
    first += 1;
  }
  const next = `${first}.${middle}.${last}`;
  parseVersion(next);
  return next;
}

export function nextTag(tags) {
  const versions = tags
    .filter((tag) => TAG_PATTERN.test(tag))
    .map((tag) => {
      const version = tag.slice(1);
      return { version, parts: parseVersion(version) };
    });
  versions.sort(
    (a, b) =>
      a.parts[0] - b.parts[0] ||
      a.parts[1] - b.parts[1] ||
      a.parts[2] - b.parts[2]
  );
  return `v${versions.length ? nextVersion(versions.at(-1).version) : '0.0.1'}`;
}

if (isMain(import.meta.url)) {
  // Read only. Fetch remote tags before using this as a release decision.
  const tags = run('git', ['tag', '--list'], { stdio: 'pipe' }).split('\n');
  console.log(nextTag(tags));
}
