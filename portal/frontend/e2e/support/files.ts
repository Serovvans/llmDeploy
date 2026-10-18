/**
 * Файлы для тестов собираются в памяти: у каждого теста своё содержимое, поэтому проверка
 * повторной загрузки по хешу (docs/portal-design.md §5.2) не связывает тесты между собой.
 */
import { createCipheriv, createHash } from 'node:crypto';
import { crc32, deflateRawSync, deflateSync, inflateRawSync } from 'node:zlib';

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

/**
 * Одноцветное изображение PNG, по умолчанию 32×32; цвет задаёт `seed`, чтобы содержимое было своим у
 * каждого теста. Сторона от 142 точек проходит пороги картинки документа (docs/portal-api.md §8.6).
 */
export function pngImage(name: string, seed: string, size = 32): TestFile {
  const color = seedColor(seed);
  const row = Buffer.concat([Buffer.from([0]), ...Array.from({ length: size }, () => color)]);
  return { name, mimeType: 'image/png', buffer: pngFromRows(size, size, Array.from({ length: size }, () => row), 6) };
}

export function textFile(name: string, content: string): TestFile {
  return { name, mimeType: name.endsWith('.md') ? 'text/markdown' : 'text/plain', buffer: Buffer.from(content, 'utf8') };
}

/** Сторона картинки, которую база знаний сохраняет: не короче 100 точек и не меньше 20 000 по площади. */
export const KEPT_IMAGE_SIDE = 200;

function seedColor(seed: string): Buffer {
  return Buffer.from(seed.padEnd(6, '0').slice(0, 6), 'hex');
}

/** PDF из одной страницы с текстовым слоем и встроенной картинкой 200×200 цвета `seed`. */
export function pdfWithImage(name: string, text: string, seed: string): TestFile {
  const side = KEPT_IMAGE_SIDE;
  const pixels = deflateSync(Buffer.concat(Array.from({ length: side * side }, () => seedColor(seed))));
  const content = `BT /F1 12 Tf 50 780 Td (${text.replace(/[\\()]/g, '\\$&')}) Tj ET q ${side} 0 0 ${side} 50 500 cm /Im1 Do Q`;
  const objects: Buffer[] = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [4 0 R] /Count 1 >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] ' +
      '/Resources << /Font << /F1 3 0 R >> /XObject << /Im1 5 0 R >> >> /Contents 6 0 R >>',
  ].map((object) => Buffer.from(object, 'latin1'));
  objects.push(
    Buffer.concat([
      Buffer.from(
        `<< /Type /XObject /Subtype /Image /Width ${side} /Height ${side} /ColorSpace /DeviceRGB ` +
          `/BitsPerComponent 8 /Filter /FlateDecode /Length ${pixels.length} >>\nstream\n`,
        'latin1',
      ),
      pixels,
      Buffer.from('\nendstream', 'latin1'),
    ]),
    Buffer.from(`<< /Length ${content.length} >>\nstream\n${content}\nendstream`, 'latin1'),
  );

  const head = Buffer.from('%PDF-1.4\n', 'latin1');
  const parts: Buffer[] = [head];
  const offsets: number[] = [];
  let length = head.length;
  objects.forEach((object, index) => {
    offsets.push(length);
    const part = Buffer.concat([Buffer.from(`${index + 1} 0 obj\n`, 'latin1'), object, Buffer.from('\nendobj\n', 'latin1')]);
    parts.push(part);
    length += part.length;
  });
  const xref =
    `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n` +
    offsets.map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`).join('') +
    `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${length}\n%%EOF\n`;
  return { name, mimeType: 'application/pdf', buffer: Buffer.concat([...parts, Buffer.from(xref, 'latin1')]) };
}

/** Часть ZIP-архива; `deflate` — сжать (иначе хранится как есть). */
export interface ZipPart {
  path: string;
  data: Buffer;
  deflate?: boolean;
}

/** ZIP-архив в памяти: части хранятся как есть или сжимаются Deflate. */
function zip(parts: ZipPart[]): Buffer {
  const locals: Buffer[] = [];
  const central: Buffer[] = [];
  let offset = 0;
  for (const part of parts) {
    const name = Buffer.from(part.path, 'utf8');
    const stored = part.deflate ? deflateRawSync(part.data) : part.data;
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(part.deflate ? 8 : 0, 8);
    local.writeUInt32LE(crc32(part.data), 14);
    local.writeUInt32LE(stored.length, 18);
    local.writeUInt32LE(part.data.length, 22);
    local.writeUInt16LE(name.length, 26);
    const entry = Buffer.alloc(46);
    entry.writeUInt32LE(0x02014b50, 0);
    entry.writeUInt16LE(20, 4);
    entry.writeUInt16LE(20, 6);
    local.copy(entry, 10, 8, 30);
    entry.writeUInt32LE(offset, 42);
    locals.push(local, name, stored);
    central.push(entry, name);
    offset += local.length + name.length + stored.length;
  }
  const directory = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(parts.length, 8);
  end.writeUInt16LE(parts.length, 10);
  end.writeUInt32LE(directory.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, directory, end]);
}

const DOCX_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
const OOXML = 'http://schemas.openxmlformats.org';

/**
 * Как картинка вставлена в текст DOCX: DrawingML (`a:blip`), VML (`v:imagedata`, документы,
 * переделанные из `.doc`) или DrawingML с запасным VML в `mc:AlternateContent` — так пишет Word
 * для совместимости, и картинка при этом одна (docs/portal-api.md §8.6).
 */
export type DocxImageLink = 'drawing' | 'vml' | 'drawing+vml';

export interface DocxImage {
  png: Buffer;
  link?: DocxImageLink;
}

function escapeXml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function drawingRun(relationship: string): string {
  return (
    `<w:drawing><wp:inline><a:graphic><a:graphicData uri="${OOXML}/drawingml/2006/picture">` +
    `<pic:pic><pic:blipFill><a:blip r:embed="${relationship}"/></pic:blipFill></pic:pic>` +
    '</a:graphicData></a:graphic></wp:inline></w:drawing>'
  );
}

function vmlRun(relationship: string): string {
  return `<w:pict><v:shape style="width:100pt;height:100pt"><v:imagedata r:id="${relationship}"/></v:shape></w:pict>`;
}

function imageRun(relationship: string, link: DocxImageLink): string {
  if (link === 'vml') {
    return vmlRun(relationship);
  }
  if (link === 'drawing') {
    return drawingRun(relationship);
  }
  return (
    `<mc:AlternateContent><mc:Choice Requires="wps">${drawingRun(relationship)}</mc:Choice>` +
    `<mc:Fallback>${vmlRun(relationship)}</mc:Fallback></mc:AlternateContent>`
  );
}

/** Абзац DOCX; `runPerWord` — каждое слово отдельным фрагментом `w:r`, как в документах с правкой. */
function paragraphXml(text: string, runPerWord: boolean): string {
  const runs = runPerWord ? text.split(/(?<= )/) : [text];
  return `<w:p>${runs.map((run) => `<w:r><w:t xml:space="preserve">${escapeXml(run)}</w:t></w:r>`).join('')}</w:p>`;
}

/**
 * Документ Word: абзацы текста, за ними картинки PNG по одной в абзаце; `extraParts` —
 * дополнительные части архива (например, вложенный объект), `deflate` — сжать разметку,
 * `runPerWord` — разметка тяжелее текста в несколько раз.
 */
export function wordDocument(
  name: string,
  options: { paragraphs: string[]; images?: DocxImage[]; extraParts?: ZipPart[]; deflate?: boolean; runPerWord?: boolean },
): TestFile {
  const images = options.images ?? [];
  const xml = (body: string) => Buffer.from(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?>${body}`, 'utf8');
  const relationships = (items: string) =>
    xml(`<Relationships xmlns="${OOXML}/package/2006/relationships">${items}</Relationships>`);
  const body = [
    ...options.paragraphs.map((text) => paragraphXml(text, options.runPerWord ?? false)),
    ...images.map((image, index) => `<w:p><w:r>${imageRun(`rId${index + 1}`, image.link ?? 'drawing')}</w:r></w:p>`),
  ].join('');
  const buffer = zip([
    {
      path: '[Content_Types].xml',
      data: xml(
        `<Types xmlns="${OOXML}/package/2006/content-types">` +
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
          '<Default Extension="xml" ContentType="application/xml"/>' +
          '<Default Extension="png" ContentType="image/png"/>' +
          `<Override PartName="/word/document.xml" ContentType="${DOCX_TYPE}.main+xml"/></Types>`,
      ),
    },
    {
      path: '_rels/.rels',
      data: relationships(
        `<Relationship Id="rId1" Type="${OOXML}/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>`,
      ),
    },
    {
      path: 'word/_rels/document.xml.rels',
      data: relationships(
        images
          .map(
            (_, index) =>
              `<Relationship Id="rId${index + 1}" Type="${OOXML}/officeDocument/2006/relationships/image" ` +
              `Target="media/image${index + 1}.png"/>`,
          )
          .join(''),
      ),
    },
    {
      path: 'word/document.xml',
      deflate: options.deflate,
      data: xml(
        `<w:document xmlns:w="${OOXML}/wordprocessingml/2006/main" xmlns:r="${OOXML}/officeDocument/2006/relationships" ` +
          `xmlns:wp="${OOXML}/drawingml/2006/wordprocessingDrawing" xmlns:a="${OOXML}/drawingml/2006/main" ` +
          `xmlns:pic="${OOXML}/drawingml/2006/picture" xmlns:v="urn:schemas-microsoft-com:vml" ` +
          `xmlns:mc="${OOXML}/markup-compatibility/2006"><w:body>${body}</w:body></w:document>`,
      ),
    },
    ...images.map((image, index) => ({ path: `word/media/image${index + 1}.png`, data: image.png })),
    ...(options.extraParts ?? []),
  ]);
  return { name, mimeType: DOCX_TYPE, buffer };
}

/** DOCX из одного абзаца текста и встроенной картинки PNG 200×200 цвета `seed`. */
export function docxWithImage(name: string, text: string, seed: string): TestFile {
  return wordDocument(name, { paragraphs: [text], images: [{ png: pngImage('image1.png', seed, KEPT_IMAGE_SIDE).buffer }] });
}

/** Одноцветная картинка PNG заданного размера; цвет — три байта RGB. */
export function solidPng(width: number, height: number, rgb: [number, number, number]): Buffer {
  const color = Buffer.from(rgb);
  const row = Buffer.concat([Buffer.from([0]), ...Array.from({ length: width }, () => color)]);
  return pngFromRows(width, height, Array.from({ length: height }, () => row), 6);
}

/**
 * Картинка PNG из шума — несжимаемая, как скриншот с мелкими деталями по объёму: файл почти
 * равен `width × height × 3` байт. Шум детерминирован: поток AES-CTR с ключом из `seed`.
 */
export function noisePng(width: number, height: number, seed: string): Buffer {
  const key = createHash('sha256').update(seed).digest().subarray(0, 16);
  const cipher = createCipheriv('aes-128-ctr', key, Buffer.alloc(16));
  const noise = cipher.update(Buffer.alloc(width * height * 3));
  const rows = Array.from({ length: height }, (_, y) =>
    Buffer.concat([Buffer.from([0]), noise.subarray(y * width * 3, (y + 1) * width * 3)]),
  );
  return pngFromRows(width, height, rows, 0);
}

function pngFromRows(width: number, height: number, rows: Buffer[], level: number): Buffer {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header.set([8, 2, 0, 0, 0], 8);
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk('IHDR', header),
    pngChunk('IDAT', deflateSync(Buffer.concat(rows), { level })),
    pngChunk('IEND', Buffer.alloc(0)),
  ]);
}

/**
 * PDF ровно из `totalBytes` байт: одна страница с текстом и неиспользуемый поток-заполнитель —
 * проверка предела размера файла без тысяч страниц.
 */
export function paddedPdf(name: string, text: string, totalBytes: number): TestFile {
  const content = `BT /F1 12 Tf 50 780 Td (${text.replace(/[\\()]/g, '\\$&')}) Tj ET`;
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [4 0 R] /Count 1 >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>',
    `<< /Length ${content.length} >>\nstream\n${content}\nendstream`,
  ];
  const assemble = (fillerLength: number): Buffer => {
    const filler = `6 0 obj\n<< /Length ${fillerLength} >>\nstream\n`;
    let head = '%PDF-1.4\n';
    const offsets: number[] = [];
    objects.forEach((object, index) => {
      offsets.push(head.length);
      head += `${index + 1} 0 obj\n${object}\nendobj\n`;
    });
    offsets.push(head.length);
    const afterFiller = '\nendstream\nendobj\n';
    const xrefAt = head.length + filler.length + fillerLength + afterFiller.length;
    const tail =
      `xref\n0 7\n0000000000 65535 f \n` +
      offsets.map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`).join('') +
      `trailer\n<< /Size 7 /Root 1 0 R >>\nstartxref\n${xrefAt}\n%%EOF\n`;
    return Buffer.concat([
      Buffer.from(head + filler, 'latin1'),
      Buffer.alloc(fillerLength, 0x20),
      Buffer.from(afterFiller + tail, 'latin1'),
    ]);
  };
  // Длина заполнителя входит в текст PDF числом: подбирается, пока размер не сойдётся точно.
  let fillerLength = totalBytes - assemble(0).length;
  while (assemble(fillerLength).length !== totalBytes) {
    fillerLength += totalBytes - assemble(fillerLength).length;
  }
  return { name, mimeType: 'application/pdf', buffer: assemble(fillerLength) };
}

/**
 * Запись WAV (моно, 8 бит, по умолчанию 8 кГц) заданной длины; для многочасовой записи частоту
 * снижают, чтобы файл остался небольшим. Подмена распознавателя стенда содержимое не слушает:
 * на каждые 30 секунд она даёт отрезок «Расшифровка-заглушка, отрезок N.» (docs/portal-api.md §8.7).
 * `seed` делает байты своими у каждого теста — иначе сработала бы проверка повторной загрузки.
 */
export function wavAudio(name: string, seconds: number, seed: string, rate = 8000): TestFile {
  const samples = Buffer.alloc(rate * seconds, 128);
  Buffer.from(seed, 'utf8').copy(samples);
  const header = Buffer.alloc(44);
  header.write('RIFF', 0, 'latin1');
  header.writeUInt32LE(36 + samples.length, 4);
  header.write('WAVEfmt ', 8, 'latin1');
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20);
  header.writeUInt16LE(1, 22);
  header.writeUInt32LE(rate, 24);
  header.writeUInt32LE(rate, 28);
  header.writeUInt16LE(1, 32);
  header.writeUInt16LE(8, 34);
  header.write('data', 36, 'latin1');
  header.writeUInt32LE(samples.length, 40);
  return { name, mimeType: 'audio/wav', buffer: Buffer.concat([header, samples]) };
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
