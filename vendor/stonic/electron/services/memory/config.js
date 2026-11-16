// Memory Core ke saare numbers — ek hi jagah.
//
// ════════════════════════════════════════════════════════════════════════════
//  YE FILE KYUN HAI
// ════════════════════════════════════════════════════════════════════════════
//
//  Yaadash ke design ki SHAKL research se aayi hai, magar us ke NUMBERS (kitne
//  tokens, kaun sa wazan, kitne din) shuruati andaze hain jo golden set par
//  tune honge. Agar ye numbers code mein bikhre hon to tuning ka matlab das
//  files kholna hota. Yahan badlo, sab jagah badal jata hai.
//
//  Koi number yahan se bahar "magic number" ban kar nahi likha jata.

const MIN = 60 * 1000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

// Schema ka version `schema.js` mein hai (MIGRATIONS.length) — yahan nahi,
// taake dono kabhi alag na ho saken.

const FILES = {
  /** userData ke andar ka folder. App update is folder ko nahi chherta. */
  dirName: 'memory',
  dbName: 'memory.db',
  backupDirName: 'backups',
};

// ── Haddein (likhte waqt) ───────────────────────────────────────────────────
const LIMITS = {
  /** Ek turn kitna lamba ho sakta hai. Us se lamba kat-ta hai aur flag lagta hai. */
  turnMaxChars: 8000,
  /**
   * Ek yaad kitni lambi ho sakti hai. Ye likhne ke andaaz ki hadd NAHI: model
   * jitna zaroori samjhe likhe ("ek yaad = ek mauzu"). Ye sirf hifazati fuse
   * hai — model kabhi poori baat-cheet ko ek "yaad" bana de to wo hamesha ke
   * liye har Recall Agent sawal ke saath jati, aur itni lambi yaad ka ek hissa
   * na badla ja sakta hai na bhulaya.
   *
   * Hadd par likhna RAD hota hai, saaf wajah ke saath ("do yaadon mein likho").
   * Chup-chaap kat-ta nahi: pehle 400 par kat jata tha aur kisi ko pata nahi
   * chalta tha.
   *
   * Yehi number ek aur jagah hai — badlo to wahan bhi:
   *   src/components/modals/memory/text.ts      MEMORY_TEXT_MAX
   */
  memoryMaxChars: 2000,
  /**
   * `remember` tool se ek baithak (voice shuru se band tak) mein zyada se zyada
   * kitni yaadein. Loop ka fuse hai — lambi baithak ke liye 12 tang tha.
   */
  rememberPerSession: 30,
  /** `follow_up` tool se ek session mein zyada se zyada kitne waade. */
  followUpsPerSession: 8,
  /** Digest ek session se zyada se zyada kitni yaadein nikal sakta hai. */
  digestMemoriesPerSession: 8,
  digestIntentionsPerSession: 5,
  /**
   * Pin ki hadd. Ye talash ke rehm par nahi hotin — har session mein jati
   * hain — is liye in ki ginti aur lambai dono par sakht hadd hai. Hadd par
   * likhna RAD hota hai (saaf error), chup-chaap kat-ta nahi.
   */
  pinnedMax: 20,
  pinnedMaxChars: 1400,
  triggerWordsPerMemory: 8,
};

// ── Context ke budgets (huroof mein; ~4 huroof = 1 token) ───────────────────
//
// Voice ka system prompt connect par ek dafa banta hai aur phir HAR turn par
// dobara bill hota hai, is liye wahan sab se chhota budget hai.
//
// PIN ki hui baatein in budgets se BAHAR hain: wo har dimagh ko POORI jati
// hain (un ki apni hadd `LIMITS.pinnedMaxChars` hai). Yehi wo waada hai jo
// code saabit kar sakta hai — "pin ki hui baat har session mein saamne hogi".
//
// `hermes` dimagh ka naam purana hai (pehle Hermes agent isi se parhta tha).
// Ab ye sirf andar ka "poori yaadash, bina character" wala nazariya hai —
// nichor (digest) aur "bhool jao" ki talash isi se chalti hain. Naam is liye
// nahi badla ke `hermes` channel purani rows mein mehfooz hai (CHANNELS).
const BUDGETS = {
  voice: { profile: 700, intentions: 300, lastTime: 400 },
  hermes: { profile: 2000, intentions: 800, lastTime: 900 },
  awareness: { profile: 400, intentions: 300, lastTime: 200 },
  /** `recall` ka jawab (tool result). */
  recallChars: { voice: 900, hermes: 2400, awareness: 500 },
};

// ── Talash ──────────────────────────────────────────────────────────────────
const SEARCH = {
  /** Har channel se kitne umeedwar. */
  perChannel: 30,
  rrfK: 60,
  /**
   * Channel ke wazan. Yaadein (nichor) kachi baat-cheet se upar, aur lafzi
   * match skeleton (sirf consonants) se upar — skeleton kamzor ishara hai jo
   * doosri script ka pul banta hai, is liye us ka wazan kam hai.
   */
  weights: {
    memUni: 1.0, memTri: 0.8, memSkel: 0.45, memEntity: 1.1, memTrigger: 2.0,
    turnUni: 0.8, turnTri: 0.6, turnSkel: 0.35,
    episodeUni: 0.7, episodeSkel: 0.3,
  },
  /** Final = R x (1 + a*A + s*S) x C  — relevance hamesha haavi rehti hai. */
  activationWeight: 0.25,
  salienceWeight: 0.25,
  /** ACT-R ka decay. Sirf tarteeb ke liye; is se kuch mit-ta nahi. */
  activationDecay: 0.5,
  salience: { explicit: 0.30, future: 0.25, emotion: 0.20, surprise: 0.15, useful: 0.10 },
  /**
   * FOUND / WEAK / NONE: sawal ke kaam ke lafzon mein se kitne top natije mein
   * mile. RRF ka score mutlaq nahi hota (sirf tarteeb batata hai), is liye
   * "mila ya nahi" ka faisla coverage par hai.
   */
  foundCoverage: 0.6,
  weakCoverage: 0.34,
  /**
   * Coverage WAZNI hai: har lafz ka wazan us ki kam-yaabi (idf) hai. Jo lafz
   * poore record mein kahin nahi (user ne doosra lafz bola) us ka wazan sab se
   * kam-yaab lafz ke wazan ka itna hissa — us ka na milna kam saza deta hai.
   */
  absentWeight: 0.3,
  /** Itne hisse (ya 3) se kam jagah aane wala lafz "kam-yaab" hai: naam, khaas cheez. */
  rareShare: 0.02,
  /** Is se kam cheezein hon to idf nahi lagta (chhote record mein har lafz "kam-yaab" hota hai). */
  idfMinDocs: 40,
  /** User ka apna jumla assistant ke jawab se zara upar. */
  userTurnBoost: 1.15,
  /** Tarteeb = coverage x (floor + (1-floor) x R). floor jitna bara, channels ki tarteeb ka asar utna kam. */
  coverageFloor: 0.6,
  /** Bina pooche bolne (Awareness) ke liye is se kam par chup. */
  proactiveCoverage: 0.75,
  defaultK: 6,
  /** Turn ke natije ke saath aage peeche kitne turns dikhane hain. */
  turnWindow: 1,
};

// ── Recall Agent (voice ka "yaad karo") ─────────────────────────────────────
//
// Voice ka `recall` ab lafzi talash nahi: ek tez model (Luna, sochna band) ko
// SAARI yaadash di jati hai aur wo matlab samajh kar jawab deta hai
// (recall-agent.js). Ye us ke numbers hain.
//
// Haddein sirf hifazati hain — aam user in se bohat neeche rehta hai (Oct 2026
// mein ek asli file: 31 yaadein = 3,600 huroof). Hadd par jo cheez bahar reh
// jaye wo chup-chaap gum nahi hoti: us ke liye lafzi talash ke natije saath
// jate hain aur ginti `stats.dropped` mein hoti hai.
const AGENT = {
  /** Voice ki chalti baat-cheet ke itne aakhri messages sawal ke saath jate hain. */
  recentMessages: 15,
  recentMessageMaxChars: 600,
  questionMaxChars: 1200,
  keywordsMaxChars: 200,
  /** MEMORY ke hisson ki haddein (huroof; ~4 huroof = 1 token). */
  factsMaxChars: 120000,
  episodesMaxChars: 24000,
  /** Lafz-ba-lafz baat-cheet: taaza se purani, poori baat-cheet ke hisaab se. */
  turnsMaxChars: 40000,
  turnMaxChars: 500,
  followUpsMax: 40,
  /** Jo MEMORY mein na samaya us ke liye lafzi talash ke itne natije saath. */
  olderHits: 6,
  /** Jawab voice ke context mein rehta aur har turn par dobara bill hota hai — is liye chhota. */
  answerMaxChars: 1100,
  maxOutputTokens: 500,
  /** Model ka intezar. Is ke baad lafzi talash ka jawab jata hai — awaaz latakti nahi. */
  timeoutMs: 8000,
  /** Ek baat-cheet mein itni dafa; us ke baad lafzi talash (muft, PC par). */
  asksPerSession: 40,
  /** Model ka jawab itni der mein na aaye to us ka intezar khatam (pack bhula diya jata hai). */
  pendingTtlMs: 5 * MIN,
};

// ── Digest (baat ke baad ka nichor) aur "neend" ─────────────────────────────
const DIGEST = {
  /** Session band hone ke baad itni khamoshi ka intezar — adhoori baat ka nichor na ho. */
  quietMs: 15 * MIN,
  /** Is se kam user turns wali baat-cheet ka nichor nahi (hello/hi kachra hai). */
  minUserTurns: 2,
  /** Ek LLM call mein zyada se zyada kitna text. Us se lambi baat tukron mein. */
  maxCharsPerCall: 14000,
  maxAttempts: 3,
  promptVersion: 'd2',
  /** Andaze wali yaad kitne alag sessions ke saboot par pakki hoti hai. */
  promoteAfterSessions: 2,
  /** Itne din tak doosra saboot na mile to andaza rank mein neeche (mit-ta nahi). */
  candidateStaleDays: 90,
  /** Fi user mahana kharche ki hadd (USD). Hadd par digest qataar mein intezar karta hai. */
  monthlyCapUsd: 1.0,
  dailyCallCap: 60,
  /** Ek call ka andaza (USD) jab asal usage na mile. */
  fallbackCostUsd: 0.002,
};

// ── Waade (follow-ups) ──────────────────────────────────────────────────────
const INTENTIONS = {
  /** Waqt wala waada apne waqt ke itne baad khud khatam; bina waqt wala banne ke itne baad. */
  expiryAfterDueMs: 14 * DAY,
  expiryUndatedMs: 30 * DAY,
  /**
   * Do waade "ek hi baat" kab hain — kaam ke lafzon ka mel (0..1, chhote jumle
   * ke hisaab se). Do jagah lagta hai: voice ka likha waada nichor dobara na
   * banaye, aur "wo ho gaya" sahi waade ko band kare.
   */
  sameOverlap: 0.6,
  /** Wohi jumla ya wohi din bhi ho to itna mel kaafi hai (lafz badal jate hain, din nahi). */
  sameOverlapWithCue: 0.34,
};

const JOBS = {
  leaseMs: 5 * MIN,
  retryBaseMs: 2 * MIN,
  tickMs: 60 * 1000,
};

const BACKUP = {
  /** App khule ho to itni der baad pehla backup dekhna (boot par bojh nahi). */
  firstCheckMs: 10 * MIN,
  minIntervalMs: 20 * HOUR,
  keepDaily: 7,
  keepWeekly: 4,
  keepMonthly: 12,
};

// ── Cheezon ki qismein ──────────────────────────────────────────────────────
const MEMORY_TYPES = [
  'identity', 'preference', 'relationship', 'project', 'routine',
  'event', 'opinion', 'instruction', 'other',
];
/**
 * In qismon mein ek waqt mein ek hi baat sach hoti hai (sheher, job, pasand).
 * Nayi baat aaye to purani MANSOOKH hoti hai. Baqi qismein jama hoti hain —
 * doosra dost aane se pehla dost khatam nahi hota.
 */
const SINGLE_VALUED_TYPES = new Set(['identity', 'preference', 'routine', 'opinion', 'instruction']);
const MEMORY_STATUSES = ['candidate', 'active', 'superseded', 'retracted'];
const ORIGINS = ['stated', 'inferred', 'assistant_tool', 'manual', 'imported', 'agent_action'];
// `hermes` = purane versions ki agent baat-cheet; un ki rows DB mein mehfooz hain.
const CHANNELS = ['voice', 'hermes', 'awareness', 'remote'];
const SPEAKERS = ['user', 'assistant', 'awareness'];
const MINDS = ['voice', 'hermes', 'awareness'];

/** `turns.flags` ke bits. */
const TURN_FLAGS = {
  /** Ye text model ko YAAD ke taur par dikhaya gaya tha — is se dobara yaad nahi banti. */
  recalled: 1,
  /** Is session mein web page / document / email parha gaya — sirf khud kahi baatein li jati hain. */
  external: 2,
  redacted: 4,
  /** Khud-kaar record (archive ka prompt, memory note) — nichor ka source nahi. */
  automated: 8,
  truncated: 16,
  /**
   * User ne is jumle se bani yaad "bhool jao" kahi. Jumla record mein rehta hai
   * (hisaab), magar talash, state card aur nichor — teeno isay nahi dekhte.
   * Is ke baghair yaad to hat jati, magar wohi baat kachi baat-cheet se wapas aa jati.
   */
  forgotten: 32,
};

/**
 * Wo text jo insaan ne nahi kaha — Stonic ke apne khud-kaar prompts. Ye turn
 * ke taur par likhe jate hain (record mukammal rahe) magar kabhi yaad nahi
 * bante. Lens bhi `[Automated record` ko isi tarah chhorta hai.
 */
const AUTOMATED_PREFIXES = [
  '[Automated record',
  '[Memory note via Stonic',
  '[Memory request via Stonic',
  '[MEMORY NOTE]',
  '[SYSTEM_MEMORY_INJECTION]',
];

module.exports = {
  MIN, HOUR, DAY,
  FILES, LIMITS, BUDGETS, SEARCH, AGENT, DIGEST, INTENTIONS, JOBS, BACKUP,
  MEMORY_TYPES, SINGLE_VALUED_TYPES, MEMORY_STATUSES, ORIGINS, CHANNELS, SPEAKERS, MINDS,
  TURN_FLAGS, AUTOMATED_PREFIXES,
};
