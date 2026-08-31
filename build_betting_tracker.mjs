import fs from 'node:fs/promises';
import { Workbook, SpreadsheetFile } from 'file:///C:/Users/santo/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/@oai/artifact-tool/dist/artifact_tool.mjs';

const outputDir = 'outputs/sports-betting-profit-tracker';
await fs.mkdir(outputDir, { recursive: true });

const wb = Workbook.create();
const dash = wb.worksheets.add('Dashboard');
const log = wb.worksheets.add('Bet Log');
const lists = wb.worksheets.add('Lists');

const navy = '#0F172A', blue = '#2563EB', teal = '#0F766E', green = '#16A34A', red = '#DC2626', slate = '#475569', light = '#F8FAFC', border = '#CBD5E1';

// Lists for drop-downs
lists.getRange('A1:A6').values = [['Sport'],['NFL'],['NBA'],['MLB'],['NHL'],['Other']];
lists.getRange('B1:B5').values = [['Bet Type'],['Straight'],['Parlay'],['Live'],['Future']];
lists.getRange('C1:C4').values = [['Result'],['Pending'],['Win'],['Loss']];
lists.getRange('D1:D7').values = [['Sportsbook'],['DraftKings'],['FanDuel'],['BetMGM'],['Caesars'],['Fanatics'],['Other']];
lists.getRange('A1:D1').format = { fill: navy, font: { bold: true, color: '#FFFFFF' } };
lists.getRange('A:D').format.columnWidth = 16;
lists.showGridLines = false;

// Bet log
log.showGridLines = false;
log.getRange('A1:N1').merge();
log.getRange('A1').values = [['SPORTS BETTING BET LOG']];
log.getRange('A1').format = { fill: navy, font: { bold: true, color: '#FFFFFF', size: 16 }, horizontalAlignment: 'center', verticalAlignment: 'center' };
log.getRange('A1').format.rowHeight = 30;
log.getRange('A2:N2').merge();
log.getRange('A2').values = [['Enter one wager per row. Use American odds (e.g., -110 or +150); profit and bankroll update automatically.']];
log.getRange('A2').format = { fill: '#E2E8F0', font: { color: slate, italic: true }, horizontalAlignment: 'left' };
const headers = ['Date','Sport','Sportsbook','Bet / Selection','Bet Type','Odds','Stake','Result','To Win','Profit / Loss','Bankroll','ROI','Notes','Month'];
log.getRange('A4:N4').values = [headers];
log.getRange('A4:N4').format = { fill: blue, font: { bold: true, color: '#FFFFFF' }, horizontalAlignment: 'center', wrapText: true };
log.getRange('A4:N4').format.rowHeight = 28;
for (let r=5; r<=204; r++) {
  log.getRange(`I${r}`).formulas = [[`=IF(OR(F${r}="",G${r}=""),"",IF(F${r}>0,G${r}*F${r}/100,G${r}*100/ABS(F${r})))`]];
  log.getRange(`J${r}`).formulas = [[`=IF(H${r}="","",IF(H${r}="Win",I${r},IF(H${r}="Loss",-G${r},0)))`]];
  log.getRange(`K${r}`).formulas = [[`=IF(COUNTA(A$5:A${r})=0,"",Dashboard!$B$5+SUM(J$5:J${r}))`]];
  log.getRange(`L${r}`).formulas = [[`=IFERROR(J${r}/G${r},"")`]];
  log.getRange(`N${r}`).formulas = [[`=IF(A${r}="","",TEXT(A${r},"mmm yyyy"))`]];
}
log.getRange('A5:H7').values = [
  [new Date('2026-07-23T12:00:00'), 'MLB', 'PrizePicks', 'Troy Melton under 6.5 Ks + Randy Dobnak over 2.5 Ks', 'Parlay', 200, 1, 'Win'],
  [new Date('2026-07-23T12:00:00'), 'MLB', 'PrizePicks', 'Dobnak + Melton under 0.5 1st inning runs + Riley Greene over 0.5 H+R+RBI', 'Parlay', 110, 1, 'Win'],
  [new Date('2026-07-23T12:00:00'), 'MLB', 'PrizePicks', 'George Springer over 0.5 hits + Kerry Carpenter under 1.5 H+R+RBI', 'Parlay', 160, 1, 'Win'],
];
log.getRange('A5:A204').format.numberFormat = 'yyyy-mm-dd';
log.getRange('F5:F204').format.numberFormat = '0';
log.getRange('G5:K204').format.numberFormat = '$#,##0.00;[Red]-$#,##0.00';
log.getRange('L5:L204').format.numberFormat = '0.0%;[Red]-0.0%';
log.getRange('A4:N204').format.borders = { preset: 'inside', style: 'thin', color: '#E2E8F0' };
log.getRange('A5:A204').dataValidation = { rule: { type: 'date', operator: 'between', formula1: 'DATE(2020,1,1)', formula2: 'DATE(2035,12,31)' } };
log.getRange('B5:B204').dataValidation = { rule: { type: 'list', formula1: "'Lists'!$A$2:$A$6" } };
log.getRange('C5:C204').dataValidation = { rule: { type: 'list', formula1: "'Lists'!$D$2:$D$7" } };
log.getRange('E5:E204').dataValidation = { rule: { type: 'list', formula1: "'Lists'!$B$2:$B$5" } };
log.getRange('H5:H204').dataValidation = { rule: { type: 'list', formula1: "'Lists'!$C$2:$C$4" } };
log.getRange('J5:J204').conditionalFormats.add('cellIs',{operator:'greaterThan',formula:0,format:{font:{color:green,bold:true}}});
log.getRange('J5:J204').conditionalFormats.add('cellIs',{operator:'lessThan',formula:0,format:{font:{color:red,bold:true}}});
log.getRange('H5:H204').conditionalFormats.add('containsText',{text:'Win',format:{fill:'#DCFCE7',font:{color:'#166534',bold:true}}});
log.getRange('H5:H204').conditionalFormats.add('containsText',{text:'Loss',format:{fill:'#FEE2E2',font:{color:'#991B1B',bold:true}}});
log.getRange('A4:N204').format.wrapText = false;
['A','B','C','E','F','G','H','I','J','K','L','N'].forEach(c => log.getRange(`${c}5:${c}204`).format.horizontalAlignment='center');
log.getRange('A:A').format.columnWidth=13; log.getRange('B:C').format.columnWidth=14; log.getRange('D:D').format.columnWidth=31; log.getRange('E:E').format.columnWidth=13; log.getRange('F:F').format.columnWidth=10; log.getRange('G:L').format.columnWidth=13; log.getRange('M:M').format.columnWidth=26; log.getRange('N:N').format.columnWidth=13;
log.freezePanes.freezeRows(4);
log.tables.add('A4:N204', true, 'BetLogTable').style = 'TableStyleMedium2';

// Dashboard
dash.showGridLines = false;
dash.getRange('A1:H1').merge(); dash.getRange('A1').values = [['SPORTS BETTING PROFIT TRACKER']];
dash.getRange('A1').format = { fill: navy, font: { bold:true, color:'#FFFFFF', size:18 }, horizontalAlignment:'center' }; dash.getRange('A1').format.rowHeight=34;
dash.getRange('A2:H2').merge(); dash.getRange('A2').values = [['Your bet log drives every number below. Start by entering your bankroll, then log wagers on the Bet Log tab.']]; dash.getRange('A2').format = { fill:'#E2E8F0', font:{color:slate,italic:true} };
dash.getRange('A4:B4').merge(); dash.getRange('A4').values=[['SETUP']]; dash.getRange('A4').format={fill:teal,font:{bold:true,color:'#FFFFFF'},horizontalAlignment:'center'};
dash.getRange('A5').values=[['Starting Bankroll']]; dash.getRange('B5').values=[[16]]; dash.getRange('B5').format={fill:'#FEF3C7',font:{bold:true}}; dash.getRange('B5').format.numberFormat='$#,##0.00';
dash.getRange('A4:B5').format.borders={preset:'outside',style:'thin',color:border};
const kpi = [['Net Profit / Loss','=SUM(\'Bet Log\'!J5:J204)'],['Current Bankroll','=B5+B7'],['Total Staked','=SUM(\'Bet Log\'!G5:G204)'],['ROI','=IFERROR(B7/B9,0)'],['Record','=COUNTIF(\'Bet Log\'!H5:H204,"Win")&" - "&COUNTIF(\'Bet Log\'!H5:H204,"Loss")'],['Win Rate','=IFERROR(COUNTIF(\'Bet Log\'!H5:H204,"Win")/(COUNTIF(\'Bet Log\'!H5:H204,"Win")+COUNTIF(\'Bet Log\'!H5:H204,"Loss")),0)']];
dash.getRange('A7:B12').values=kpi.map(x=>[x[0],null]); dash.getRange('B7:B12').formulas=kpi.map(x=>[x[1]]);
dash.getRange('A7:A12').format={fill:'#E2E8F0',font:{bold:true,color:navy}}; dash.getRange('B7:B12').format={fill:light,font:{bold:true,color:navy,size:13}}; dash.getRange('A7:B12').format.borders={preset:'all',style:'thin',color:border};
dash.getRange('B7:B9').format.numberFormat='$#,##0.00;[Red]-$#,##0.00'; dash.getRange('B10:B10').format.numberFormat='0.0%;[Red]-0.0%'; dash.getRange('B12:B12').format.numberFormat='0.0%';
dash.getRange('B7').conditionalFormats.add('cellIs',{operator:'greaterThan',formula:0,format:{font:{color:green,bold:true}}}); dash.getRange('B7').conditionalFormats.add('cellIs',{operator:'lessThan',formula:0,format:{font:{color:red,bold:true}}});
dash.getRange('D4:E4').values=[['Month','Profit / Loss']]; dash.getRange('D4:E4').format={fill:blue,font:{bold:true,color:'#FFFFFF'}};
const months=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
dash.getRange('D5:D16').values=months.map(m=>[m]);
for(let r=5;r<=16;r++){ dash.getRange(`E${r}`).formulas=[[`=SUMIFS('Bet Log'!$J$5:$J$204,'Bet Log'!$A$5:$A$204,">="&DATE(YEAR(TODAY()),${r-4},1),'Bet Log'!$A$5:$A$204,"<"&DATE(YEAR(TODAY()),${r-3},1))`]]; }
dash.getRange('E5:E16').format.numberFormat='$#,##0.00;[Red]-$#,##0.00'; dash.getRange('D4:E16').format.borders={preset:'all',style:'thin',color:border};
const chart=dash.charts.add('bar',dash.getRange('D4:E16')); chart.title='Monthly Profit / Loss'; chart.hasLegend=false; chart.yAxis={numberFormatCode:'$#,##0'}; chart.setPosition('G4','N19');
dash.getRange('A20:F20').merge(); dash.getRange('A20').values=[['HOW TO USE']]; dash.getRange('A20').format={fill:teal,font:{bold:true,color:'#FFFFFF'},horizontalAlignment:'center'};
dash.getRange('A21:F24').merge(); dash.getRange('A21').values=[['1. Edit Starting Bankroll above.\n2. Add each wager in Bet Log.\n3. Enter Result only when settled (Win, Loss, or Pending).\n4. Profit, ROI, record, and monthly chart update automatically.']]; dash.getRange('A21').format={fill:light,wrapText:true,verticalAlignment:'top'}; dash.getRange('A21:F24').format.borders={preset:'outside',style:'thin',color:border};
dash.getRange('A:A').format.columnWidth=21; dash.getRange('B:B').format.columnWidth=16; dash.getRange('C:C').format.columnWidth=4; dash.getRange('D:E').format.columnWidth=15; dash.getRange('F:F').format.columnWidth=4;

const check = await wb.inspect({kind:'table',range:'Dashboard!A1:E16',include:'values,formulas',tableMaxRows:20,tableMaxCols:8});
console.log(check.ndjson);
const errors = await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A',options:{useRegex:true,maxResults:100},summary:'formula errors'});
console.log(errors.ndjson);
for (const [sheetName, file] of [['Dashboard','dashboard.png'],['Bet Log','bet-log.png']]) { const img=await wb.render({sheetName,autoCrop:'all',scale:1,format:'png'}); await fs.writeFile(`${outputDir}/${file}`,new Uint8Array(await img.arrayBuffer())); }
const out=await SpreadsheetFile.exportXlsx(wb); await out.save(`${outputDir}/Sports_Betting_Profit_Tracker.xlsx`);
