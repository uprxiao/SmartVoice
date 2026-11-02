import { createHash } from 'node:crypto';
import { SimulationError, type CsvRow } from './store';

const invalid = (message: string) => new SimulationError(422, 'VALIDATION_ERROR', message);

export function multipartCsv(body: Buffer, contentType: string): Buffer {
  const match = /^multipart\/form-data;\s*boundary=(?:"([A-Za-z0-9'()+_,./:=?-]{1,70})"|([A-Za-z0-9'()+_,./:=?-]{1,70}))$/i.exec(contentType);
  if (!match) throw invalid('CSV 上传格式无效');
  const marker = Buffer.from(`--${match[1] ?? match[2]}`);
  if (!body.subarray(0, marker.length).equals(marker)) throw invalid('CSV 上传边界无效');
  let offset = marker.length;
  let file: Buffer | null = null;
  for (;;) {
    if (body.subarray(offset, offset + 2).equals(Buffer.from('--'))) {
      const tail = body.subarray(offset + 2).toString('ascii');
      if (tail !== '' && tail !== '\r\n') throw invalid('CSV 上传尾部无效');
      break;
    }
    if (body.subarray(offset, offset + 2).toString('ascii') !== '\r\n') throw invalid('CSV 上传格式无效');
    offset += 2;
    const headerEnd = body.indexOf('\r\n\r\n', offset);
    if (headerEnd < offset || headerEnd - offset > 4096) throw invalid('CSV 上传头无效');
    const headers = body.subarray(offset, headerEnd).toString('utf8');
    if (!/^content-disposition: form-data;\s*name="file";\s*filename="[^"\r\n]{1,255}"\r\ncontent-type: (?:text\/csv|application\/octet-stream)$/i.test(headers)) {
      throw invalid('CSV 上传仅允许单个 file 文件');
    }
    const start = headerEnd + 4;
    const next = body.indexOf(Buffer.concat([Buffer.from('\r\n'), marker]), start);
    if (next < 0 || file) throw invalid('CSV 上传必须包含且仅包含一个文件');
    file = body.subarray(start, next);
    if (file.length > 1024 * 1024) throw new SimulationError(413, 'REQUEST_TOO_LARGE', 'CSV 文件超过 1 MiB');
    offset = next + 2 + marker.length;
  }
  if (!file) throw invalid('CSV 文件缺失');
  return file;
}

export function parseCsv(bytes: Buffer): { digest: string; rows: CsvRow[] } {
  let text: string;
  try { text = new TextDecoder('utf-8', { fatal: true }).decode(bytes).replace(/^\uFEFF/, ''); }
  catch { throw invalid('CSV 必须为 UTF-8 编码'); }
  const records: string[][] = [];
  let cells: string[] = [], cell = '', phase: 'plain' | 'quoted' | 'after-quote' = 'plain';
  const finishCell = () => { cells.push(cell); cell = ''; phase = 'plain'; };
  const finishRow = () => { finishCell(); records.push(cells); cells = []; };
  for (let index = 0; index < text.length; index++) {
    const char = text[index];
    if (phase === 'quoted') {
      if (char === '"') {
        if (text[index + 1] === '"') { cell += '"'; index++; } else phase = 'after-quote';
      } else cell += char;
    } else if (char === ',') finishCell();
    else if (char === '\n' || char === '\r') {
      if (char === '\r' && text[index + 1] === '\n') index++;
      finishRow();
    } else if (char === '"' && phase === 'plain' && cell === '') phase = 'quoted';
    else if (phase === 'after-quote' || char === '"') throw invalid('CSV 语法错误');
    else cell += char;
  }
  if (phase === 'quoted') throw invalid('CSV 引号未结束');
  if (cell || cells.length || phase === 'after-quote') finishRow();
  const header = records.shift()?.map(value => value.trim().toLowerCase());
  if (!header || !header.includes('term') || new Set(header).size !== header.length
    || header.some(value => !['term', 'aliases'].includes(value))) throw invalid('CSV 表头必须包含 term，可选 aliases');
  const rows: CsvRow[] = [];
  records.forEach((record, index) => {
    if (record.every(value => !value.trim())) return;
    if (rows.length >= 1000) throw new SimulationError(413, 'REQUEST_TOO_LARGE', 'CSV 数据行超过 1000');
    if (record.length > header.length) throw invalid('CSV 数据行包含多余字段');
    rows.push({ row: index + 2, term: record[header.indexOf('term')] ?? '',
      aliases: (record[header.indexOf('aliases')] ?? '').split(';').map(value => value.trim()).filter(Boolean) });
  });
  return { digest: createHash('sha256').update(bytes).digest('hex'), rows };
}
