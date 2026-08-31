import type { Cs2Market, PropLine } from "../model/types";

const marketFromText = (text: string): Cs2Market | null => {
  if (/maps?\s*1\s*[-–]\s*2\s*headshots?/i.test(text)) return "maps_1_2_headshots";
  if (/maps?\s*1\s*[-–]\s*2\s*kills?/i.test(text)) return "maps_1_2_kills";
  return null;
};

export function parsePrizePicksText(input: string): PropLine[] {
  const lines = input.split(/\r?\n/).map((line) => line.replace(/\s+/g, " ").trim()).filter(Boolean);
  const props: PropLine[] = [];
  for (let index = 0; index < lines.length; index += 1) {
    const market = marketFromText(lines[index]);
    if (!market) continue;
    const context = lines.slice(Math.max(0, index - 5), index + 3);
    const combined = context.join(" ");
    const lineValue = context
      .flatMap((line) => [...line.matchAll(/(?:^|\s)(\d{1,2}(?:\.5)?)(?=\s|$)/g)].map((match) => Number(match[1])))
      .filter((value) => value >= 5 && value <= 60)
      .at(-1);
    if (!lineValue) continue;
    const isNoise = (value: string) => {
      const cleaned = value.replace(/[🔥🎯]/gu, "").trim();
      return !cleaned ||
        /^\d+(?:\.5)?$/.test(cleaned) ||
        /maps?\s*1\s*[-–]\s*2/i.test(cleaned) ||
        /^(more|less|demon|goblin|popular|trending)$/i.test(cleaned) ||
        /\bvs\.?\b/i.test(cleaned) ||
        /\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b/i.test(cleaned) ||
        /\d{1,2}:\d{2}\s*(?:am|pm)/i.test(cleaned) ||
        /\s-\s(?:G|F|C)$/i.test(cleaned);
    };
    const player = [...context]
      .reverse()
      .map((value) => value.replace(/[🔥🎯]/gu, "").replace(/^(More|Less)\s+/i, "").trim())
      .find((value) => !isNoise(value) && /^[A-Za-z0-9_.-]{2,24}$/.test(value)) ?? "Needs review";
    const opponent = combined.match(/\bvs\.?\s+(.+?)(?=\s+maps?\s*1\s*[-–]\s*2|\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b|$)/i)?.[1]?.trim() ?? "Needs review";
    const team = context.find((line) => /\s-\s(?:G|F|C)$/.test(line))?.replace(/\s-\s(?:G|F|C)$/, "") ?? "Needs review";
    props.push({ player, team, opponent, market, line: lineValue, source: "PrizePicks" });
  }
  return props.filter((prop, index, all) => index === all.findIndex((candidate) => candidate.player.toLowerCase() === prop.player.toLowerCase() && candidate.market === prop.market && candidate.line === prop.line));
}

export function decodeImportedBoard(encoded: string): PropLine[] {
  try {
    const json = decodeURIComponent(escape(atob(encoded)));
    const rows = JSON.parse(json) as PropLine[];
    return rows.filter((row) => row.player && row.market && Number.isFinite(Number(row.line))).map((row) => ({ ...row, line: Number(row.line), source: "PrizePicks" }));
  } catch {
    return [];
  }
}
