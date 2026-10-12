import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const [inputPath, outputPath, previewPath] = process.argv.slice(2);

if (!inputPath || !outputPath || !previewPath) {
  throw new Error("Usage: node build_article_metadata_workbook.mjs input.json output.xlsx preview.png");
}

const payload = JSON.parse(await fs.readFile(inputPath, "utf8"));
const headers = payload.headers;
const records = payload.records;

function columnName(index) {
  let name = "";
  let n = index + 1;
  while (n > 0) {
    const remainder = (n - 1) % 26;
    name = String.fromCharCode(65 + remainder) + name;
    n = Math.floor((n - 1) / 26);
  }
  return name;
}

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("Articles");
sheet.showGridLines = false;

const rowCount = records.length + 1;
const colCount = headers.length;

sheet.getRangeByIndexes(0, 0, 1, colCount).values = [headers];

const chunkSize = 5000;
for (let start = 0; start < records.length; start += chunkSize) {
  const chunk = records.slice(start, start + chunkSize).map((record) =>
    headers.map((header) => record[header] ?? ""),
  );
  sheet.getRangeByIndexes(start + 1, 0, chunk.length, colCount).values = chunk;
}

const used = sheet.getRangeByIndexes(0, 0, rowCount, colCount);
used.format = {
  font: { name: "Aptos", size: 10, color: "#1F2937" },
  wrapText: true,
  verticalAlignment: "Top",
  borders: { preset: "all", style: "thin", color: "#E5E7EB" },
};

const headerRange = sheet.getRangeByIndexes(0, 0, 1, colCount);
headerRange.format = {
  fill: "#0F766E",
  font: { bold: true, color: "#FFFFFF", name: "Aptos", size: 10 },
  horizontalAlignment: "Center",
  verticalAlignment: "Middle",
};

sheet.getRange("A:A").format.columnWidthPx = 420;
sheet.getRange("B:B").format.columnWidthPx = 190;
sheet.getRange("C:C").format.columnWidthPx = 240;
sheet.getRange("D:D").format.columnWidthPx = 260;
sheet.getRange("E:E").format.columnWidthPx = 360;
sheet.getRange("F:F").format.columnWidthPx = 230;
sheet.getRange("G:G").format.columnWidthPx = 360;
sheet.getRange("H:H").format.columnWidthPx = 360;
sheet.getRange("I:I").format.columnWidthPx = 190;
headerRange.format.rowHeightPx = 28;
sheet.freezePanes.freezeRows(1);

const lastCol = columnName(colCount - 1);
const table = sheet.tables.add(`A1:${lastCol}${rowCount}`, true, "ArticleMetadata");
table.style = "TableStyleMedium2";
table.showFilterButton = true;

await fs.mkdir(path.dirname(outputPath), { recursive: true });

const inspect = await workbook.inspect({
  kind: "table",
  range: `Articles!A1:${lastCol}8`,
  include: "values",
  tableMaxRows: 8,
  tableMaxCols: colCount,
  maxChars: 3000,
});
console.log(inspect.ndjson);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
console.log(errors.ndjson);

const preview = await workbook.render({
  sheetName: "Articles",
  range: `A1:${lastCol}20`,
  scale: 1,
  format: "png",
});
await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(`Saved ${records.length} rows to ${outputPath}`);
