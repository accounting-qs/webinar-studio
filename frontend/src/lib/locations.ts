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
 * regions whose PRESENT countries are all selected shrink to the region name
 * ("Europe"), remaining countries stay as-is. Larger regions are checked first
 * so Europe absorbs DACH/BENELUX and North America absorbs USA; a region is
 * skipped when a prior region already covers all its members. */
export function collapseCountriesForLabel(selected: string[], present: string[]): string[] {
  const sel = new Set(selected);
  const covered = new Set<string>();
  const out: string[] = [];
  // Widest first — deliberate deviation from REGION_ORDER (which is UI order).
  for (const label of ["Europe", "North America", "DACH", "BENELUX", "USA"]) {
    const set = REGION_COUNTRIES[label];
    const members = present.filter((c) => set.has(normCountry(c)));
    if (!members.length) continue;
    if (!members.every((c) => sel.has(c))) continue;
    if (members.every((c) => covered.has(c))) continue;  // subsumed by a wider pick
    out.push(label);
    members.forEach((c) => covered.add(c));
  }
  for (const c of selected) if (!covered.has(c)) out.push(c);
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
