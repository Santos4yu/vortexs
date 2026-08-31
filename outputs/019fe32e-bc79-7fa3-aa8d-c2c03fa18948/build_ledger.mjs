import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outDir = "C:/Users/santo/OneDrive/Desktop/Vortex/outputs/019fe32e-bc79-7fa3-aa8d-c2c03fa18948";
const source = JSON.parse(await fs.readFile(`${outDir}/ledger_source.json`, "utf8"));
const rows = source.rows;
const workbook = Workbook.create();
const summary = workbook.worksheets.add("Daily Summary");
const picks = workbook.worksheets.add("All Props");
summary.showGridLines = false;
picks.showGridLines = false;

const headers = ["Date", "Player", "Sport", "Prop", "Side", "Line", "Tier", "Score", "Matchup", "Matchup Label", "L5 %", "L10 %", "L20 %", "Projection Edge", "Pitcher", "Pitcher ERA", "Book", "Odds", "Why", "Risk", "Result", "Actual", "Logged At", "Game Time"];
const values = rows.map(r => [
  r.game_date || "", r.player_name || "", r.sport || "", r.stat_type || r.market_key || "",
  String(r.side || "").toUpperCase(), r.line ?? null, r.tier || "", r.vortex_score ?? null,
  r.matchup_score ?? null, r.matchup_label || "", r.l5_rate ?? null, r.l10_rate ?? null,
  r.l20_rate ?? null, r.proj_edge ?? null, r.pitcher_name || "", r.pitcher_era ?? null,
  r.best_book || "", r.best_odds ?? null, r.case_summary || "", r.risk_summary || "",
  r.result ? String(r.result).toUpperCase() : "PENDING", r.actual_value ?? null,
  r.logged_at || "", r.commence_time || "",
]);

picks.getRangeByIndexes(0, 0, 1, headers.length).values = [headers];
if (values.length) picks.getRangeByIndexes(1, 0, values.length, headers.length).values = values;
picks.getRange(`A1:X${Math.max(2, values.length + 1)}`).format.font = { name: "Aptos", size: 10, color: "#DCE7F7" };
picks.getRange("A1:X1").format = { fill: "#142033", font: { bold: true, color: "#55E6C1", size: 10 }, rowHeight: 30, verticalAlignment: "center" };
picks.getRange(`A2:X${Math.max(2, values.length + 1)}`).format.fill = "#0B1220";
picks.getRange(`F2:F${Math.max(2, values.length + 1)}`).format.numberFormat = "0.0";
picks.getRange(`H2:I${Math.max(2, values.length + 1)}`).format.numberFormat = "0";
picks.getRange(`K2:M${Math.max(2, values.length + 1)}`).format.numberFormat = "0.0";
picks.getRange(`N2:N${Math.max(2, values.length + 1)}`).format.numberFormat = "+0.00;-0.00;0.00";
picks.getRange(`P2:P${Math.max(2, values.length + 1)}`).format.numberFormat = "0.00";
picks.freezePanes.freezeRows(1);
picks.freezePanes.freezeColumns(2);
const widths = {A:13,B:23,C:9,D:22,E:9,F:9,G:11,H:9,I:10,J:18,K:9,L:9,M:9,N:14,O:22,P:11,Q:15,R:9,S:48,T:42,U:11,V:10,W:23,X:23};
for (const [col,width] of Object.entries(widths)) picks.getRange(`${col}:${col}`).format.columnWidth = width;
picks.getRange(`S2:T${Math.max(2, values.length + 1)}`).format.wrapText = true;
picks.getRange(`A1:X${Math.max(2, values.length + 1)}`).format.borders = { insideHorizontal: { style: "thin", color: "#23334D" } };
if (values.length) {
  picks.getRange(`U2:U${values.length + 1}`).conditionalFormats.add("containsText", { text: "HIT", format: { fill: "#123D32", font: { color: "#62F5C5", bold: true } } });
  picks.getRange(`U2:U${values.length + 1}`).conditionalFormats.add("containsText", { text: "MISS", format: { fill: "#4B2028", font: { color: "#FF8795", bold: true } } });
  picks.getRange(`U2:U${values.length + 1}`).conditionalFormats.add("containsText", { text: "PENDING", format: { fill: "#40371D", font: { color: "#F6D365" } } });
}

const dates = [...new Set(rows.map(r => r.game_date).filter(Boolean))].sort().reverse();
summary.getRange("A1:H1").merge();
summary.getRange("A1").values = [["VORTEX PROP ROTATION LEDGER"]];
summary.getRange("A1:H1").format = { fill: "#0B1220", font: { name: "Aptos Display", size: 20, bold: true, color: "#55E6C1" }, rowHeight: 42, verticalAlignment: "center" };
summary.getRange("A2:H2").merge();
summary.getRange("A2").values = [["Every posted ELITE/STRONG prop remains in history through scans and restarts. Results update after grading."]];
summary.getRange("A2:H2").format = { fill: "#0B1220", font: { color: "#94A6BF", italic: true }, rowHeight: 28 };
summary.getRange("A4:H4").values = [["Date", "Picks", "Hits", "Misses", "Pending", "Settled", "Hit Rate", "Avg Matchup"]];
summary.getRange("A4:H4").format = { fill: "#142033", font: { bold: true, color: "#55E6C1" }, rowHeight: 28 };
dates.forEach((date, i) => {
  const row = i + 5;
  summary.getRange(`A${row}`).values = [[date]];
  summary.getRange(`B${row}`).formulas = [[`=COUNTIF('All Props'!$A$2:$A$2001,A${row})`]];
  summary.getRange(`C${row}`).formulas = [[`=COUNTIFS('All Props'!$A$2:$A$2001,A${row},'All Props'!$U$2:$U$2001,"HIT")`]];
  summary.getRange(`D${row}`).formulas = [[`=COUNTIFS('All Props'!$A$2:$A$2001,A${row},'All Props'!$U$2:$U$2001,"MISS")`]];
  summary.getRange(`E${row}`).formulas = [[`=COUNTIFS('All Props'!$A$2:$A$2001,A${row},'All Props'!$U$2:$U$2001,"PENDING")`]];
  summary.getRange(`F${row}`).formulas = [[`=C${row}+D${row}`]];
  summary.getRange(`G${row}`).formulas = [[`=IF(F${row}=0,0,C${row}/F${row})`]];
  summary.getRange(`H${row}`).formulas = [[`=IFERROR(AVERAGEIF('All Props'!$A$2:$A$2001,A${row},'All Props'!$I$2:$I$2001),0)`]];
});
const last = Math.max(5, dates.length + 4);
summary.getRange(`A5:H${last}`).format = { fill: "#0B1220", font: { name: "Aptos", color: "#DCE7F7" }, rowHeight: 25, borders: { insideHorizontal: { style: "thin", color: "#23334D" } } };
summary.getRange(`G5:G${last}`).format.numberFormat = "0.0%";
summary.getRange(`H5:H${last}`).format.numberFormat = "0";
summary.getRange("A:A").format.columnWidth = 15;
summary.getRange("B:H").format.columnWidth = 13;
summary.freezePanes.freezeRows(4);

const inspect = await workbook.inspect({ kind: "table", range: `Daily Summary!A1:H${Math.min(last, 12)}`, include: "values,formulas", tableMaxRows: 12, tableMaxCols: 8 });
console.log(inspect.ndjson);
const errors = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", options: { useRegex: true, maxResults: 100 }, summary: "formula scan" });
console.log(errors.ndjson);
const preview = await workbook.render({ sheetName: "Daily Summary", range: `A1:H${Math.min(last, 15)}`, scale: 1.5, format: "png" });
await fs.writeFile(`${outDir}/ledger_preview.png`, new Uint8Array(await preview.arrayBuffer()));
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(`${outDir}/VORTEX_Prop_Rotation_Ledger.xlsx`);
