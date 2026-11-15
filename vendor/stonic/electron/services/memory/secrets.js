// Memory Core ka chowkidar — baat-cheet likhe jane se PEHLE us mein se raaz nikalna.
//
// ════════════════════════════════════════════════════════════════════════════
//  KYUN, AUR KITNI SAKHTI SE
// ════════════════════════════════════════════════════════════════════════════
//
//  Memory Core har turn hoobahoo aur hamesha ke liye rakhta hai, aur file par
//  koi tala nahi (saadi SQLite, user ke apne AppData mein). Is liye password,
//  OTP ya card number disk tak pohanchna hi nahi chahiye — ek dafa likha gaya
//  to backup aur har app update ke saath aage chalta rahega.
//
//  Ulta nuqsan bhi utna hi bura hai: phone number, tareekh, qeemat, saal, order
//  number, pata — ye sab JAAIZ yaadein hain. Inhein mita dena bug hai. Is liye
//  yahan ka usool "shak ho to chhor do" hai: sirf hindse dekh kar kuch nahi
//  mitta, saath mein saboot chahiye — hisaab (Luhn, mod-97), jani pehchani
//  shakl (CNIC ke dash, key ka prefix), ya bilkul paas koi lafz ("otp", "pin").
//
// ── NISHAN KI SHAKL EK MUAHIDA HAI ──────────────────────────────────────────
//
//  Raaz ki jagah "[redacted <kind>]" likha jata hai aur wohi database mein
//  rehta hai. Aaj ki app ka likha hua nishan 5 saal baad ki app bhi parhegi
//  (store.js isi shakl ko pehchanta hai), is liye ye shakl KABHI na badlo.
//  Naya kind add karna theek hai; purane ka naam badalna nahi.
//
// ── YE FILE ELECTRON AUR DATABASE DONO KE BAGHAIR HAI ───────────────────────
//
//  Sirf text andar, text bahar — is liye `node --test` se seconds mein jaanchi
//  ja sakti hai (secrets.test.js). Ye har turn par chalti hai, is liye har
//  regex aisa rakha gaya hai ke 50,000 huroof ki ek hi qataar par bhi na atke:
//  jahan dhoondna lamba ho sakta tha wahan hadd lagi hai ({0,24}, slice).

const KINDS = Object.freeze([
  'card', 'cvv', 'otp', 'password', 'api_key', 'private_key', 'cnic', 'iban', 'bank_account',
]);

// Do qaide ek hi jagah par lagen to naam us ka chalta hai jo zyada pakka hai.
const RANK = {
  private_key: 0, api_key: 1, iban: 2, cnic: 3, bank_account: 4, card: 5, cvv: 6, otp: 7, password: 8,
};

const placeholder = (kind) => `[redacted ${kind}]`;
const PLACEHOLDER = /\[redacted [a-z_]+\]/g;
// Pehle se laga nishan jaanch ke waqt is se dhak diya jata hai — warna
// "[redacted otp]" ka apna "otp" agle number ko OTP bana deta. Ye deewar bhi
// hai: keyword aur number ke beech nishan aa jaye to wo number us keyword ka nahi.
const MASK = '\uFFFC';
// Ek raaz hatne se doosra saamne aa sakta hai ("pin 1234 <card>"), is liye
// jaanch tab tak dohrai jati hai jab tak kuch naya na mile. Isi se "do dafa
// chalao, natija wohi" ki zamanat milti hai.
const MAX_PASSES = 6;

// `\b` sirf angrezi huroof samajhta hai; Urdu aur Hindi ke lafz ke liye apni hadd.
const WB = String.raw`(?<![\p{L}\p{M}\p{N}_])`;
const WE = String.raw`(?![\p{L}\p{M}_])`;        // hindsa chipka ho sakta hai: "pin1234"
const WEN = String.raw`(?![\p{L}\p{M}\p{N}_])`;

// Urdu ke teen huroof Arabi keyboard se doosre code ke saath aate hain
// (ک/ك, ی/ي, ہ/ه/ھ) — dekhne mein ek, likhne mein alag.
const K = '[کك]';
const Y = '[یي]';
const H = '[ہهھ]';

const COPULA = `is|was|hai|hay|hy|he|${H}ے|है`;
const POSSESSIVE = String.raw`your|my|the|mera|meri|apka|aapka|aap[ \t]ka|tumhara|hamara|humara|م${Y}را|آپ[ \t]${K}ا|मेरा|आपका`;

const words = (list) => new Set(list.split(/\s+/).filter(Boolean));

// ════════════════════════════════════════════════════════════════════════════
//  TAYYARI
// ════════════════════════════════════════════════════════════════════════════

// Urdu (۰-۹), Arabi (٠-٩) aur Hindi (०-९) ke hindse. Har ek ki jagah ek hi
// angrezi hindsa aata hai, is liye jagahein (index) asal text wali hi rehti hain.
//
// Isi tarah web page ya bank ki app se copy kiya hua number "na tootne wali"
// space (NBSP) aur lambi dash ke saath aata hai — dekhne mein wohi card ya
// CNIC, magar code alag, aur har qaida chup-chaap chook jata. Ye bhi ek ke
// badle ek hi badalte hain.
const LOOKALIKE = /[\u0660-\u0669\u06F0-\u06F9\u0966-\u096F\u00A0\u2007\u2009\u202F\u2010-\u2013\u2212]/g;

function prepare(text) {
  let s = text;
  if (s.includes('[redacted ')) s = s.replace(PLACEHOLDER, (m) => MASK.repeat(m.length));
  return s.replace(LOOKALIKE, (ch) => {
    const c = ch.charCodeAt(0);
    if (c === 0x00A0 || c === 0x2007 || c === 0x2009 || c === 0x202F) return ' ';
    if (c >= 0x2010) return '-';
    const zero = c >= 0x0966 ? 0x0966 : c >= 0x06F0 ? 0x06F0 : 0x0660;
    return String.fromCharCode(48 + c - zero);
  });
}

const WORD_BEFORE = /([\p{L}\p{M}\p{N}]+)[ \t-]*$/u;
// "error ka code", "item ki pin" — Roman Urdu mein asal lafz "ka/ki/ke" se pehle hota hai.
const LINKERS = new Set(['ka', 'ki', 'ke', 'کا', 'کی', 'کے', 'का', 'की', 'के']);
function wordBefore(s, at, skipLinker) {
  const win = s.slice(Math.max(0, at - 40), at);
  const m = WORD_BEFORE.exec(win);
  if (!m) return '';
  const word = m[1].toLowerCase();
  if (!skipLinker || !LINKERS.has(word)) return word;
  const prior = WORD_BEFORE.exec(win.slice(0, m.index));
  return prior ? prior[1].toLowerCase() : word;
}

// ── Ginti ya raqam, raaz nahi ───────────────────────────────────────────────
//
//  "OTP wali transaction Rs 5000 ki thi", "ye code 2019 mein likha" — keyword
//  paas hai, phir bhi number raqam ya saal hai. Aage/peechhe ka lafz bata deta hai.

const MONEY_BEFORE = /(?:rs\.?|pkr|inr|usd|rupees|rupay|روپے|रुपये|[$₨₹€£])[ \t]*$/i;
const NEXT_WORD = /^[ \t]*(%|[\p{L}\p{M}]+)/u;
const UNIT_WORDS = words(`
  rupees rupee rupay rupaye rupiya rupaiye rs pkr inr usd dollars dollar percent
  saal sal year years month months mahine mahina din day days ghante ghanta hour hours hrs
  minute minutes min mins second seconds sec secs baje bajay am pm
  lines line users user log dafa baar times km kg gb mb px items item characters chars digits
  روپے سال بجے فیصد دن منٹ रुपये साल बजे दिन मिनट`);
// "mein" ka matlab "andar" bhi hai aur "main (khud)" bhi ("otp 482913 main ne
// dala"), is liye ye lafz sirf saal jaise number (1900–2099) ke baad maane jate hain.
const YEAR = /^(?:19|20)\d\d$/;
const AFTER_YEAR = words(`
  mein main me men se tak ka ki ke ko wala wali
  میں سے تک کا کی کے کو में से तक का की के को`);

function looksLikeQuantity(s, start, end) {
  if (MONEY_BEFORE.test(s.slice(Math.max(0, start - 12), start))) return true;
  const m = NEXT_WORD.exec(s.slice(end, end + 24));
  if (!m) return false;
  const next = m[1].toLowerCase();
  return next === '%' || UNIT_WORDS.has(next) || (AFTER_YEAR.has(next) && YEAR.test(s.slice(start, end)));
}

// ════════════════════════════════════════════════════════════════════════════
//  PRIVATE KEY (PEM)
// ════════════════════════════════════════════════════════════════════════════

const PEM_BEGIN = /-----BEGIN ([A-Z0-9 ]{0,40}?)PRIVATE KEY( BLOCK)?-----/g;
// END wali line na mile (adha paste) to bhi key ka maal hatana hai — lekin
// sirf base64 jaise lambe tukre, aage ki aam baat nahi.
const PEM_LOOSE_BODY = /(?:\s*[A-Za-z0-9+/=]{16,})*/y;

function findPrivateKeys(s, add) {
  if (!s.includes('-----BEGIN')) return;
  const noEnd = new Set();   // jo END ek dafa na mila wo aage bhi nahi milega — dobara mat dhoondo
  PEM_BEGIN.lastIndex = 0;
  let m;
  while ((m = PEM_BEGIN.exec(s))) {
    const endLine = `-----END ${m[1]}PRIVATE KEY${m[2] || ''}-----`;
    let end = -1;
    if (!noEnd.has(endLine)) {
      const at = s.indexOf(endLine, PEM_BEGIN.lastIndex);
      if (at === -1) noEnd.add(endLine);
      else end = at + endLine.length;
    }
    if (end === -1) {
      PEM_LOOSE_BODY.lastIndex = PEM_BEGIN.lastIndex;
      PEM_LOOSE_BODY.exec(s);
      end = Math.max(PEM_BEGIN.lastIndex, PEM_LOOSE_BODY.lastIndex);
    }
    add(m.index, end, 'private_key');
    PEM_BEGIN.lastIndex = end;
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  API KEYS
// ════════════════════════════════════════════════════════════════════════════

const KEY_PREFIXED = new RegExp(`(?<![A-Za-z0-9_-])(?:${[
  'sk-[A-Za-z0-9_-]{20,}',                              // OpenAI, Anthropic (sk-ant-), OpenRouter (sk-or-)
  '(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}',        // Stripe
  'whsec_[A-Za-z0-9+/=_-]{16,}',
  'AIza[A-Za-z0-9_-]{30,}',                             // Google / Gemini
  'gh[pous]_[A-Za-z0-9]{20,}',
  'github_pat_[A-Za-z0-9_]{20,}',
  'xox[bpa]-[A-Za-z0-9-]{10,}',                         // Slack
  'AKIA[A-Z0-9]{16}(?![A-Za-z0-9])',                    // AWS
  'polar_[A-Za-z0-9_]{20,}',
  'hf_[A-Za-z0-9]{20,}',
  'gsk_[A-Za-z0-9]{20,}',                               // Groq — Stonic ki apni settings mein lagti hai
  'sb_secret_[A-Za-z0-9_-]{16,}',                       // Supabase
  'eyJ[A-Za-z0-9_-]{5,}\\.[A-Za-z0-9_-]{5,}\\.[A-Za-z0-9_-]*',   // JWT
].join('|')})`, 'g');
// Ye teen prefix aam lafzon jaise hain ("polar_bear_photos_..."), is liye in
// ke saath kam az kam ek hindsa bhi chahiye.
const KEY_WORDY_PREFIX = /^(?:sk-|polar_|hf_)/;

const KEY_GENERIC = /(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}/g;
const KEY_MIN_ENTROPY = 3.5;
// Hex ke sirf 16 huroof hain, us ki entropy 4.0 se oopar ja hi nahi sakti. 3.5
// ki hadd par har paanchvin asli 32-hex key (Twilio, OpenWeather, Deepgram
// jaisi) bach nikalti thi — wohi qaida, kabhi mitata kabhi nahi. Hex ke liye
// hadd us ke apne paimane par rakhi gayi hai.
const KEY_MIN_ENTROPY_HEX = 3.0;
const HEX_ONLY = /^(?:0x)?[0-9a-fA-F]+$/;
const UUID_INSIDE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;
// Lafzon se bani cheez ka har tukra: lafz, number, "python3", "1080p".
const WORDLIKE_PART = /^(?:[A-Za-z]+\d{0,4}|\d+|\d{1,4}[A-Za-z]{1,5})$/;
const WORD_CHUNK = /[A-Z]?[a-z]{3,}/g;
const FILE_EXT_AFTER = /^\.[A-Za-z][A-Za-z0-9]{1,4}(?![A-Za-z0-9])/;
// Commit hash aur checksum bhi lambe aur be-tarteeb hote hain, magar raaz nahi.
const HASH_LABEL_BEFORE = /\b(?:commit|sha-?\d*|md5|hash|checksum)\b[\s:=#]{0,3}$/i;

function entropy(str) {
  const counts = new Map();
  for (let i = 0; i < str.length; i++) {
    const c = str.charCodeAt(i);
    counts.set(c, (counts.get(c) || 0) + 1);
  }
  let bits = 0;
  for (const n of counts.values()) {
    const p = n / str.length;
    bits -= p * Math.log2(p);
  }
  return bits;
}

/** URL ka slug ya file ka naam: har tukra apni jagah ek lafz ya number hai. */
const madeOfWords = (token) => token.split(/[-_]+/).every((part) => !part || WORDLIKE_PART.test(part));

/** Prefix ke baghair wala token: lamba, huroof + hindse mile hue, aur be-tarteeb. */
function looksRandom(token) {
  // UUID pehchan hai, chaabi nahi — URL ke andar ho ya akela.
  const rest = token.includes('-') ? token.replace(UUID_INSIDE, '') : token;
  if (rest.length < 32 || !/[A-Za-z]/.test(rest) || !/\d/.test(rest)) return false;
  if (madeOfWords(rest)) return false;
  // Code ka lamba naam ("handleGen3SubscriptionRenewalWebhookEvent"): hindse ek do
  // jagah, baqi sab parhne laiq lafz. Asli chaabi mein hindse jagah jagah bikhre hote hain.
  if ((rest.match(/\d+/g) || []).length <= 2) {
    let inWords = 0;
    for (const chunk of rest.match(WORD_CHUNK) || []) inWords += chunk.length;
    if (inWords >= rest.replace(/[^A-Za-z]/g, '').length * 0.8) return false;
  }
  return entropy(rest) >= (HEX_ONLY.test(rest) ? KEY_MIN_ENTROPY_HEX : KEY_MIN_ENTROPY);
}

function findApiKeys(s, add, hasDigit) {
  for (const m of s.matchAll(KEY_PREFIXED)) {
    // "polar_bear_and_2_penguins_photos" mein hindsa hai, phir bhi ye lafz hi hain.
    if (KEY_WORDY_PREFIX.test(m[0]) && (!/\d/.test(m[0]) || madeOfWords(m[0]))) continue;
    add(m.index, m.index + m[0].length, 'api_key');
  }
  if (!hasDigit) return;
  for (const m of s.matchAll(KEY_GENERIC)) {
    const end = m.index + m[0].length;
    if (!looksRandom(m[0])) continue;
    if (FILE_EXT_AFTER.test(s.slice(end, end + 8))) continue;
    if (HASH_LABEL_BEFORE.test(s.slice(Math.max(0, m.index - 16), m.index))) continue;
    add(m.index, end, 'api_key');
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  PASSWORD
// ════════════════════════════════════════════════════════════════════════════
//
//  "password" ke baad wala lafz aksar raaz nahi hota ("password bhool gaya",
//  "password is wrong"). Is liye teen darje:
//
//    ":" ya "="             -> jo bhi aaye, siwaye aam lafzon ke ("password is: X" bhi yehi hai)
//    "is" / "hai" pehle     -> hindse/alamat wala token hamesha; saada lafz sirf
//                              tab jab jumla wahin khatam ho ("password is sunshine.")
//    seedha saath / "X hai" -> sirf hindse ya alamat wala token ("Abc123"); saada
//                              lafz sirf poori shakl mein: "mera password sunshine hai"

// "pass" akela Roman Urdu mein "paas" hai ("mere pass hai 500"), aur "pwd"
// terminal ka command — ye chhote naam sirf ":" ya "=" ke saath maane jate hain.
// Config aur .env ke naam kisi aur lafz se jure hote hain (DB_PASSWORD=...,
// "smtpPassword": ...); wahan bhi ":" ya "=" hi saboot hai. "pass" sirf "_" ke
// baad, warna "compass: north" bhi pakra jata.
const PW_ASSIGNED = String.raw`(?=["']?[ \t]*[:=])`;
const PW_KEYWORD = new RegExp(
  String.raw`(?:${WB}(?:pass?w(?:o|a)?rd|pass[ -]word|pass[ -]?code|passphrase|پاس\s?ورڈ|پاسورڈ|पासवर्ड|(?:passwd|pwd|pass|pw)${PW_ASSIGNED})`
  + String.raw`|(?:(?<=[A-Za-z0-9_])(?:password|passwd)|(?<=_)(?:pwd|pass))${PW_ASSIGNED})${WE}`, 'giu');
// Shuru ka quote JSON ke liye hai: {"password": "X"} mein naam ke baad `"` aata hai, phir ":".
const PW_HEAD = new RegExp(
  String.raw`["'”’]?[ \t]*(?:(?:for|of)[ \t]+(?:[^\s.!?:=]{1,24}[ \t]+){1,4}?)?`
  + String.raw`(?:(?<cop>${COPULA})${WEN}(?:[ \t]*(?<csym>[:=]+))?|(?<sym>[:=]+[->]?|[-–—]+>?(?=[ \t])))[ \t]*`, 'yiu');
const PW_TOKEN = /"([^"\n]{1,80})"|'([^'\n]{1,80})'|“([^”\n]{1,80})”|`([^`\n]{1,80})`|(\S{1,129})/y;
const PW_CLOSE = new RegExp(String.raw`[ \t]+(?:hai|hay|hy|he|tha|${H}ے|تھا|है|था)${WEN}`, 'yiu');
const PW_CLAUSE_END = new RegExp(String.raw`^[ \t]*(?:$|[\n.,;!?۔،]|(?:and|aur|or|ya|but|lekin|magar|so|then|phir|hai|اور|${H}ے|और|है)${WEN})`, 'iu');
const PW_HAS_SIGNAL = /[\d!@#$%^&*+=?~|\\_]/;
// URL, raasta, sitare ("********"), "100%", "5/10", "12:30" — ye password nahi, kuch aur hai.
const PW_NOT_A_VALUE = /^(?:[a-z][a-z0-9+.-]*:\/\/|www\.|[a-z]:[\\/]|\.{0,2}\/|~\/|[*•●·]+$|\d+%$|\d+(?:[/:.,-]\d+)+$)/i;
// "password to Abc123 hai", "password ye hai Abc123" — "to" aur "ye" sirf bharti
// ke lafz hain, token un ke baad hai.
const PW_PARTICLE = new RegExp(String.raw`^(?:[ \t]+(?:to|toh|tou|ye|yeh|yah|تو|${Y}${H}|तो|यह|ये)(?=[ \t])){1,2}`, 'iu');
const EMAIL = /^[^\s@]+@[^\s@]+\.[A-Za-z]{2,}$/;
const PW_MAX = 128;
// Token ke aakhir ka nuqta/bracket jumle ka hai, password ka nahi. "!" aur "?"
// alag hain: "Abc123!" mein password ka hissa, "galat hai kya?" mein jumle ka.
const PW_TAIL = /[)\]}>"'”’.,;:۔،]+$/;
const PW_TAIL_LOUD = /[)\]}>"'”’.,;:۔،!?]+$/;
// `password = req.body.password`, `password = input(...)` — code ka tukra, raaz nahi.
// Sirf "=" ke saath aur sirf hindson ke baghair: "ali.khan123" asli password ho sakta hai.
const PW_CODE_EXPR = /^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*[([]|^[A-Za-z_$]+(?:\.[A-Za-z_$]+)+$/;
// "password 0300 1234567 par bhejo" — number manzil hai (kahan bhejna hai), password nahi.
const PW_PHONE = /^(?:0|92|0092)3\d{9}$/;
const PW_SENT_TO = new RegExp(String.raw`^[ \t]+(?:par|pe|per|ko|پر|${K}و|पर|को)${WEN}`, 'iu');
const PW_SELF = /^pass(?:word|code)?$/;
const PW_MORE_DIGITS = /(?:[ -]\d+)+/y;
// Database ka link: "postgres://user:PASSWORD@host". Yahan "password" lafz hota
// hi nahi — "://naam:kuch@" ki shakl hi saboot hai. Password ke andar "@" ho
// ("Lahore@2026") to aakhri "@" tak sab password hai.
const URL_CREDENTIALS = /:\/\/[^\s:@/]{0,64}:([^\s/]{3,128})@/g;

const PW_ORDINARY = words(`
  the a an my your his her their our its this that these those it he she we they you me
  not no yes too very so also still now just only always never ever what which where when why how who
  same different there here in on at for of to with by from as like than then and or but if because
  being been be am are was were is going gone get got set kept stored saved written required needed need
  protected encrypted hashed case sensitive something nothing anything everything unknown known
  secret private public personal confidential important compromised leaked stolen hacked expired
  changed updated reset shared lost forgotten forgot working fine done ready available null none undefined
  correct incorrect wrong invalid valid right true false empty blank missing hidden visible long short
  old new bad good weak strong simple easy hard secure safe unsafe optional mandatory default temporary
  random unique please pls thanks ok okay sure probably maybe actually really already mine yours
  hai hain tha thi ho hoga hogi hota hoti hua hui gaya gayi kya kia kyun kyu kab kahan kidhar kaise
  kaisa kaun kon kis jis jo wo woh ye yeh yah us un mera meri mere tera teri tere tumhara tumhari
  apka aapka apna apni apne uska uski iska iski inka unka hamara humara sabka koi kuch sab har ek
  nahi nahin nhi nai na mat haan han ji galat ghalat sahi theek thik purana purani naya nayi lamba
  chota bara mushkil asaan asan kamzor mazboot mehfooz yaad yad bhool bhul maloom malum pata zaroori
  lazmi wohi wahi yehi yahi aisa waisa itna bohat bohot bahut kafi thora bilkul sirf bas abhi ab phir
  bhi hi toh tou lekin magar aur ya par pe mein main ko se ka ki ke tak liye wala wali bhai yaar yar
  dost sir janab shayad zaroor change badal badlo badla badalna likho likha likh batao bata btao bta
  bataya dalo daalo dala daal lagao laga lagaya rakho rakha rakh bhejo bheja bhej send do de dena dedo
  diya dia karo kar karna karni kiya chahiye chahye mila dekho dekha yahan wahan alag khali gayab band
  save leak hack expire block blocked lock locked open closed enabled disabled on off pehle baad
  string str text varchar char boolean bool number int any object type nil
  show hide view copy paste enter click tap dikhao chupao dabao`);
// "mera password X hai" / "wifi ka password X hai" — kis ka password, ye pehle aa chuka ho.
const PW_OWNER = words(`
  mera meri mere my our apna apni hamara humara tumhara apka aapka ka ki ke
  میرا میری اپنا ہمارا کا کی मेरा मेरी अपना हमारा का की`);

/** "wrong", "case-sensitive", "wrong password" — har tukra aam lafz ho to poora token aam hai. */
function isOrdinary(token) {
  const lower = token.toLowerCase();
  if (PW_ORDINARY.has(lower)) return true;
  const parts = lower.split(/[\s-]+/).filter(Boolean);
  return parts.length > 1 && parts.every((part) => PW_ORDINARY.has(part) || PW_SELF.test(part));
}

function findPasswords(s, add) {
  for (const m of s.matchAll(PW_KEYWORD)) {
    let at = m.index + m[0].length;
    const particle = PW_PARTICLE.exec(s.slice(at, at + 16));
    if (particle) at += particle[0].length;
    PW_HEAD.lastIndex = at;
    const head = PW_HEAD.exec(s);
    if (head) at = PW_HEAD.lastIndex;
    else {
      // Seedha saath wala token: beech mein khaali jagah lazmi ("password-reset" nahi).
      const gap = /^[ \t]+/.exec(s.slice(at, at + 8));
      if (!gap) continue;
      at += gap[0].length;
    }
    PW_TOKEN.lastIndex = at;
    const t = PW_TOKEN.exec(s);
    if (!t) continue;
    const rawEnd = at + t[0].length;
    const quoted = t[5] === undefined;
    let token = quoted ? t[0].slice(1, -1) : t[5];
    let start = quoted ? at + 1 : at;

    if (!quoted) {
      // Brackets aur jumle ka nuqta token ka hissa nahi.
      const lead = /^[("'“‘[{<]+/.exec(token);
      if (lead) { token = token.slice(lead[0].length); start += lead[0].length; }
      // "?password=Abc123&user=ali" — "&" se aage doosra parameter hai, password nahi.
      if (head && head.groups.sym && head.groups.sym.includes('=')) {
        const next = token.search(/&[A-Za-z_][\w.-]*=/);
        if (next > 0) token = token.slice(0, next);
      }
      const core = token.replace(PW_TAIL_LOUD, '');
      token = PW_HAS_SIGNAL.test(core) ? token.replace(PW_TAIL, '') : core;
    }
    if (!token || token.length > PW_MAX || token.includes(MASK) || PW_NOT_A_VALUE.test(token)) continue;

    let end = start + token.length;
    let tail = rawEnd;   // token ke baad jumla yahan se aage chalta hai
    const signal = PW_HAS_SIGNAL.test(token);
    const digitsOnly = /^\d+$/.test(token);
    if (digitsOnly) {
      // Number aage bhi chalta ho ("0300 1234567") to wo ek hi cheez hai — adha
      // number mitana sab se bura natija hai, is liye faisla poore number par hota hai.
      PW_MORE_DIGITS.lastIndex = end;
      if (PW_MORE_DIGITS.test(s)) { end = PW_MORE_DIGITS.lastIndex; tail = end; }
      // "pass: 104" (test ka natija), "password 2024 mein badla" — ginti aur saal password nahi.
      if (end - start < 4 || end - start > PW_MAX || looksLikeQuantity(s, start, end)) continue;
    }
    const split = end - start > token.length;
    const ordinary = isOrdinary(token);
    const assign = head ? head.groups.sym || head.groups.csym : '';

    let ok;
    if (assign) {
      // "==" muqabla hai, qeemat dena nahi.
      if (/^={2,}/.test(assign)) continue;
      if (!quoted && assign.includes('=') && PW_CODE_EXPR.test(token)) continue;
      // Urdu/Hindi ka saada lafz password nahi hota ("پاس ورڈ: درکار").
      ok = token.length >= 3 && !ordinary && (signal || !/^[^\x00-\x7F]+$/.test(token));
    } else if (head) {
      if (signal) ok = token.length >= 3;
      else if (quoted) ok = !ordinary;
      else {
        ok = token.length >= 3 && !ordinary && /^[A-Za-z'’-]+$/.test(token)
          && (rawEnd > end || PW_CLAUSE_END.test(s.slice(rawEnd, rawEnd + 12)));
      }
    } else {
      PW_CLOSE.lastIndex = tail;
      const closed = PW_CLOSE.test(s);
      if (signal) {
        ok = token.length >= (closed ? 3 : digitsOnly ? 4 : 6) && !EMAIL.test(token);
        // "password 0300 1234567 par bhejo" — "hai" ke baghair ye number manzil hai, password nahi.
        if (digitsOnly && !closed && (split || PW_PHONE.test(token) || PW_SENT_TO.test(s.slice(end, end + 8)))) ok = false;
      } else if (quoted) {
        // 'gmail ka password "meri billi" hai' — quotes khud bata rahe hain ke ye qeemat hai.
        ok = token.length >= 3 && !ordinary && (closed || PW_OWNER.has(wordBefore(s, m.index)));
      // Saada lafz: sirf poori shakl mein ("mera password sunshine hai").
      } else {
        ok = closed && token.length >= 4 && !ordinary && /^[A-Za-z]+$/.test(token)
          && PW_OWNER.has(wordBefore(s, m.index));
      }
    }
    if (ok) add(start, end, 'password');
  }

  if (!s.includes('://')) return;
  for (const m of s.matchAll(URL_CREDENTIALS)) {
    const start = m.index + m[0].length - 1 - m[1].length;
    if (!m[1].includes(MASK)) add(start, start + m[1].length, 'password');
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  CARD
// ════════════════════════════════════════════════════════════════════════════

const DIGIT_RUN = /\d(?:[ -]?\d)*/g;
// Asli card inhi hindson se shuru hote hain (Visa 4, Mastercard 5 aur 22–27,
// Amex 3, UnionPay 6, PayPak 22). Baqi shuruaat kuch aur hai: 0 = "0092..."
// wala phone, 1 = ghari ka timestamp ya order number, 20 = "2026..." tareekh.
const CARD_START = /^(?:2[2-7]|[3-6])/;
const CARD_GROUPINGS = new Set(['4-4-4-4', '4-4-4-4-3', '4-6-5', '4-6-4']);
// IMEI bhi 15 hindse ka hota hai aur Luhn pass karta hai — magar wo phone ki pehchan hai.
const IMEI_BEFORE = /\bimei\b[^\d\n]{0,20}$/i;

function luhn(digits) {
  let sum = 0;
  let double = false;
  for (let i = digits.length - 1; i >= 0; i--) {
    let n = digits.charCodeAt(i) - 48;
    if (double) { n *= 2; if (n > 9) n -= 9; }
    sum += n;
    double = !double;
  }
  return sum % 10 === 0;
}

/**
 * Hindson ke groups card jaise bante hain ya nahi.
 *
 * Kisi lambi qataar ke BEECH mein se sirf card ki jani pehchani shakl uthai
 * jati hai (4-4-4-4 waghera). Poori qataar ho to chhote barabar groups bhi
 * chalte hain ("4 1 1 1 ...", "41 11 11 ..." — bol kar likhwaya hua). Is se do
 * phone number, do tareekhein ya do order number saath aa kar ittefaq se card
 * nahi bante ("50363238 20171208" card ki shakl nahi).
 */
function cardShape(groups, from, to, whole) {
  const count = to - from + 1;
  if (count === 1) return true;
  if (!whole && count > 5) return false;
  const lens = [];
  for (let i = from; i <= to; i++) lens.push(groups[i].len);
  if (CARD_GROUPINGS.has(lens.join('-'))) return true;
  const size = lens[0];
  if (!whole || size > 4) return false;
  return lens.every((len, i) => (i < count - 1 ? len === size : len <= size));
}

function findCards(s, add) {
  for (const m of s.matchAll(DIGIT_RUN)) {
    const run = m[0];
    if (run.length < 13) continue;
    const at = m.index;
    const prev = s[at - 1] || '';
    const next = s[at + run.length] || '';
    // "+92 ..." = phone; huroof se chipka = koi ID; "3.1415..." = ashaariya.
    if (/[A-Za-z_+]/.test(prev) || /[A-Za-z_]/.test(next)) continue;
    if ((prev === '.' || prev === ',') && /\d/.test(s[at - 2] || '')) continue;

    const groups = [];
    let groupAt = 0;
    for (let i = 0; i <= run.length; i++) {
      const c = run.charCodeAt(i);
      if (i === run.length || c === 32 || c === 45) {
        groups.push({ at: groupAt, len: i - groupAt });
        groupAt = i + 1;
      }
    }
    if (run.length - (groups.length - 1) < 13) continue;
    if (IMEI_BEFORE.test(s.slice(Math.max(0, at - 28), at))) continue;

    for (let i = 0; i < groups.length; i++) {
      const lead = run.charCodeAt(groups[i].at) - 48;
      if (lead < 2 || lead > 6) continue;
      const ends = [];
      let digits = 0;
      for (let j = i; j < groups.length && digits + groups[j].len <= 19; j++) {
        digits += groups[j].len;
        if (digits >= 13) ends.push(j);
      }
      // Sab se lambi khirki pehle — card ke saath chipka CVV bhi saath hi chala jaye.
      for (let k = ends.length - 1; k >= 0; k--) {
        const j = ends[k];
        if (!cardShape(groups, i, j, i === 0 && j === groups.length - 1)) continue;
        const from = groups[i].at;
        const to = groups[j].at + groups[j].len;
        const number = run.slice(from, to).replace(/[ -]/g, '');
        if (!CARD_START.test(number) || !luhn(number)) continue;
        add(at + from, at + to, 'card');
        i = j;
        break;
      }
    }
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  CVV
// ════════════════════════════════════════════════════════════════════════════

const CVV_KEYWORD = new RegExp(String.raw`${WB}(?:cvv2?|cvc2?|cv2|security[ -]?code|card[ -]?verification[ -]?(?:code|value|number)|س${Y}\s?و${Y}\s?و${Y}|س${Y}?${K}${Y}ورٹ${Y}\s?${K}وڈ|सीवीवी|सिक्योरिटी\s?कोड)${WE}`, 'giu');
const CVV_AFTER = /[^\d\n.!?۔,;،؛\uFFFC]{0,16}?(\d{3,4})(?![ -]?\d)(?![.,:/]\d)(?![A-Za-z_])/y;

function findCvvs(s, add) {
  for (const m of s.matchAll(CVV_KEYWORD)) {
    CVV_AFTER.lastIndex = m.index + m[0].length;
    const hit = CVV_AFTER.exec(s);
    if (!hit) continue;
    const end = CVV_AFTER.lastIndex;
    const start = end - hit[1].length;
    if (!looksLikeQuantity(s, start, end)) add(start, end, 'cvv');
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  OTP / PIN
// ════════════════════════════════════════════════════════════════════════════
//
//  Keyword do qism ke hain:
//
//    pakka  — "otp", "passcode", "verification code", "atm pin": in ke baad
//             isi jumle mein thori door tak ka code bhi uthaya jata hai
//             ("Your OTP for login is 482913").
//    kacha  — akela "code" ya "pin": ye lafz din mein sau dafa aata hai ("ye
//             code line 1200 par crash"), is liye number bilkul saath hona
//             chahiye, beech mein sirf "is / hai / number / :" jaise lafz.
//
//  Aur kuch "code" raaz hote hi nahi (promo, area, zip, error...) — pichhla
//  lafz dekh kar wo yahin chhoot jate hain.

const OTP_PREFIX = `verification|confirmation|security|login|auth|authentication|2fa|sms|activation|otp|atm|upi|debit|credit|card|sim|transaction|تصد${Y}ق${Y}|و${Y}ر${Y}ف${Y}${K}${Y}شن|س${Y}${K}${Y}ورٹ${Y}|सत्यापन|वेरिफिकेशन|सिक्योरिटी|एटीएम|यूपीआई`;
const OTP_KEYWORD = new RegExp(
  `${WB}(?:`
  + String.raw`(?<strong>o\.?[ ]?t\.?[ ]?p|one[ -]?time[ -]?(?:password|passcode|pin|code)|pass[ -]?code|[mt]-?pin|او\s?ٹ${Y}\s?پ${Y}|ون\s?ٹائم\s?(?:پاس\s?ورڈ|${K}وڈ|پن)|پاس\s?${K}وڈ|ओ\s?टी\s?पी|वन\s?टाइम\s?(?:पासवर्ड|कोड|पिन)|पासकोड)`
  + String.raw`|(?:(?<pre>${OTP_PREFIX})[ -]+)?(?:(?<postal>pin[ -]?code|پن\s?${K}وڈ|पिन\s?कोड)|(?<weak>code|pin|${K}وڈ|پن|कोड|पिन))`
  + `)${WE}`, 'giu');

const CODE_NOT_SECRET = words(`
  promo coupon discount voucher referral refer invite area zip postal post country dialing dial city
  std branch swift sort ifsc hs product item sku model part color colour hex employee student course
  subject tracking booking error status exit response http source qr bar morse dress cheat machine
  binary html css python javascript js java vs studio visual
  پرومو کوپن ڈسکاؤنٹ ایریا زپ پوسٹل پوسٹ کنٹری ایرر سورس آر بار برانچ
  प्रोमो कूपन डिस्काउंट एरिया ज़िप जिप पोस्टल कंट्री एरर सोर्स क्यूआर बार ब्रांच`);

const OTP_WINDOW = 48;
const SENTENCE_END = /[!?۔\n\uFFFC]|(?<!\b(?:rs|no))\.(?=\s|$)/i;
const COPULA_BEFORE = /(?:\b(?:is|hai|hay|hy)|[ہهھ]ے|है|[:=])\s*$/i;
// 4 se 8 hindse. Bol kar likhwaya code tukron mein aata hai ("4 8 2 9 1 3",
// "48 29 13"), aur SMS wala dash ke saath ("482-913").
const CODE_SHAPE = String.raw`(\d{3}-\d{3}|\d(?: ?\d){3,7})(?![ -]?\d)(?![.,:/]\d)(?![A-Za-z_])`;
const CODE_ANYWHERE = new RegExp(String.raw`(?<![\dA-Za-z_+])(?<!\d[ \-.,:/])${CODE_SHAPE}`, 'g');
const CODE_HERE = new RegExp(CODE_SHAPE, 'y');
// "the pin for my card is 4821", "your code for Stonic is 482913" — beech mein
// "kis ka" aa jata hai; "is/hai" tak pohanchna lazmi, aur raste mein hindsa nahi.
const WEAK_FOR = String.raw`(?:[ \t]+(?:for|of)(?:[ \t]+[^\s.!?:=\d]{1,24}){1,3}?(?=[ \t]+(?:is|hai|hay)${WEN}))?`;
const WEAK_GAP = new RegExp(String.raw`${WEAK_FOR}(?:[ \t]*[:=#-]+)?(?:[ \t]+(?:${COPULA}|hain|tha|thi|number|nmbr|num|no|ye|yeh|yah|to|toh|tou|aya|aaya|ayi|aayi|mila|mili|received|${H}${Y}ں|${Y}${H}|نمبر|آ${Y}ا|ملا|यह|नंबर|आया|मिला)${WEN}){0,3}(?:[ \t]*[:=#-]+)?[ \t]*`, 'yiu');
// Keyword se PEHLE wala code: "482913 is your Google verification code",
// "4821 mera pin hai". Kache keyword ke liye "is/hai" ya "my/mera" lazmi —
// warna "2019 ka code" bhi pakra jata.
const CODE_BEFORE_HEAD = String.raw`(?<![\dA-Za-z_+])(?<!\d[ \-.,:/])(\d{4,8})[ \t]+`;
const CODE_BEFORE_OWNER = String.raw`(?:${POSSESSIVE})[ \t]+(?:[A-Za-z]{2,15}[ \t]+)?`;
const CODE_BEFORE_STRONG = new RegExp(String.raw`${CODE_BEFORE_HEAD}(?:(?:${COPULA})[ \t]+)?(?:${CODE_BEFORE_OWNER})?$`, 'iu');
const CODE_BEFORE_WEAK = new RegExp(String.raw`${CODE_BEFORE_HEAD}(?:(?:${COPULA})[ \t]+(?:${CODE_BEFORE_OWNER})?|${CODE_BEFORE_OWNER})$`, 'iu');
// Number apne pichhle lafz ka hai, aage wale keyword ka nahi:
//   "In 2023 my code was messy"  — saal; ye hamesha chhoot jata hai.
//   "order 48213 otp nahi aya"   — order ka number; ye sirf tab jab number aur
//                                  keyword ke beech "is / mera" jaisa koi jor na ho
//                                  ("ye number 4821 mera pin hai" phir bhi raaz hai).
const YEAR_WORD_BEFORE = new RegExp(String.raw`${WB}(?:in|since|from|until|till|by|during|year|saal|sal|san|سال|साल)[ \t]*$`, 'iu');
const NUMBER_LABEL_BEFORE = new RegExp(String.raw`(?:${WB}(?:order|number|no|num|nmbr|room|ticket|invoice|ref|reference|tracking|flat|house|street|plot|gali|makan|model|version|ext|extension|total|roll|bill|page|line|port|آرڈر|نمبر|ऑर्डर|नंबर)|#)[ \t.:#-]*$`, 'iu');

/** Pakke keyword ke baad, isi jumle mein, pehla code — "is/hai/:" wala ho to wohi. */
function codeInWindow(s, from) {
  let win = s.slice(from, from + OTP_WINDOW + 20);
  const stop = win.search(SENTENCE_END);
  if (stop !== -1) win = win.slice(0, stop);
  let first = null;
  for (const m of win.matchAll(CODE_ANYWHERE)) {
    if (m.index >= OTP_WINDOW) break;
    const start = from + m.index;
    const end = start + m[0].length;
    if (looksLikeQuantity(s, start, end)) continue;
    if (COPULA_BEFORE.test(win.slice(0, m.index))) return [start, end];
    if (!first) first = [start, end];
  }
  return first;
}

function findOtps(s, add) {
  for (const m of s.matchAll(OTP_KEYWORD)) {
    const g = m.groups;
    const start = m.index;
    const end = start + m[0].length;
    if (!g.strong && CODE_NOT_SECRET.has(wordBefore(s, start, true))) continue;
    const strong = Boolean(g.strong || g.pre);

    let hit = null;
    if (strong) hit = codeInWindow(s, end);
    else {
      WEAK_GAP.lastIndex = end;
      WEAK_GAP.exec(s);
      CODE_HERE.lastIndex = Math.max(end, WEAK_GAP.lastIndex);
      const code = CODE_HERE.exec(s);
      if (code) {
        const codeAt = CODE_HERE.lastIndex - code[1].length;
        // "pin code 110001" Hindustan ka postal code hai, "pin code 54000" Pakistan
        // ka — 5 ya 6 hindse wala "pin code" pata hai, raaz nahi.
        const digits = code[1].replace(/\D/g, '').length;
        const postal = g.postal && (digits === 5 || digits === 6);
        if (!postal && !looksLikeQuantity(s, codeAt, CODE_HERE.lastIndex)) hit = [codeAt, CODE_HERE.lastIndex];
      }
    }
    if (hit) add(hit[0], hit[1], 'otp');

    if (g.postal) continue;
    const from = Math.max(0, start - 64);
    const before = (strong ? CODE_BEFORE_STRONG : CODE_BEFORE_WEAK).exec(s.slice(from, start));
    // index 0 = tukra number ke beech se kata ho sakta hai, us par bharosa nahi.
    if (!before || (before.index === 0 && from > 0)) continue;
    const codeAt = from + before.index;
    const lead = s.slice(Math.max(0, codeAt - 24), codeAt);
    if (MONEY_BEFORE.test(lead) || YEAR_WORD_BEFORE.test(lead)) continue;
    const bare = before[0].trim().length === before[1].length;
    if (bare && NUMBER_LABEL_BEFORE.test(lead)) continue;
    // "2019 is the code freeze year" — saal jaisa number, aur kache keyword ke
    // baad baat aage chal rahi hai. "1998 mera pin hai" phir bhi pakra jata hai.
    if (!strong && YEAR.test(before[1]) && !PW_CLAUSE_END.test(s.slice(end, end + 12))) continue;
    add(codeAt, codeAt + before[1].length, 'otp');
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  CNIC
// ════════════════════════════════════════════════════════════════════════════

// Dash wali shakl (12345-1234567-1) apni jagah saboot hai. Dash ke baghair 13
// hindse kuch bhi ho sakte hain, is liye wahan paas mein lafz chahiye.
const CNIC_DASHED = /(?<!\d)\d{5}-\d{7}-\d(?!\d)/g;
const CNIC_PLAIN = /(?<![\d+])(?<!\d[ -])(?:\d{13}|\d{5} \d{7} \d)(?![ -]?\d)/g;
const CNIC_KEYWORD = new RegExp(String.raw`${WB}(?:cnic|nic|shanakhti|shnakhti|shinakhti|id[ -]?card|identity[ -]?card|national[ -]?id|شناخت${Y}|آئ${Y}\s?ڈ${Y}\s?${K}ارڈ|पहचान\s?पत्र|आईडी\s?कार्ड)${WE}`, 'iu');

function findCnics(s, add) {
  for (const m of s.matchAll(CNIC_DASHED)) add(m.index, m.index + m[0].length, 'cnic');
  for (const m of s.matchAll(CNIC_PLAIN)) {
    const end = m.index + m[0].length;
    const near = CNIC_KEYWORD.test(s.slice(Math.max(0, m.index - 48), m.index))
      || CNIC_KEYWORD.test(s.slice(end, end + 32));
    if (near) add(m.index, end, 'cnic');
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  IBAN
// ════════════════════════════════════════════════════════════════════════════

// Pakistani IBAN ki shakl itni khaas hai ke hisaab ke baghair bhi maani jati
// hai (bolte hue ek hindsa ghalat ho jaye to bhi ye bank account hi hai).
// Dash bhi chalti hai: bank ki raseed par "PK36-SCBL-0000-..." likha hota hai.
const IBAN_PK = /(?<![A-Za-z0-9])PK[ -]?\d{2}[ -]?[A-Za-z]{4}(?:[ -]?\d){16}(?!\d)/gi;
const IBAN_HEAD = /(?<![A-Za-z0-9])[A-Za-z]{2}\d{2}/g;
const IBAN_SOLID = /[A-Za-z0-9]{11,30}(?![A-Za-z0-9])/y;
const IBAN_GROUP = / ([A-Za-z0-9]{1,4})(?![A-Za-z0-9])/y;

function mod97(iban) {
  const moved = (iban.slice(4) + iban.slice(0, 4)).toUpperCase();
  let rem = 0;
  for (let i = 0; i < moved.length; i++) {
    const c = moved.charCodeAt(i);
    rem = c <= 57 ? (rem * 10 + c - 48) % 97 : (rem * 100 + c - 55) % 97;
  }
  return rem === 1;
}

function findIbans(s, add) {
  for (const m of s.matchAll(IBAN_PK)) add(m.index, m.index + m[0].length, 'iban');
  for (const m of s.matchAll(IBAN_HEAD)) {
    const bodyAt = m.index + 4;
    IBAN_SOLID.lastIndex = bodyAt;
    const solid = IBAN_SOLID.exec(s);
    if (solid) {
      if (mod97(m[0] + solid[0])) add(m.index, IBAN_SOLID.lastIndex, 'iban');
      continue;
    }
    // Chhapi hui shakl: chaar chaar ke groups. Aakhir mein aam lafz bhi chaar
    // huroof ka ho sakta hai, is liye lambi se chhoti har hadd par hisaab lagta hai.
    const ends = [];
    let compact = m[0];
    let at = bodyAt;
    while (compact.length < 34) {
      IBAN_GROUP.lastIndex = at;
      const group = IBAN_GROUP.exec(s);
      if (!group) break;
      compact += group[1];
      at = IBAN_GROUP.lastIndex;
      if (compact.length >= 15 && compact.length <= 34) ends.push([at, compact]);
      if (group[1].length < 4) break;
    }
    for (let i = ends.length - 1; i >= 0; i--) {
      const [end, text] = ends[i];
      if (/\d/.test(text.slice(4)) && mod97(text)) { add(m.index, end, 'iban'); break; }
    }
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  BANK ACCOUNT
// ════════════════════════════════════════════════════════════════════════════

const ACCOUNT_KEYWORD = new RegExp(String.raw`${WB}(?:(?:account|acct|acc)\.?[ -]*(?:number|nmbr|num|no|#)|a\/c(?:[ .]*(?:number|nmbr|num|no|#))?|ا${K}ا(?:ؤ|و)نٹ\s*نمبر|${K}ھاتہ\s*نمبر|अकाउंट\s*नंबर|खाता\s*(?:नंबर|संख्या))${WE}\.?`, 'giu');
const ACCOUNT_AFTER = /[^\d\n.!?۔,;،؛\uFFFC]{0,24}?(\d(?:[ -]?\d)*)/y;

const DATE_SHAPE = /^(?:(?:19|20)\d\d-\d{1,2}-\d{1,2}|\d{1,2}-\d{1,2}-(?:19|20)\d\d)$/;

function findBankAccounts(s, add) {
  for (const m of s.matchAll(ACCOUNT_KEYWORD)) {
    ACCOUNT_AFTER.lastIndex = m.index + m[0].length;
    const hit = ACCOUNT_AFTER.exec(s);
    if (!hit) continue;
    const run = hit[1];
    const start = ACCOUNT_AFTER.lastIndex - run.length;
    if (/[A-Za-z_+]/.test(s[start - 1] || '')) continue;
    // "account number band ho gaya 12-10-2026 ko" — 8 hindse hain, magar tareekh hai.
    if (DATE_SHAPE.test(run)) continue;
    let length = run.length;
    const digits = length - (run.match(/[ -]/g) || []).length;
    if (digits < 8 || digits > 20) {
      // Account ke foran baad koi aur number ho to sirf pehla tukra.
      length = /^\d+/.exec(run)[0].length;
      if (length < 8 || length > 20) continue;
    }
    add(start, start + length, 'bank_account');
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  SAB KO JORNA
// ════════════════════════════════════════════════════════════════════════════

/** Text mein raaz kahan kahan hain — tarteeb se, ek doosre par charhe baghair. */
function detect(text) {
  const s = prepare(text);
  const spans = [];
  const add = (start, end, kind) => { if (end > start) spans.push({ start, end, kind }); };
  const hasDigit = /\d/.test(s);

  findPrivateKeys(s, add);
  findApiKeys(s, add, hasDigit);
  findPasswords(s, add);
  if (hasDigit) {
    findIbans(s, add);
    findCnics(s, add);
    findBankAccounts(s, add);
    findCards(s, add);
    findCvvs(s, add);
    findOtps(s, add);
  }
  if (spans.length < 2) return spans;

  // Do qaide ek hi jagah lagen to dono ko mila kar ek — adha raaz bahar na rahe.
  spans.sort((a, b) => a.start - b.start || RANK[a.kind] - RANK[b.kind]);
  const merged = [spans[0]];
  for (let i = 1; i < spans.length; i++) {
    const last = merged[merged.length - 1];
    const span = spans[i];
    if (span.start >= last.end) { merged.push(span); continue; }
    last.end = Math.max(last.end, span.end);
    if (RANK[span.kind] < RANK[last.kind]) last.kind = span.kind;
  }
  return merged;
}

/**
 * Raaz ki jagah "[redacted <kind>]".
 * @returns {{ text: string, found: { kind: string }[] }} found = milne ki tarteeb mein.
 */
function redactSecrets(input) {
  if (typeof input !== 'string' || !input) return { text: '', found: [] };
  let text = input;
  let marks = [];   // { at, kind } — is call ke lagaye hue nishan, maujooda text mein un ki jagah
  for (let pass = 0; pass < MAX_PASSES; pass++) {
    const spans = detect(text);
    if (!spans.length) break;
    const moved = [];
    let out = '';
    let cursor = 0;
    let shift = 0;
    let i = 0;
    for (const span of spans) {
      // Pichhle daur ke nishan: span se pehle wale khisak jate hain, andar wale nigal liye jate hain.
      for (; i < marks.length && marks[i].at < span.end; i++) {
        if (marks[i].at < span.start) moved.push({ at: marks[i].at + shift, kind: marks[i].kind });
      }
      out += text.slice(cursor, span.start);
      moved.push({ at: out.length, kind: span.kind });
      const mark = placeholder(span.kind);
      out += mark;
      shift += mark.length - (span.end - span.start);
      cursor = span.end;
    }
    for (; i < marks.length; i++) moved.push({ at: marks[i].at + shift, kind: marks[i].kind });
    text = out + text.slice(cursor);
    marks = moved;
  }
  return { text, found: marks.map((mark) => ({ kind: mark.kind })) };
}

function hasSecret(text) {
  return typeof text === 'string' && text.length > 0 && detect(text).length > 0;
}

module.exports = { redactSecrets, hasSecret, KINDS };
