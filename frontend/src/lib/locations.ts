// Shared country / region helpers for import + assign filters.
//
// Regions map to sets of (normalized, lowercased) country strings so a shortcut
// can group whatever countries actually appear in the data. Each region set also
// includes its own label (e.g. "europe") so a list_location stored as the region
// itself ("Europe") is grouped under that shortcut too. "Europe" is intentionally
// broad (EU + UK + EFTA + geographic Europe). Tune these sets here.

export const REGION_COUNTRIES: Record<string, Set<string>> = {
  Europe: new Set([
    "europe",
    "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia", "czech republic",
    "denmark", "estonia", "finland", "france", "germany", "greece", "hungary", "ireland",
    "italy", "latvia", "lithuania", "luxembourg", "malta", "netherlands", "the netherlands",
    "poland", "portugal", "romania", "slovakia", "slovenia", "spain", "sweden",
    "united kingdom", "uk", "u.k.", "great britain", "england", "scotland", "wales",
    "switzerland", "norway", "iceland", "liechtenstein", "ukraine", "serbia", "monaco",
    "andorra", "san marino", "north macedonia", "macedonia", "montenegro",
    "bosnia and herzegovina", "albania", "moldova", "kosovo",
  ]),
  // Sub-regions of Europe (kept as separate shortcuts; they overlap Europe).
  DACH: new Set(["dach", "germany", "austria", "switzerland"]),
  BENELUX: new Set(["benelux", "belgium", "netherlands", "the netherlands", "luxembourg"]),
  USA: new Set([
    "usa", "us", "u.s.", "u.s.a.", "united states", "united states of america", "america",
  ]),
  // North America = USA + Canada. (Canada on its own is just a country in the
  // list, so it isn't a separate region shortcut anymore.)
  "North America": new Set([
    "north america",
    "usa", "us", "u.s.", "u.s.a.", "united states", "united states of america", "america",
    "canada", "ca",
  ]),
};

export const REGION_ORDER = ["Europe", "DACH", "BENELUX", "USA", "North America"];

export const normCountry = (c: string) => c.trim().toLowerCase();

/** Collapse a selected-country list into a compact human label for list names:
 * a region shrinks to its name ("Europe") when the selection covers every one
 * of the region's members from the FIXED world catalog (COUNTRIES) — compared
 * in normalized space, so dirty data variants ("Czech Republic", "UK") that
 * sneak into the selection are absorbed by the region's synonym set instead of
 * breaking the match (comparing against the live per-bucket option list did
 * exactly that, twice). Widest region first: Europe absorbs DACH/BENELUX,
 * North America absorbs USA; a region already fully covered is skipped.
 * Anything not absorbed stays listed (deduped by normalized name). */
export function collapseCountriesForLabel(selected: string[], _present?: string[]): string[] {
  const selNorm = new Set(selected.map(normCountry));
  const coveredNorm = new Set<string>();
  const out: string[] = [];
  for (const label of ["Europe", "North America", "DACH", "BENELUX", "USA"]) {
    const set = REGION_COUNTRIES[label];
    const canon = COUNTRIES.filter((c) => set.has(normCountry(c)));
    if (!canon.length) continue;
    if (!canon.every((c) => selNorm.has(normCountry(c)))) continue;
    if (canon.every((c) => coveredNorm.has(normCountry(c)))) continue; // subsumed
    out.push(label);
    // Absorb EVERY selected entry that normalizes into this region — including
    // dirty variants the region's synonym set recognizes.
    for (const s of selected) if (set.has(normCountry(s))) coveredNorm.add(normCountry(s));
  }
  const listedNorm = new Set<string>();
  for (const s of selected) {
    const n = normCountry(s);
    if (coveredNorm.has(n) || listedNorm.has(n)) continue;
    listedNorm.add(n);
    out.push(s);
  }
  return out;
}

// Full list of selectable countries for the import location picker.
export const COUNTRIES: string[] = [
  "Afghanistan", "Albania", "Algeria", "Andorra", "Angola", "Argentina", "Armenia",
  "Australia", "Austria", "Azerbaijan", "Bahamas", "Bahrain", "Bangladesh", "Barbados",
  "Belarus", "Belgium", "Belize", "Benin", "Bhutan", "Bolivia", "Bosnia and Herzegovina",
  "Botswana", "Brazil", "Brunei", "Bulgaria", "Burkina Faso", "Burundi", "Cambodia",
  "Cameroon", "Canada", "Chile", "China", "Colombia", "Costa Rica", "Croatia", "Cuba",
  "Cyprus", "Czechia", "Denmark", "Dominican Republic", "Ecuador", "Egypt", "El Salvador",
  "Estonia", "Ethiopia", "Finland", "France", "Georgia", "Germany", "Ghana", "Greece",
  "Guatemala", "Honduras", "Hong Kong", "Hungary", "Iceland", "India", "Indonesia", "Iran",
  "Iraq", "Ireland", "Israel", "Italy", "Jamaica", "Japan", "Jordan", "Kazakhstan", "Kenya",
  "Kuwait", "Latvia", "Lebanon", "Liechtenstein", "Lithuania", "Luxembourg", "Malaysia",
  "Maldives", "Malta", "Mexico", "Moldova", "Monaco", "Mongolia", "Montenegro", "Morocco",
  "Nepal", "Netherlands", "New Zealand", "Nigeria", "North Macedonia", "Norway", "Oman",
  "Pakistan", "Panama", "Paraguay", "Peru", "Philippines", "Poland", "Portugal", "Qatar",
  "Romania", "Russia", "Rwanda", "San Marino", "Saudi Arabia", "Senegal", "Serbia",
  "Singapore", "Slovakia", "Slovenia", "South Africa", "South Korea", "Spain", "Sri Lanka",
  "Sweden", "Switzerland", "Taiwan", "Tanzania", "Thailand", "Tunisia", "Turkey", "Uganda",
  "Ukraine", "United Arab Emirates", "United Kingdom", "United States", "Uruguay",
  "Uzbekistan", "Venezuela", "Vietnam", "Zambia", "Zimbabwe",
];
