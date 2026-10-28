export interface Replacement {
  offset: number;
  before: string;
  after: string;
  left: string;
  right: string;
}

const latin = (value: string | undefined): boolean => !!value && /[A-Za-z]/.test(value);
const latinWord = (value: string): boolean => /^[A-Za-z]+(?:['’-][A-Za-z]+)*$/.test(value);
const forbidden = /[\p{Cc}\p{Zl}\p{Zp}\p{N}\n\r.!?。！？；;,，:：/\\]/u;
const han = (value: string | undefined): boolean => !!value && /^\p{Script=Han}$/u.test(value);
const chineseWords = new Intl.Segmenter('zh-CN', { granularity: 'word' });

/** Segmenter indices are UTF-16; all correction boundaries remain code points. */
function chineseBoundaries(text: string): Set<number> {
  const boundaries = new Set([0]);
  let offset = 0;
  for (const segment of chineseWords.segment(text)) {
    offset += [...segment.segment].length;
    boundaries.add(offset);
  }
  return boundaries;
}

/** Minimal edit runs, with equal characters preferred to concealment by substitution. */
function editRuns(before: string[], after: string[]): number {
  const cost = Array.from({ length: before.length + 1 }, () => Array<number>(after.length + 1).fill(0));
  for (let i = 0; i <= before.length; i++) cost[i][0] = i;
  for (let j = 0; j <= after.length; j++) cost[0][j] = j;
  for (let i = 1; i <= before.length; i++) {
    for (let j = 1; j <= after.length; j++) {
      cost[i][j] = Math.min(cost[i - 1][j] + 1, cost[i][j - 1] + 1,
        cost[i - 1][j - 1] + (before[i - 1] === after[j - 1] ? 0 : 1));
    }
  }
  let i = before.length, j = after.length, runs = 0, editing = false;
  while (i || j) {
    if (i && j && before[i - 1] === after[j - 1] && cost[i][j] === cost[i - 1][j - 1]) {
      editing = false; i--; j--;
    } else {
      if (!editing) runs++;
      editing = true;
      if (i && j && cost[i][j] === cost[i - 1][j - 1] + 1) { i--; j--; }
      else if (i && cost[i][j] === cost[i - 1][j] + 1) i--;
      else j--;
    }
  }
  return runs;
}

/** Reject a change touching a protected token, including a path's unchanged slash. */
function touchesProtected(text: string, start: number, end: number): boolean {
  const tokens = /(?:https?:\/\/|[A-Za-z]:[\\/]|\.{0,2}\/)[^\s，。！？；,!?;]*|\p{N}+|[零〇一二三四五六七八九十百千万亿两]+|今天|明天|昨天|后天|前天|现在|稍后|上午|下午|不|没|无|非|勿|别|未|禁止|\b(?:not|no|never|cannot|today|tomorrow|yesterday)\b/giu;
  for (const match of text.matchAll(tokens)) {
    const a = [...text.slice(0, match.index)].length;
    const b = a + [...match[0]].length;
    if (start < b && end > a) return true;
    if (start === end && start > a && start < b) return true;
  }
  return false;
}

/** One local word correction; all offsets, limits and contexts use Unicode code points. */
export function singleReplacement(base: string, current: string): Replacement | null {
  const old = [...base], next = [...current];
  // Bound memory/CPU before allocating an alignment matrix.
  if (!old.length || !next.length || old.length > 32_000 || next.length > 32_256) return null;
  let start = 0;
  while (start < old.length && start < next.length && old[start] === next[start]) start++;
  if (start === old.length && start === next.length) return null;
  let tail = 0;
  while (tail < old.length - start && tail < next.length - start &&
    old[old.length - 1 - tail] === next[next.length - 1 - tail]) tail++;
  let oldEnd = old.length - tail, newEnd = next.length - tail;
  const rawBefore = old.slice(start, oldEnd), rawAfter = next.slice(start, newEnd);
  if (rawBefore.length > 32 || rawAfter.length > 32) return null;
  if (touchesProtected(base, start, oldEnd) || touchesProtected(current, start, newEnd)) return null;
  if (forbidden.test(rawBefore.join('') + rawAfter.join(''))) return null;
  // A complete phrase rewrite is not a term correction. An isolated single
  // character (F-LEARN L07) remains eligible for server-side suggestion only.
  if (start === 0 && tail === 0 && Math.max(old.length, next.length) > 1) return null;

  const rawHasSpace = /\s/u.test(rawBefore.join('') + rawAfter.join(''));
  const joinInsideLatinWord = rawHasSpace && start > 0 && tail > 0 &&
    latin(old[start - 1]) && latin(next[start - 1]) && latin(old[oldEnd]) && latin(next[newEnd]);
  // Pure addition/deletion is excluded, except an internal Latin word-boundary
  // correction such as SQL lite -> SQLite. Never infer a term from appended text.
  if ((!rawBefore.length || !rawAfter.length) && !joinInsideLatinWord) return null;
  const latinChange = [...rawBefore, ...rawAfter].every(c => latin(c) || c === ' ' || c === '-' || c === "'") &&
    (!rawHasSpace || joinInsideLatinWord) &&
    (latin(old[start]) || latin(old[start - 1]) || latin(next[start]) || latin(next[start - 1]));
  if (latinChange) {
    // Token-level evidence is essential for SQL lite -> SQLite: its smallest
    // character diff is deletion of " l". Expand only Latin boundaries; never
    // use lexical boundaries without expanding across separate Latin words.
    while (start > 0 && latin(old[start - 1]) && latin(next[start - 1])) start--;
    while (oldEnd < old.length && newEnd < next.length && latin(old[oldEnd]) &&
      latin(next[newEnd]) && old[oldEnd] === next[newEnd]) { oldEnd++; newEnd++; }
  } else if ([...rawBefore, ...rawAfter].every(han)) {
    // A common Chinese prefix/suffix belongs to the corrected word too:
    // 按钮 -> 按键 must not teach a global 钮 -> 键 mapping. Require a shared
    // boundary in both versions; only unchanged Han neighbours may be added.
    const oldBoundaries = chineseBoundaries(base), newBoundaries = chineseBoundaries(current);
    while (start > 0 && (!oldBoundaries.has(start) || !newBoundaries.has(start)) &&
      han(old[start - 1]) && han(next[start - 1]) && old[start - 1] === next[start - 1]) start--;
    while (oldEnd < old.length && newEnd < next.length &&
      (!oldBoundaries.has(oldEnd) || !newBoundaries.has(newEnd)) &&
      han(old[oldEnd]) && han(next[newEnd]) && old[oldEnd] === next[newEnd]) { oldEnd++; newEnd++; }
  }
  const before = old.slice(start, oldEnd).join(''), after = next.slice(start, newEnd).join('');
  if (!before || !after || before === after || [...before].length > 32 || [...after].length > 32) return null;
  if (forbidden.test(before + after)) return null;
  if (touchesProtected(base, start, oldEnd) || touchesProtected(current, start, newEnd)) return null;
  // Two edits inside one Latin word remain one lexical correction (cloud ->
  // Claude). An unchanged island between separate words/Chinese edits is not.
  const oneLatinToken = latinWord(before) && latinWord(after);
  if (!oneLatinToken && editRuns(rawBefore, rawAfter) !== 1) return null;
  if (/\s/u.test(before + after) && !joinInsideLatinWord) return null;
  return {
    offset: start, before, after,
    left: old.slice(Math.max(0, start - 80), start).join(''),
    right: old.slice(oldEnd, oldEnd + 80).join(''),
  };
}
