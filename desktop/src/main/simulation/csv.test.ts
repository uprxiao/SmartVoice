import { describe, expect, it } from 'vitest';
import { multipartCsv, parseCsv } from './csv';

describe('simulation CSV boundary', () => {
  it('reads UTF-8 BOM, quoted commas, escaped quotes and semicolon aliases', () => {
    const value = parseCsv(Buffer.from('\uFEFFterm,aliases\r\n"React, library","React.js;ReactJS"\r\n"Docker ""Engine""",容器\r\n'));
    expect(value.rows).toEqual([
      { row: 2, term: 'React, library', aliases: ['React.js', 'ReactJS'] },
      { row: 3, term: 'Docker "Engine"', aliases: ['容器'] },
    ]);
    expect(value.digest).toMatch(/^[0-9a-f]{64}$/);
  });

  it.each(['', 'aliases\nReact\n', 'term,term\nReact,React\n', 'term,unknown\nReact,React\n',
    'term\n"React\n', 'term\n"React"suffix\n', 'term\nReact,extra\n'])('rejects malformed CSV input %j', text => {
    expect(() => parseCsv(Buffer.from(text))).toThrow();
  });

  it('rejects invalid UTF-8 and more than 1000 data rows', () => {
    expect(() => parseCsv(Buffer.from([0xff, 0xfe, 0x00]))).toThrow();
    expect(() => parseCsv(Buffer.from(`term\n${'React\n'.repeat(1001)}`))).toThrow();
  });

  it('extracts only the single CSV part and refuses extra multipart fields', () => {
    const type = 'multipart/form-data; boundary=fictional-boundary';
    const part = '--fictional-boundary\r\nContent-Disposition: form-data; name="file"; filename="fictional.csv"\r\nContent-Type: text/csv\r\n\r\nterm\nReact\n\r\n';
    expect(multipartCsv(Buffer.from(`${part}--fictional-boundary--\r\n`), type).toString()).toBe('term\nReact\n');
    expect(() => multipartCsv(Buffer.from(`${part}${part}--fictional-boundary--\r\n`), type)).toThrow();
    expect(() => multipartCsv(Buffer.from(part), type)).toThrow();
    expect(() => multipartCsv(Buffer.alloc(0), 'text/csv')).toThrow();
  });
});
