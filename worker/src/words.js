// Generated display names ("Calm Otter 4821") and a small word filter for names and comments.

const ADJECTIVES = [
  'Calm', 'Brave', 'Bright', 'Clever', 'Swift', 'Quiet', 'Lucky', 'Bold', 'Gentle', 'Keen',
  'Merry', 'Nimble', 'Steady', 'Witty', 'Sunny', 'Misty', 'Rapid', 'Silver', 'Golden', 'Amber',
  'Cosmic', 'Jolly', 'Patient', 'Curious', 'Daring', 'Eager', 'Frosty', 'Humble', 'Lively', 'Noble',
  'Plucky', 'Proud', 'Rustic', 'Sly', 'Snowy', 'Stormy', 'Tidy', 'Vivid', 'Wise', 'Zesty',
];
const ANIMALS = [
  'Otter', 'Falcon', 'Badger', 'Heron', 'Lynx', 'Panda', 'Raven', 'Tiger', 'Walrus', 'Yak',
  'Beaver', 'Bison', 'Cobra', 'Crane', 'Dingo', 'Eagle', 'Ferret', 'Gecko', 'Hare', 'Ibis',
  'Jackal', 'Koala', 'Lemur', 'Marmot', 'Newt', 'Ocelot', 'Puffin', 'Quokka', 'Robin', 'Seal',
  'Stoat', 'Tapir', 'Toucan', 'Viper', 'Wombat', 'Wolf', 'Zebra', 'Moose', 'Owl', 'Fox',
];

export function generateName() {
  const r = crypto.getRandomValues(new Uint32Array(3));
  const n = 1000 + (r[2] % 9000);
  return `${ADJECTIVES[r[0] % ADJECTIVES.length]} ${ANIMALS[r[1] % ANIMALS.length]} ${n}`;
}

// Substring-matched after normalisation (lowercase, leetspeak folded, non-letters removed).
// Kept short and unambiguous to avoid false positives on ordinary words.
const BLOCKED_SUBSTRINGS = [
  'fuck', 'shit', 'nigger', 'nigga', 'faggot', 'retard', 'whore', 'slut', 'bitch',
  'rapist', 'hitler', 'tranny', 'porn', 'pussy', 'asshole', 'motherf', 'wanker',
];
// Whole-word matches only (would false-positive as substrings: "class", "Scunthorpe", ...).
const BLOCKED_WORDS = new Set([
  'ass', 'cum', 'fag', 'fags', 'rape', 'kys', 'twat', 'coon', 'jizz', 'cunt', 'cunts', 'dick', 'dicks',
  'cock', 'cocks', 'spic', 'chink', 'kike', 'pedo', 'nazi', 'nazis', 'wank', 'bastard',
]);
// Reserved for names only (impersonation of the benchmark or its agents).
const RESERVED_NAME_WORDS = [
  'admin', 'operator', 'moderator', 'agentpit', 'official', 'claude', 'codex', 'grok', 'gemini',
  'anthropic', 'openai', 'support',
];

const LEET = { 0: 'o', 1: 'i', 3: 'e', 4: 'a', 5: 's', 7: 't', 8: 'b', '@': 'a', $: 's', '!': 'i' };

function fold(s) {
  return s
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[0134578@$!]/g, (c) => LEET[c] ?? c);
}

export function containsBlocked(text) {
  const folded = fold(String(text));
  const squashed = folded.replace(/[^a-z]/g, '');
  if (BLOCKED_SUBSTRINGS.some((w) => squashed.includes(w))) return true;
  const words = folded.split(/[^a-z]+/).filter(Boolean);
  return words.some((w) => BLOCKED_WORDS.has(w));
}

// Validates and normalises a requested display name. Returns {name} or {error}.
export function checkName(raw) {
  if (typeof raw !== 'string') return { error: 'name must be a string' };
  const name = raw.normalize('NFC').replace(/\s+/g, ' ').trim();
  if (name.length < 3 || name.length > 24) return { error: 'name must be 3-24 characters' };
  if (!/^[\p{L}\p{N}][\p{L}\p{N} ._'-]*$/u.test(name)) {
    return { error: 'letters, digits, spaces and . _ \' - only, starting with a letter or digit' };
  }
  if (containsBlocked(name)) return { error: 'name not allowed' };
  const squashed = fold(name).replace(/[^a-z]/g, '');
  if (RESERVED_NAME_WORDS.some((w) => squashed.includes(w))) return { error: 'name reserved' };
  return { name };
}
