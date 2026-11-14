// Выполняется на Mac из корня проекта с поставляемым @oai/artifact-tool.
// Оригинал поставки остаётся неизменным; первые 10 сигналов сохраняются.
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { FileBlob, SpreadsheetFile } from '@oai/artifact-tool';

const root = path.resolve(fileURLToPath(new URL('../../', import.meta.url)));
function numericOption(name, fallback) {
  const index = process.argv.indexOf(name);
  const value = index < 0 ? fallback : Number(process.argv[index + 1]);
  if (!Number.isSafeInteger(value) || value < 1) throw new Error(`Нужен положительный целый ${name}`);
  return value;
}
const deviceCount = numericOption('--devices', 10);
const signalCount = numericOption('--signals', 50);
const periodMs = numericOption('--period-ms', 1000);
if (!Number.isSafeInteger(deviceCount * signalCount)) throw new Error('Слишком много сигналов');
const scenarioName = `${deviceCount}x${signalCount}-${periodMs}ms`;
const lastRow = deviceCount * signalCount + 1;
const boundaryStart = Math.max(1, signalCount - 2);
const outputName = `test_sp_${scenarioName}.xlsx`;
const sourcePath = path.join(root, 'input/highload/sp-emulation/config/test_sp_local.xlsx');
const outputDir = path.join(root, `outputs/highload-${scenarioName}`);
const reportDir = path.join(root, `reports/highload-mac/${scenarioName}-${new Date().toISOString().replace(/[:.]/g, '-')}`);
const outputIndex = process.argv.indexOf('--output');
const requestedOutput = outputIndex < 0 ? path.join(outputDir, outputName) : process.argv[outputIndex + 1];
if (!requestedOutput) throw new Error('После --output нужен путь');
const targetPath = path.resolve(requestedOutput);
if (!targetPath.startsWith(root + path.sep)) throw new Error('Выходной файл должен находиться в проекте');
await fs.mkdir(reportDir, { recursive: true });
const sourceHash = createHash('sha256').update(await fs.readFile(sourcePath)).digest('hex');
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(sourcePath));
const sheet = workbook.worksheets.getItemAt(0);

if (process.argv.includes('--inspect')) {
  console.log((await workbook.inspect({
    kind: 'table', range: 'Sheet1!A1:O4', include: 'values,formulas',
    tableMaxRows: 4, tableMaxCols: 15, maxChars: 4000,
  })).ndjson);
  const preview = await workbook.render({ sheetName: sheet.name, range: 'A1:O12', scale: 1 });
  await fs.writeFile(path.join(reportDir, 'source.png'), new Uint8Array(await preview.arrayBuffer()));
  process.exit(0);
}

const sourceRows = sheet.getRange('A2:O101').values;
const byDevice = new Map();
for (const row of sourceRows) {
  const match = /^\/devices\/([^/]+)\/controls\//.exec(row[0]);
  if (!match) throw new Error('Исходный топик не соответствует формату поставки');
  if (!byDevice.has(match[1])) byDevice.set(match[1], []);
  byDevice.get(match[1]).push(row);
}
if (byDevice.size !== 10 || [...byDevice.values()].some(rows => rows.length !== 10)) {
  throw new Error('Ожидался исходный сценарий 10 устройств по 10 сигналов');
}

const rows = [];
const expectedTopics = [];
const deviceTemplates = [...byDevice.values()];
for (let deviceIndex = 0; deviceIndex < deviceCount; deviceIndex++) {
  const templates = deviceTemplates[deviceIndex % deviceTemplates.length];
  const originalDevice = templates[0][0].split('/')[2];
  if (!/_n\d+_test$/.test(originalDevice)) throw new Error('Неизвестный шаблон имени устройства');
  const device = originalDevice.replace(/_n\d+_test$/, `_n${deviceIndex + 1}_test`);
  for (let index = 0; index < signalCount; index++) {
    const row = [...templates[index % 10]];
    row[2] = periodMs;
    row[12] = deviceIndex + 1;
    row[14] = index + 1;
    row[0] = `${row[11]}${row[12]}${row[13]}${row[14]}`;
    if (!row[0].startsWith(`/devices/${device}/controls/`)) {
      throw new Error('Изменилось имя устройства');
    }
    expectedTopics.push(row[0]);
    rows.push(row);
  }
}
if (new Set(expectedTopics).size !== deviceCount * signalCount) throw new Error('Топики повторяются');

// Продлеваем оформление тела таблицы, сохраняя заголовок и ширины колонок.
for (let row = 102; row <= lastRow; row++) {
  sheet.getRange(`A${row}:O${row}`).copyFrom(sheet.getRange('A2:O2'), 'all');
}
if (lastRow < 101) sheet.getRange(`A${lastRow + 1}:O101`).clear({ applyTo: 'contents' });
sheet.getRange(`A2:O${lastRow}`).values = rows;
sheet.getRange(`A2:A${lastRow}`).formulas = rows.map((_, index) => {
  const row = index + 2;
  return [`=L${row}&M${row}&N${row}&O${row}`];
});
workbook.recalculate();

const calculated = sheet.getRange(`A2:K${lastRow}`).values;
const counts = {};
for (let index = 0; index < calculated.length; index++) {
  const row = calculated[index];
  if (row[0] !== expectedTopics[index] || row[2] !== periodMs || row[9] !== 0) {
    throw new Error(`Ошибка топика, периода или лимита в строке ${index + 2}`);
  }
  const device = row[0].split('/')[2];
  counts[device] = (counts[device] || 0) + 1;
}
if (Object.keys(counts).length !== deviceCount || Object.values(counts).some(count => count !== signalCount)) {
  throw new Error('Число устройств или сигналов не соответствует запросу');
}
console.log((await workbook.inspect({
  kind: 'table', range: `Sheet1!A${boundaryStart}:K${signalCount + 4}`, include: 'values,formulas',
  tableMaxRows: 7, tableMaxCols: 11, maxChars: 3500,
})).ndjson);
console.log((await workbook.inspect({
  kind: 'match', searchTerm: '#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!',
  options: { useRegex: true, maxResults: 10 }, maxChars: 1000,
})).ndjson);

for (const [name, range] of [
  ['first', 'A1:O12'],
  ['boundary', `A${boundaryStart}:O${signalCount + 4}`],
  ['last', `A${Math.max(1, lastRow - 6)}:O${lastRow}`],
]) {
  const preview = await workbook.render({ sheetName: sheet.name, range, scale: 1 });
  await fs.writeFile(path.join(reportDir, `${name}.png`), new Uint8Array(await preview.arrayBuffer()));
}
await fs.mkdir(outputDir, { recursive: true });
const outputPath = path.join(outputDir, outputName);
await (await SpreadsheetFile.exportXlsx(workbook)).save(outputPath);
if (targetPath !== outputPath) {
  await fs.mkdir(path.dirname(targetPath), { recursive: true });
  await fs.copyFile(outputPath, targetPath);
}
if (sourceHash !== createHash('sha256').update(await fs.readFile(sourcePath)).digest('hex')) {
  throw new Error('Оригинал поставки изменился');
}
await fs.writeFile(path.join(reportDir, 'scenario.json'), JSON.stringify({
  generatedAt: new Date().toISOString(), sourceSha256: sourceHash,
  outputSha256: createHash('sha256').update(await fs.readFile(outputPath)).digest('hex'),
  devices: counts, topics: calculated.length, periodMs, limitPerTopic: 0,
}, null, 2));
console.log(`Сформирован сценарий: ${deviceCount} устройств × ${signalCount} сигналов, период ${periodMs} мс.`);
