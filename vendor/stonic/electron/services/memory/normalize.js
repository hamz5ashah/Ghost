// Memory Core ka "kaan" — likha hua lafz aur dhoonda hua lafz yahan ek hi shakl
// mein dhalte hain, taake spelling ya script badalne se yaad gum na ho.
//
// ════════════════════════════════════════════════════════════════════════════
//  YE FILE ELECTRON AUR DATABASE KE BAGHAIR HAI — JAAN BUJH KAR
// ════════════════════════════════════════════════════════════════════════════
//
//  Hamare users ek hi jumle mein Urdu (عربی رسم الخط), Roman Urdu, Hindi
//  (देवनागरी) aur English milate hain, aur Roman Urdu ki koi tay-shuda spelling
//  nahi: "acha / achha / accha", "nahi / nahin / nhi". Full-text index sirf
//  hoobahoo lafz milata hai, is liye store hone wala text AUR search ki query
//  dono isi file se guzarte hain.
//
//  USOOL: yaksaniyat zubaan-daani se zyada ahem hai. Ek hi input hamesha ek hi
//  shakl mein dhale — chahe wo shakl "ghalat Urdu" ho. "meeting" dono taraf
//  "miting" ban jaye to search phir bhi milti hai.
//
//  🔴 YAHAN KE USOOL BADALNA = PURANI YAADEIN DOBARA INDEX KARNA. Database mein
//  fold/skel pehle se likhe hote hain; rule badla to purana index naye query se
//  nahi milega. App update par yaad bachi rahe, is liye rule badlo to migration
//  mein reindex bhi karo.
//
// ── DO TAHEIN ───────────────────────────────────────────────────────────────
//
//    fold  -> usi script mein, saaf aur spelling-variants ek kiye hue.
//             "Mujhay chai pasand hay." -> "mujhe chai pasand hai"
//    skel  -> har lafz ka consonant dhancha, ASCII mein — teeno scripts ka ek.
//             "kitab" / "کتاب" / "किताब" -> "ktb"
//
//  fold tez aur durust hai (precision), skel jaal hai (recall): dhancha "kal",
//  "kala", "call" sab ko "kl" banata hai, is liye ranking mein fold ka wazan
//  zyada rakhna.

const crypto = require('node:crypto');

// ── 1. SAFAI (teeno scripts par) ────────────────────────────────────────────
//
//  Yahan ke huroof code point ke number se likhe hain, hoobahoo nahi: zero-width
//  aur bidi huroof source mein nazar nahi aate, aur editor / git / formatter
//  unhein chup-chaap kha jaye to normaliser badal jata hai aur kisi ko pata
//  nahi chalta.

const hex = (cp) => `\\u${cp.toString(16).padStart(4, '0')}`;
const cls = (ranges) => ranges.map(([from, to = from]) => (from === to ? hex(from) : `${hex(from)}-${hex(to)}`)).join('');
const chr = String.fromCharCode;

const HAS_NON_ASCII = /[^\x00-\x7f]/;

// Ye lafz ki pehchan nahi badalte, sirf match todte hain — is liye hata diye jate hain.
const INVISIBLE = new RegExp(`[${cls([
  [0x00ad],                                   // soft hyphen
  [0x02bb, 0x02bc],                           // harf jaisa apostrophe
  [0x0300, 0x036f],                           // Latin ke combining marks
  [0x0610, 0x061a], [0x061c],                 // Arabic ke oopar wale nishaan
  [0x0640],                                   // tatweel (lafz ko lamba karne wali lakeer)
  [0x064b, 0x065f],                           // aeraab: zabar, zer, pesh, tanween, shadda, sukoon
  [0x0670],                                   // khara alif
  [0x06d6, 0x06dc], [0x06df, 0x06e4], [0x06e7, 0x06e8], [0x06ea, 0x06ed],   // Qurani nishaan
  [0x180e], [0x200b, 0x200f], [0x202a, 0x202e], [0x2060, 0x2064], [0xfeff], // zero-width aur bidi
  [0x20d0, 0x20f0], [0xfe00, 0xfe0f],         // emoji ke saath jure nishaan
])}]`, 'g');

// Arabic keyboard se likhi Urdu mein yeh / kaf / heh ke code alag hote hain
// magar dikhte ek jaise hain — sab ko Urdu wali shakl par lana.
const LETTER_CANON = new Map([
  [0x064a, 0x06cc], [0x0649, 0x06cc], [0x06d0, 0x06cc],                     // Arabic yeh ki shaklein -> ی
  [0x0643, 0x06a9], [0x06aa, 0x06a9],                                       // Arabic kaf -> ک
  [0x0647, 0x06c1], [0x0629, 0x06c1], [0x06c0, 0x06c1],                     // Arabic heh, gol te -> ہ
  [0x06c2, 0x06c1], [0x06c3, 0x06c1], [0x06d5, 0x06c1],
  [0x0623, 0x0627], [0x0625, 0x0627], [0x0671, 0x0627],                     // hamza wala alif -> ا
  [0x06d3, 0x06d2],                                                         // hamza wali bari ye -> ے
  // Chandrabindu aur anusvara log adal-badal likhte hain (माँ / मां).
  [0x0901, 0x0902],
].map(([from, to]) => [chr(from), chr(to)]));
const LETTER_VARIANT = new RegExp(`[${cls([...LETTER_CANON.keys()].map((ch) => [ch.charCodeAt(0)]))}]`, 'g');

// Arabic-Indic, Extended Arabic-Indic (Urdu / Farsi) aur Devanagari ke hindse.
const FOREIGN_DIGIT = new RegExp(`[${cls([[0x0660, 0x0669], [0x06f0, 0x06f9], [0x0966, 0x096f]])}]`, 'g');
const asciiDigit = (ch) => {
  const c = ch.charCodeAt(0);
  return String(c < 0x0900 ? c & 0xf : c - 0x0966);
};

const LATIN_EXTENDED = new RegExp(`[${cls([[0x00c0, 0x024f], [0x1e00, 0x1eff]])}]`, 'g');
const COMBINING = new RegExp(`[${cls([[0x0300, 0x036f]])}]`, 'g');
// Ye huroof NFD se nahi khulte, is liye haath se.
const LATIN_SPECIAL = { 'ß': 'ss', 'æ': 'ae', 'œ': 'oe', 'ø': 'o', 'đ': 'd', 'ł': 'l', 'ı': 'i', 'þ': 'th', 'ð': 'd' };
const plainLatin = (ch) => LATIN_SPECIAL[ch] || ch.normalize('NFD').replace(COMBINING, '');

// "10,000" aur "10000" ek hi raqam hai — payment wali yaadein isi par milti hain.
// Lakh wali ginti bhi ("1,50,000" = 150000): do-do hindson ke group sirf tab
// jurte hain jab aakhri group teen ka ho, warna "12,13" jaisi fehrist ek raqam
// ban jati. Urdu keyboard ka comma (U+060C) bhi yahi kaam karta hai. Group ki
// ginti par hadd (4) is liye ke "11,11,11,…" jaisi lambi fehrist par regex
// har comma se aakhir tak na daure.
const DIGIT_SEP = `,${hex(0x060c)}${hex(0x066c)}`;
const THOUSANDS = new RegExp(`([0-9])[${DIGIT_SEP}](?=(?:[0-9]{2}[${DIGIT_SEP}]){0,4}[0-9]{3}(?![0-9]))`, 'g');

const APOS = `'\`${cls([[0x2018, 0x2019], [0x00b4]])}`;
const ANY_APOSTROPHE = new RegExp(`[${APOS}${cls([[0x02bb, 0x02bc]])}]`);
// Milkiyat ka 's: "Ali's birthday" -> "ali birthday". Sirf apostrophe hatane se
// "alis" banta tha, aur "Ali" dhoondne par wo yaad naam se milti hi nahi thi.
// it's / let's / what's bhi isi se apne asal lafz par aa jate hain.
const POSSESSIVE = new RegExp(`(?<=\\p{L})[${APOS}${cls([[0x02bb, 0x02bc]])}]s(?![\\p{L}\\p{N}\\p{M}])`, 'gu');
// Lafz ke andar ka apostrophe: don't -> dont, ma'loom -> maloom.
const APOSTROPHE = new RegExp(`(?<=\\p{L})[${APOS}](?=\\p{L})`, 'gu');

// Lafz = huroof, hindse aur un se jure nishaan (matra); baqi sab space.
const NOT_WORD = /[^\p{L}\p{N}\p{M}]+/gu;
// NOT_WORD durust hai magar sust (har harf par Unicode table). Hamare 99% text
// mein sirf ye teen scripts aur aam wirama hote hain, is liye pehle ye tez
// raasta; koi anjaan harf (emoji, doosri script) bache tabhi NOT_WORD chalta hai.
// Natija dono raston ka ek hai: test har BMP harf par dekhta hai ke wirama
// kabhi lafz mein nahi bachta aur koi harf ya hindsa kabhi nahi girta.
const ASCII_NOT_WORD = /[^a-z0-9]+/g;
const COMMON_PUNCT = new RegExp(`[${cls([
  [0x0000, 0x002f], [0x003a, 0x0040], [0x005b, 0x0060], [0x007b, 0x00bf],   // ASCII aur Latin-1 ka wirama
  [0x0600, 0x060f], [0x061b, 0x061f], [0x066a, 0x066d], [0x06d4],           // ، ؛ ؟ ٪ ۔
  [0x06dd, 0x06de], [0x06e9], [0x06fd, 0x06fe],
  [0x0964, 0x0965], [0x0970],                                               // । ॥
  [0x2000, 0x206f],                                                         // dashes, quotes, bullets
])}]+`, 'g');
const UNKNOWN_CHAR = new RegExp(`[^ a-z0-9${cls([
  [0x0620, 0x064a], [0x066e, 0x066f], [0x0671, 0x06d3], [0x06d5], [0x06ee, 0x06ef], [0x06fa, 0x06fc], [0x06ff],
  [0x0750, 0x077f],
  [0x0900, 0x0963], [0x0971, 0x097f],
])}]`);

// "WhatsAppپر" ya "5بجے" — script badalte hi lafz toot jata hai. Latin ke saath
// digit jura rehta hai (gpt4, mp3), Urdu / Hindi ke saath nahi.
const ARABIC = cls([[0x0600, 0x06ff], [0x0750, 0x077f]]);
const DEVA = cls([[0x0900, 0x097f]]);
const SCRIPT_EDGE = new RegExp(
  `(?<=[a-z0-9])(?=[${ARABIC}${DEVA}])|(?<=[${ARABIC}])(?=[a-z0-9${DEVA}])|(?<=[${DEVA}])(?=[a-z0-9${ARABIC}])`,
  'g',
);

/** Text -> saaf lafzon ki list (lowercase, script ke andar normalised, folding se pehle). */
function canonWords(text) {
  if (typeof text !== 'string' || text.length === 0) return [];
  let s = text.normalize('NFKC').toLowerCase();
  // INVISIBLE se pehle: wo harf jaise apostrophe (U+02BC) ko kha jata hai.
  if (ANY_APOSTROPHE.test(s)) s = s.replace(POSSESSIVE, '');
  if (!HAS_NON_ASCII.test(s)) {
    return s.replace(THOUSANDS, '$1').replace(APOSTROPHE, '').replace(ASCII_NOT_WORD, ' ').split(' ').filter(Boolean);
  }
  s = s
    .replace(LATIN_EXTENDED, plainLatin)
    .replace(INVISIBLE, '')
    .replace(LETTER_VARIANT, (ch) => LETTER_CANON.get(ch))
    .replace(FOREIGN_DIGIT, asciiDigit)
    .replace(THOUSANDS, '$1')
    .replace(APOSTROPHE, '')
    .replace(COMMON_PUNCT, ' ');
  if (UNKNOWN_CHAR.test(s)) s = s.replace(NOT_WORD, ' ');
  return s.replace(SCRIPT_EDGE, ' ').split(' ').filter(Boolean);
}

const NUMBER = 0;
const LATIN = 1;
const URDU = 2;
const DEVANAGARI = 3;
const OTHER = 4;

/** Lafz kis script ka hai — pehle non-digit harf se (canonWords ke baad lafz ek hi script ka hota hai). */
function kindOf(word) {
  for (let i = 0; i < word.length; i++) {
    const c = word.charCodeAt(i);
    if (c >= 48 && c <= 57) continue;
    if (c >= 97 && c <= 122) return LATIN;
    if ((c >= 0x0600 && c <= 0x06ff) || (c >= 0x0750 && c <= 0x077f)) return URDU;
    if (c >= 0x0900 && c <= 0x097f) return DEVANAGARI;
    return OTHER;
  }
  return NUMBER;
}

// ── 2. ROMAN URDU FOLDING ───────────────────────────────────────────────────
//
//  Tarteeb pakki hai (badli to natija badlega):
//
//    0. (safai mein ho chuka) diacritics, milkiyat ka 's aur lafz ke andar ka
//       apostrophe hata
//    1. lughat mein hoobahoo lafz mila?  -> canonical, bas          ("q" -> kyun)
//    2. ph -> f,  k / q / x se pehle ka c gir jata hai   (ck -> k, acquire -> akuire)
//    3. q -> k,  w -> v,  x -> ks
//    4. aa -> a,  ee -> i,  oo -> u
//    5. ek hi harf ki qataar -> ek harf                (achha / accha -> acha)
//       "ch" bhi ek harf gina jata hai                 (bachche -> bache)
//    6. aakhir ka "ay" / "ey" -> "e", sirf 5+ huroof wale lafz par
//    7. lughat mein dhali hui shakl mili? -> canonical          (teek -> thik)
//
//  Qadam 1 is liye pehle hai ke "q" (kyun) aur "k" (ke) qadam 3 ke baad ek ho
//  jate. Qadam 6 ki hadd is liye ke warna key / pay / day English lafz "ke /
//  pe / de" ban kar stopword ho jate; chhote Roman Urdu lafz (hay, kay, say,
//  thay) lughat se sambhalte hain. "ck -> k" spec mein nahi tha — is se
//  check / چیک aur ticket / ٹکٹ ka dhancha milta hai.
//
//  PAKKA WADA: dhali hui shakl ko dobara dhalo to wohi rehti hai (test 300,000
//  random lafzon par dekhta hai). Naya rule likho to ye wada na toote — warna
//  dobara index karne par purani yaadon ki shakl khisak jati hai.
//
//  Qadam 2–5 regex ki zanjeer ki jagah do seedhe chakkar hain: ye function har
//  lafz par chalta hai aur nau regex is file ka sab se mehnga hissa the.

function phonetic(word) {
  // Qadam 2–3: huroof ki adla-badli.
  let s = '';
  for (let i = 0; i < word.length; i++) {
    const ch = word[i];
    if (ch === 'p' && word[i + 1] === 'h') { s += 'f'; i += 1; }
    else if (ch === 'c') {
      // "c" (ya "cc") jo k ki awaaz se pehle ho, gir jata hai. q aur x bhi k
      // hi bante hain — unhein na dekhein to "acquire" pehli dafa "ackuire"
      // aur doosri dafa "akuire" banta, yani fold ka fold alag aata.
      let j = i + 1;
      while (word[j] === 'c') j += 1;
      const next = word[j];
      // Qataar ek hi dafa parhi jati hai (i aage barh jata hai): "cccc…" par
      // har "c" se dobara ginna lambe kachre par waqt kha jata.
      i = j - 1;
      if (next !== 'k' && next !== 'q' && next !== 'x') s += 'c';
    }
    else if (ch === 'q') s += 'k';
    else if (ch === 'w') s += 'v';
    else if (ch === 'x') s += 'ks';
    else s += ch;
  }
  // Qadam 4–5: qataarein. Hindse kabhi nahi simat-te (100 -> 100).
  let out = '';
  let last = '';
  for (let i = 0; i < s.length; i++) {
    let ch = s[i];
    if (ch >= 'a' && ch <= 'z') {
      let end = i + 1;
      while (end < s.length && s[end] === ch) end += 1;
      // "ch" bhi ek harf hai: dohra "chch" ek hi rehta hai (bachche -> bache,
      // achchha -> acha) — bilkul "cc" aur "hh" ki tarah.
      if (ch === 'c' && s[end] === 'h' && last === 'h' && out.endsWith('ch')) { i = end; continue; }
      if (end - i > 1) {
        if (ch === 'e') ch = 'i';
        else if (ch === 'o') ch = 'u';
        i = end - 1;
      }
      // "ee" se bana "i" pehle se pare "i" ke saath phir ek qataar hai.
      if (ch === last) continue;
    }
    out += ch;
    last = ch;
  }
  // Qadam 6. Jar pehle hi "e" par khatam ho to doosra "e" nahi lagta, warna
  // "…eay" -> "…ee" banta aur agli dafa "…i" ho jata.
  if (out.length >= 5 && (out.endsWith('ay') || out.endsWith('ey'))) {
    const base = out.slice(0, -2);
    out = base.endsWith('e') ? base : `${base}e`;
  }
  return out;
}

// Aam function-words ki mukhtalif spellings -> ek shakl. Sirf wo lafz jo jumle
// ka "gond" hain; cheezon aur logon ke naam yahan kabhi nahi aate. Jo variants
// qadam 2–6 se khud ek ho jate hain (jaana, nahee, mujhay, kaisay) wo yahan
// likhne ki zaroorat nahi.
//
// Jaan bujh kar NAHI rakhe: pr (PR), kb / tm (units), kiya -> kya (kiya = kiya).
const ROMAN_VARIANTS = {
  hai: 'hay hy he hae',
  hain: 'hein hen hyn',
  hun: 'hoon hu houn',
  tha: 'thi the thay thy',
  nahi: 'nahin nai nhi nahe nhe nahein nhin',
  kya: 'kia',
  kyun: 'kyu kiun q kyon kiyun kion',
  kyunki: 'kyunke kyuki kyuke kiunke kiunki kyonki kyonke',
  kaise: 'kese kaisy kesy',
  kaisa: 'kesa',
  kaisi: 'kesi',
  mein: 'main me mai mei',
  maine: 'mene meine mne',
  mujhe: 'muje mujhy mjhe mjy',
  // SMS wali likhai mein aakhri "e" ki jagah "y": mery ghar, apny kaam.
  mere: 'mery',
  apne: 'apny',
  uske: 'usky',
  iske: 'isky',
  unke: 'unky',
  tujhe: 'tuje tujhy',
  ke: 'kay k ky',
  se: 'say sy',
  aur: 'or aor',
  to: 'tou toh',
  wo: 'woh voh',
  ye: 'yeh yh',
  bhi: 'bhy',
  hum: 'ham',
  theek: 'thik teek thek thk',
  bohat: 'bahut bhot bht boht bohot bahot buhat bohut',
  acha: 'achcha achchha',
  raha: 'rha',
  rahi: 'rhi',
  rahe: 'rhe rahy rhy',
  kar: 'kr',
  karna: 'krna',
  karne: 'krne krny',
  karni: 'krni',
  karo: 'kro',
  karta: 'krta',
  karti: 'krti',
  karte: 'krte krty',
  karke: 'krke',
  karen: 'kren karein karain',
  liye: 'liay lye liey',
  liya: 'lia lya',
  diya: 'dia dya',
  diye: 'diay',
  kiye: 'kiay kie',
  gaya: 'gya geya',
  gayi: 'gai gyi',
  gaye: 'gae gye gaey',
  hua: 'hwa huwa',
  hue: 'huay huey',
  chahiye: 'chahye chaiye chahie chaheye chahiay',
  sakta: 'skta',
  sakti: 'skti',
  sakte: 'skte',
  phir: 'fir phr pher',
  lekin: 'lkn laikin lakin',
  agar: 'agr',
  sath: 'sth',
  pehle: 'pahle phle pehly',
  zyada: 'ziada ziyada zyda zayada',
  thora: 'thoda',
  thori: 'thodi',
  kuch: 'kch kuj',
  abhi: 'abi',
  sirf: 'srf',
  bilkul: 'bikul blkl bilkol',
  zaroor: 'zrur zror',
  shayad: 'shyd shaid shayd',
  jaldi: 'jldi',
  ghalat: 'galat',
  matlab: 'mtlb matlb',
  pata: 'pta',
  yahan: 'yaha yhan',
  wahan: 'waha whan',
  kaun: 'kon koun',
  kitna: 'ktna',
  kitni: 'ktni',
  kitne: 'ktne',
  shukriya: 'shukria shukrya',
  han: 'hn',
  please: 'plz pls',
  thanks: 'thnx thx tnx thnks',
  ok: 'okay okey oky',
  message: 'msg',
};

// Do chaabiyan har variant ki: jaisa likha gaya, aur jaisa dhal kar banta hai.
// Likhi hui chaabiyan pehle bharti hain taake "q" ki dhali shakl ("k") "ke"
// wale "k" ko na dabaye. Takrao par pehla jeet-ta hai — hamesha ek hi natija.
const VARIANT_TO_CANON = new Map();
{
  const groups = Object.entries(ROMAN_VARIANTS).map(([canon, list]) => [phonetic(canon), list.split(' ')]);
  for (const [canon, list] of groups) {
    for (const v of list) if (v !== canon && !VARIANT_TO_CANON.has(v)) VARIANT_TO_CANON.set(v, canon);
  }
  for (const [canon, list] of groups) {
    for (const v of list) {
      const key = phonetic(v);
      if (key !== canon && !VARIANT_TO_CANON.has(key)) VARIANT_TO_CANON.set(key, canon);
    }
  }
  // Canonical shakl khud kabhi kisi aur ki variant nahi — warna fold ka fold alag aata.
  for (const [canon] of groups) VARIANT_TO_CANON.delete(canon);
}

function foldLatin(word) {
  const direct = VARIANT_TO_CANON.get(word);
  if (direct !== undefined) return direct;
  const w = phonetic(word);
  const mapped = VARIANT_TO_CANON.get(w);
  return mapped !== undefined ? mapped : w;
}

const NUKTA = chr(0x093c);

// Devanagari fold mein nukta gir jata hai: log "ज़रूरत" ko "जरूरत" bhi likhte
// hain aur dono ek hi lafz hain. Dhanche (skel) mein nukta parha jata hai.
function foldWord(word, kind) {
  if (kind === LATIN) return foldLatin(word);
  if (kind === DEVANAGARI) return word.includes(NUKTA) ? word.split(NUKTA).join('') : word;
  return word;
}

// ── 3. DHANCHA (consonant skeleton) ─────────────────────────────────────────
//
//  Teeno scripts ka ek hi dhancha tab banta hai jab teeno mein wohi cheezein
//  girein: vowels, aur wo huroof jo kabhi vowel kabhi consonant hote hain
//  (y / w / v, ی / و, य / व). Aspiration ka "h" bhi girta hai (bh, th, kh …)
//  kyunki Urdu mein wo ھ hai aur Hindi mein alag harf hi nahi.
//
//  Spec se aage ke faisle (har ek ki wajah ek asal lafz hai):
//    - dhanche ka aakhri "h" girta hai:  paisa / پیسہ,  jagah / جگہ,  subah / صبح
//    - akela "c" -> k (e / i / y se pehle s):  doctor / ڈاکٹر,  office / آفس
//    - "sch" -> sk,  "tion" -> sn:  school / اسکول,  station / اسٹیشن
//    - "h" sirf b c d g j k r s t ke baad aspiration hai; m / n / l / z ke baad
//      asli h hai:  tumhara / تمہارا,  mazhab / مذہب
//    - پھ -> f, taake phool / پھول / फूल teeno milein (Latin ph -> f, फ -> f)
//    - anusvara aur chandrabindu dono -> n (lab wale harf se pehle m)
//    - ज्ञ -> g (gyan),  dohra harf (ब्ब) -> ek

const LATIN_DROPPED = new Set(['a', 'e', 'i', 'o', 'u', 'y', 'v', 'w']);
const LATIN_ASPIRATED = new Set(['b', 'c', 'd', 'g', 'j', 'k', 'r', 's', 't']);

/** Dhali hui (folded) Latin shakl se dhancha. */
function skelLatin(w) {
  let out = '';
  for (let i = 0; i < w.length; i++) {
    const ch = w[i];
    let add;
    if (ch === 's' && w[i + 1] === 'c' && w[i + 2] === 'h') { add = 'sk'; i += 2; }
    else if (ch === 't' && w[i + 1] === 'i' && w[i + 2] === 'o' && w[i + 3] === 'n') { add = 'sn'; i += 3; }
    else if (ch === 'c') {
      const next = w[i + 1];
      if (next === 'h') { add = 'c'; i += 1; }
      else add = next === 'e' || next === 'i' || next === 'y' ? 's' : 'k';
    } else if (LATIN_DROPPED.has(ch)) continue;
    else add = ch;
    out += add;
    if (w[i + 1] === 'h' && LATIN_ASPIRATED.has(add[add.length - 1])) i += 1;
  }
  return out.endsWith('h') ? out.slice(0, -1) : out;
}

const URDU_SKEL = {
  'ب': 'b', 'پ': 'p', 'ت': 't', 'ٹ': 't', 'ث': 's', 'ج': 'j', 'چ': 'c', 'ح': 'h', 'خ': 'k',
  'د': 'd', 'ڈ': 'd', 'ذ': 'z', 'ر': 'r', 'ڑ': 'r', 'ز': 'z', 'ژ': 'z', 'س': 's', 'ش': 's',
  'ص': 's', 'ض': 'z', 'ط': 't', 'ظ': 'z', 'غ': 'g', 'ف': 'f', 'ق': 'k', 'ک': 'k', 'گ': 'g',
  'ل': 'l', 'م': 'm', 'ن': 'n', 'ں': 'n', 'ہ': 'h',
};
const DO_CHASHMI_HE = 'ھ';
const PE = 'پ';

function skelUrdu(word) {
  let out = '';
  for (let i = 0; i < word.length; i++) {
    const ch = word[i];
    if (ch === DO_CHASHMI_HE) {
      if (i > 0 && word[i - 1] === PE) out = `${out.slice(0, -1)}f`;
      continue;
    }
    const mapped = URDU_SKEL[ch];
    if (mapped) out += mapped;
  }
  return out.endsWith('h') ? out.slice(0, -1) : out;
}

const DEVA_SKEL = {
  'क': 'k', 'ख': 'k', 'ग': 'g', 'घ': 'g', 'ङ': 'n', 'च': 'c', 'छ': 'c', 'ज': 'j', 'झ': 'j', 'ञ': 'n',
  'ट': 't', 'ठ': 't', 'ड': 'd', 'ढ': 'd', 'ण': 'n', 'त': 't', 'थ': 't', 'द': 'd', 'ध': 'd', 'न': 'n',
  'ऩ': 'n', 'प': 'p', 'फ': 'f', 'ब': 'b', 'भ': 'b', 'म': 'm', 'र': 'r', 'ऱ': 'r', 'ल': 'l', 'ळ': 'l',
  'ऴ': 'l', 'श': 's', 'ष': 's', 'स': 's', 'ह': 'h', 'ऋ': 'r', 'ृ': 'r',
};
// Nukta sirf in huroof ki awaaz badalta hai; baqi par wohi rehti hai.
const DEVA_NUKTA_SKEL = { 'ज': 'z', 'झ': 'z', 'ड': 'r', 'ढ': 'r' };
const VIRAMA = chr(0x094d);
const ANUSVARA = chr(0x0902);
const DEVA_LABIAL = new Set(['प', 'फ', 'ब', 'भ', 'म']);

function skelDevanagari(word) {
  let out = '';
  let joined = false;   // pichhla harf virama se jura tha — dohra consonant pakarne ke liye
  for (let i = 0; i < word.length; i++) {
    const ch = word[i];
    if (ch === VIRAMA) { joined = true; continue; }
    let mapped;
    if (ch === ANUSVARA) {
      mapped = DEVA_LABIAL.has(word[i + 1]) ? 'm' : 'n';
    } else if (ch === 'ज' && word[i + 1] === VIRAMA && word[i + 2] === 'ञ') {
      mapped = 'g';
      i += 2;
    } else if (word[i + 1] === NUKTA) {
      mapped = DEVA_NUKTA_SKEL[ch] || DEVA_SKEL[ch];
      i += 1;
    } else {
      mapped = DEVA_SKEL[ch];
    }
    if (mapped && !(joined && out.endsWith(mapped))) out += mapped;
    joined = false;
  }
  return out.endsWith('h') ? out.slice(0, -1) : out;
}

/** Ek lafz ka dhancha. 2 se chhota = '' (ek harf ka dhancha har cheez se mil jata). */
function skelWord(word, kind, folded) {
  if (kind === NUMBER) return word;
  let s = '';
  if (kind === LATIN) s = skelLatin(folded);
  else if (kind === URDU) s = skelUrdu(word);
  else if (kind === DEVANAGARI) s = skelDevanagari(word);
  return s.length >= 2 ? s : '';
}

// ── 4. STOPWORDS ────────────────────────────────────────────────────────────
//
//  Sirf jumle ka gond: postpositions, "hai / tha", zameerein, sawaliya lafz,
//  aur "karna / raha / gaya" jaise madadgar fail. Dhali hui (folded) shakl par
//  jaanche jate hain, is liye yahan aam spelling likhi hai aur neeche fold hoti.
//
//  FAISLE:
//    - Din ke lafz (kal, aaj, parson) stopword NAHI — "aaj ka kaam" mein wohi
//      asal baat hain, aur ranking aam lafzon ko khud halka kar deti hai.
//    - Jo lafz fold ho kar kisi naam ya cheez se takrate hain wo yahan nahi:
//      but (Butt), aap (app), mat (Matt), pas (pass), sab (saab), har (haar),
//      baad (bad), diya (Diya), abhi (Abhi), jahan (Jahan), wali (Wali), may, will,
//      an (Ann), am (aam), been (bin), shall (shaal), do (do = 2), aur Urdu / Hindi
//      ka "بس / बस" (bus).

const STOPWORD_SOURCE = [
  // English
  'a the and or if of to in on at by for from with as is are was were be being',
  'have has had does did not no so than that this these those there here it its',
  'i me my we us our you your he him his she her they them their',
  'what which who whom when where why how can could would should',
  'about into over under again very just also too only',
  'im ive dont doesnt didnt cant wont isnt arent wasnt please ok yes hi hello hey',
  // Roman Urdu
  'hai hain hun ho hoga hogi honge hota hoti hote hona hua hui hue tha',
  'ka ki ke ko se ne par pe mein tak ya aur lekin magar agar to bhi hi na nahi',
  'kya kyun kyunki kaise kaisa kaisi kab kahan kaun kis kisi koi kuch kitna kitni kitne',
  'jo jab jaise jis ye wo is us in un yahan wahan aisa aise aisi itna itni itne',
  'mujhe mujh mujhko tujhe tum tu hum mera meri mere tera teri tere hamara hamari hamare',
  // "ne" ke saath jure zameer (Urdu mein ye do lafz hain: میں نے) aur mustaqbil
  // ka alag likha "ga / gi / ge" (karun ga) — Urdu / Hindi ki list mein pehle se hain.
  'maine tumne usne isne humne unhone inhone kisne jisne ga gi ge',
  'tumhara tumhari tumhare apka apki apke uska uski uske iska iski iske unka unki unke inka inki inke',
  'apna apni apne usko isko unko inko usay isay unhe unhen inhe inhen',
  'kar karna karne karni karo karta karti karte karke karen karun karunga karungi karenge kiya kiye',
  'raha rahi rahe gaya gayi gaye liya liye sakta sakti sakte chahiye wala wale',
  'phir bohat zyada sirf bas ab acha yaar ji han',
  // Urdu
  'کا کی کے کو سے نے پر پہ میں تک یا اور لیکن مگر اگر تو بھی ہی نہ نہیں کہ',
  'ہے ہیں ہوں ہو ہوگا ہوگی ہوتا ہوتی ہوتے ہونا ہوا ہوئی ہوئے تھا تھی تھے تھیں گا گی گے',
  'کیا کیوں کیونکہ کیسے کیسا کیسی کب کہاں کون کس کسی کوئی کچھ کتنا کتنی کتنے',
  'جو جب جیسے جس یہ وہ اس ان یہاں وہاں ایسا ایسے ایسی اتنا اتنی اتنے',
  'مجھے مجھ تجھے تم تو آپ ہم میرا میری میرے تیرا تیری تیرے ہمارا ہماری ہمارے',
  'تمہارا تمہاری تمہارے اپنا اپنی اپنے اسے انہیں انہوں',
  'کر کرنا کرنے کرنی کرو کرتا کرتی کرتے کرکے کریں کروں کیے',
  'رہا رہی رہے گیا گئی گئے لیا لیے لئے سکتا سکتی سکتے چاہیے چاہئے والا والی والے',
  'پھر بہت زیادہ صرف اب اچھا یار جی ہاں',
  // Hindi
  'का की के को से ने पर पे में तक या और लेकिन मगर अगर तो भी ही न नहीं कि',
  // "नही" ghalat spelling hai magar rozmarra ki — "नहीं" jitni hi aam.
  'है हैं हूँ हो होगा होगी होता होती होते होना हुआ हुई हुए था थी थे थीं गा गी गे मैं नही',
  'क्या क्यों क्योंकि कैसे कैसा कैसी कब कहाँ कौन किस किसी कोई कुछ कितना कितनी कितने',
  'जो जब जैसे जिस यह ये वह वो इस उस इन उन यहाँ वहाँ ऐसा ऐसे ऐसी इतना इतनी इतने',
  'मुझे मुझ तुझे तुम तू आप हम मेरा मेरी मेरे तेरा तेरी तेरे हमारा हमारी हमारे',
  'तुम्हारा तुम्हारी तुम्हारे अपना अपनी अपने उसे उन्हें इन्हें',
  'कर करना करने करनी करो करता करती करते करके करें करूँ किया किए किये',
  'रहा रही रहे गया गई गयी गए गये लिया लिए लिये सकता सकती सकते चाहिए वाला वाली वाले',
  'फिर बहुत ज़्यादा सिर्फ़ अब अच्छा यार जी हाँ',
];

const STOPWORDS = new Set();
for (const line of STOPWORD_SOURCE) {
  for (const word of canonWords(line)) STOPWORDS.add(foldWord(word, kindOf(word)));
}

// ── 5. BAHAR KE FUNCTIONS ───────────────────────────────────────────────────

const MIXED_SHARE = 0.2;

const isArabicLetter = (c) => (c >= 0x0620 && c <= 0x064a) || (c >= 0x066e && c <= 0x06d5 && c !== 0x0670 && c !== 0x06d4)
  || (c >= 0x06ee && c <= 0x06ef) || (c >= 0x06fa && c <= 0x06ff) || (c >= 0x0750 && c <= 0x077f)
  || (c >= 0xfb50 && c <= 0xfdff) || (c >= 0xfe70 && c <= 0xfefc);
const isDevanagariLetter = (c) => (c >= 0x0904 && c <= 0x0939) || c === 0x093d || c === 0x0950
  || (c >= 0x0958 && c <= 0x0961) || (c >= 0x0972 && c <= 0x097f);
const isLatinLetter = (c) => (c >= 97 && c <= 122) || (c >= 65 && c <= 90)
  || (c >= 0x00c0 && c <= 0x024f && c !== 0x00d7 && c !== 0x00f7) || (c >= 0x1e00 && c <= 0x1eff);
const ANY_LETTER = /\p{L}/u;

/**
 * Text ki script, huroof gin kar. Do scripts ka hissa 20% ya zyada ho to
 * "mixed" — lambe Urdu jumle mein ek "PDF" usay mixed nahi banata.
 */
function detectScript(text) {
  if (typeof text !== 'string' || text.length === 0) return 'other';
  let urdu = 0;
  let devanagari = 0;
  let latin = 0;
  let other = 0;
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i);
    if (c < 0x80) {
      if (isLatinLetter(c)) latin += 1;
    } else if (isArabicLetter(c)) urdu += 1;
    else if (isDevanagariLetter(c)) devanagari += 1;
    else if (isLatinLetter(c)) latin += 1;
    else if (c >= 0xd800 && c <= 0xdbff) {
      // Surrogate pair (emoji ya nayab scripts) — poora code point ek saath.
      if (ANY_LETTER.test(String.fromCodePoint(text.codePointAt(i)))) other += 1;
      i += 1;
    } else if ((c < 0x0600 || c > 0x06ff) && (c < 0x0900 || c > 0x097f) && ANY_LETTER.test(text[i])) other += 1;
  }
  const total = urdu + devanagari + latin + other;
  if (total === 0) return 'other';
  const shares = [['urdu', urdu], ['devanagari', devanagari], ['latin', latin], ['other', other]];
  if (shares.filter(([, n]) => n / total >= MIXED_SHARE).length >= 2) return 'mixed';
  shares.sort((a, b) => b[1] - a[1]);
  return shares[0][0];
}

function foldText(text) {
  const words = canonWords(text);
  for (let i = 0; i < words.length; i++) words[i] = foldWord(words[i], kindOf(words[i]));
  return words.join(' ');
}

function skeleton(text) {
  const out = [];
  for (const word of canonWords(text)) {
    const kind = kindOf(word);
    const s = skelWord(word, kind, kind === LATIN ? foldLatin(word) : word);
    if (s) out.push(s);
  }
  return out.join(' ');
}

/** Store aur search dono isi se guzarte hain — teeno cheezein ek hi chakkar mein. */
function normalizeForSearch(text) {
  const folds = [];
  const skels = [];
  for (const word of canonWords(text)) {
    const kind = kindOf(word);
    const fold = foldWord(word, kind);
    folds.push(fold);
    const s = skelWord(word, kind, fold);
    if (s) skels.push(s);
  }
  return { fold: folds.join(' '), skel: skels.join(' '), script: detectScript(text) };
}

/**
 * Query ke kaam ke lafz. `raw` = user ka lafz saaf shakl mein (folding se
 * pehle), `skel` khali ho sakta hai (chhota lafz — sirf fold se dhoondo).
 */
function queryTokens(text) {
  const out = [];
  const seen = new Set();
  for (const word of canonWords(text)) {
    const kind = kindOf(word);
    const fold = foldWord(word, kind);
    // Likha hua lafz bhi naapa jata hai: akela "x" dhal kar "ks" ban jata hai.
    if (kind !== NUMBER && (word.length < 2 || fold.length < 2)) continue;
    if (STOPWORDS.has(fold) || seen.has(fold)) continue;
    seen.add(fold);
    out.push({ raw: word, fold, skel: skelWord(word, kind, fold) });
  }
  return out;
}

/** Dohri yaad pakarne ke liye: spelling, spacing aur punctuation ka farq hash nahi badalta. */
function contentHash(text) {
  return crypto.createHash('sha256').update(foldText(text).replace(/ /g, ''), 'utf8').digest('hex');
}

module.exports = {
  detectScript, foldText, skeleton, normalizeForSearch, queryTokens, contentHash, STOPWORDS,
};
