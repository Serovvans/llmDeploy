/**
 * Файлы для тестов собираются в памяти: у каждого теста своё содержимое, поэтому проверка
 * повторной загрузки по хешу (docs/portal-design.md §5.2) не связывает тесты между собой.
 */
import { crc32, deflateSync, inflateRawSync } from 'node:zlib';

export interface TestFile {
  name: string;
  mimeType: string;
  buffer: Buffer;
}

/** PDF с текстовым слоем: по одной строке латиницей на страницу (встроенный шрифт Helvetica). */
export function textPdf(name: string, pages: string[]): TestFile {
  const objects: string[] = [];
  const pageIds = pages.map((_, index) => 4 + index * 2);
  objects[1] = '<< /Type /Catalog /Pages 2 0 R >>';
  objects[2] = `<< /Type /Pages /Kids [${pageIds.map((id) => `${id} 0 R`).join(' ')}] /Count ${pages.length} >>`;
  objects[3] = '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>';
  pages.forEach((text, index) => {
    const pageId = 4 + index * 2;
    const escaped = text.replace(/[\\()]/g, '\\$&');
    const stream = `BT /F1 12 Tf 50 780 Td (${escaped}) Tj ET`;
    objects[pageId] =
      '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] ' +
      `/Resources << /Font << /F1 3 0 R >> >> /Contents ${pageId + 1} 0 R >>`;
    objects[pageId + 1] = `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`;
  });

  let body = '%PDF-1.4\n';
  const offsets: number[] = [];
  for (let id = 1; id < objects.length; id += 1) {
    offsets[id] = body.length;
    body += `${id} 0 obj\n${objects[id]}\nendobj\n`;
  }
  const xref = body.length;
  body += `xref\n0 ${objects.length}\n0000000000 65535 f \n`;
  for (let id = 1; id < objects.length; id += 1) {
    body += `${String(offsets[id]).padStart(10, '0')} 00000 n \n`;
  }
  body += `trailer\n<< /Size ${objects.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return { name, mimeType: 'application/pdf', buffer: Buffer.from(body, 'latin1') };
}

function pngChunk(type: string, data: Buffer): Buffer {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  const typed = Buffer.concat([Buffer.from(type, 'latin1'), data]);
  const checksum = Buffer.alloc(4);
  checksum.writeUInt32BE(crc32(typed));
  return Buffer.concat([length, typed, checksum]);
}

/** Одноцветное изображение PNG 32×32; цвет задаёт `seed`, чтобы содержимое было своим у каждого теста. */
export function pngImage(name: string, seed: string): TestFile {
  const size = 32;
  const color = Buffer.from(seed.padEnd(6, '0').slice(0, 6), 'hex');
  const row = Buffer.concat([Buffer.from([0]), ...Array.from({ length: size }, () => color)]);
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0);
  header.writeUInt32BE(size, 4);
  header.set([8, 2, 0, 0, 0], 8);
  const buffer = Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk('IHDR', header),
    pngChunk('IDAT', deflateSync(Buffer.concat(Array.from({ length: size }, () => row)))),
    pngChunk('IEND', Buffer.alloc(0)),
  ]);
  return { name, mimeType: 'image/png', buffer };
}

export function textFile(name: string, content: string): TestFile {
  return { name, mimeType: name.endsWith('.md') ? 'text/markdown' : 'text/plain', buffer: Buffer.from(content, 'utf8') };
}

/** Файлы ZIP-архива (DOCX) по именам: читается центральный каталог, содержимое распаковывается. */
export function unzip(archive: Buffer): Map<string, Buffer> {
  const files = new Map<string, Buffer>();
  const end = archive.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
  if (end < 0) {
    throw new Error('Это не ZIP-архив: нет записи конца центрального каталога');
  }
  const count = archive.readUInt16LE(end + 10);
  let entry = archive.readUInt32LE(end + 16);
  for (let index = 0; index < count; index += 1) {
    const method = archive.readUInt16LE(entry + 10);
    const compressedSize = archive.readUInt32LE(entry + 20);
    const nameLength = archive.readUInt16LE(entry + 28);
    const extraLength = archive.readUInt16LE(entry + 30);
    const commentLength = archive.readUInt16LE(entry + 32);
    const local = archive.readUInt32LE(entry + 42);
    const name = archive.toString('utf8', entry + 46, entry + 46 + nameLength);
    const dataStart = local + 30 + archive.readUInt16LE(local + 26) + archive.readUInt16LE(local + 28);
    const data = archive.subarray(dataStart, dataStart + compressedSize);
    files.set(name, method === 0 ? Buffer.from(data) : inflateRawSync(data));
    entry += 46 + nameLength + extraLength + commentLength;
  }
  return files;
}

/** Текст документа DOCX: содержимое `word/document.xml` без разметки. */
export function docxText(archive: Buffer): string {
  const parts = unzip(archive);
  const document = parts.get('word/document.xml');
  if (!parts.has('[Content_Types].xml') || !document) {
    throw new Error('В архиве нет частей документа Word');
  }
  return document
    .toString('utf8')
    .replace(/<\/w:p>/g, '\n')
    .replace(/<[^>]+>/g, '')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&');
}
