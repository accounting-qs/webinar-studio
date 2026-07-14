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
  USA: new Set([
    "usa", "us", "u.s.", "u.s.a.", "united states", "united states of america", "america",
  ]),
  Canada: new Set(["canada", "ca"]),
};

export const REGION_ORDER = ["Europe", "USA", "Canada"];

export const normCountry = (c: string) => c.trim().toLowerCase();

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
