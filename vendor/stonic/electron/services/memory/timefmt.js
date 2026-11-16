// Waqt ko insaani lafzon mein — model ke liye (aur neeche: model ke lafzon se waqt).
//
// Model tareekh ginne mein kamzor hota hai ("ye 3 hafte pehle tha ya 3 mahine?")
// aur ghalat ginti seedha ghalat jawab ban jati hai. Is liye hisaab code karta
// hai aur model ko tayyar lafz milte hain: "3 weeks ago (12 Sep 2026)".
// Prompts English mein hain, is liye ye bhi English mein hai.

const DAY = 24 * 60 * 60 * 1000;
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const startOfDay = (t) => { const d = new Date(t); d.setHours(0, 0, 0, 0); return d.getTime(); };

/** "12 Sep 2026" */
function absoluteDate(ts) {
  const d = new Date(ts);
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

/** "today" | "yesterday" | "3 days ago" | "2 weeks ago" | "5 months ago" | "about a year ago" | "in 2 days" */
function relativeTime(ts, now = Date.now()) {
  if (!Number.isFinite(ts) || ts <= 0) return '';
  // Calendar ke din ginte hain, 24-24 ghante nahi: raat 11 baje ki baat subah
  // 9 baje "kal" ki hai, chahe 10 hi ghante guzre hon.
  const days = Math.round((startOfDay(now) - startOfDay(ts)) / DAY);
  if (days === 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days === -1) return 'tomorrow';
  const abs = Math.abs(days);
  let span;
  if (abs < 14) span = `${abs} days`;
  else if (abs < 60) span = `${Math.round(abs / 7)} weeks`;
  else if (abs < 330) span = `${Math.round(abs / 30)} months`;
  else if (abs < 550) span = 'about a year';
  else span = `${Math.round(abs / 365)} years`;
  return days > 0 ? `${span} ago` : `in ${span}`;
}

/** "3 weeks ago (12 Sep 2026)" — haal hi ki baat par tareekh nahi (shor hai). */
function whenText(ts, now = Date.now()) {
  const rel = relativeTime(ts, now);
  if (!rel) return '';
  const days = Math.abs(Math.round((startOfDay(now) - startOfDay(ts)) / DAY));
  return days < 7 ? rel : `${rel} (${absoluteDate(ts)})`;
}

// ── "kab" — voice ke follow_up tool se ───────────────────────────────────────
//
// Ulta raasta: yahan model lafz deta hai aur code waqt nikalta hai. Voice ke
// model ko aaj ki tareekh pata nahi hoti aur wo ginti mein kamzor hai, is liye
// wo "kal shaam 5 baje" ko khud tareekh mein nahi badalta — sirf chhoti shakl
// likhta hai ("tomorrow 17:00", "+10m") aur hisaab PC ki ghari se hota hai.

const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const WEEKDAYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];
const WEEKDAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
/** Waqt na diya ho ("tomorrow") to subah ka ye ghanta — nichor ka `parseDate` bhi yehi leta hai. */
const DEFAULT_HOUR = 9;
/** Is se aage ka waada ghalti hai (model ne saal ghalat likh diya). */
const MAX_AHEAD = 5 * 365 * DAY;

/**
 * "17:00" | "5pm" | "5:30 pm" -> { h, m }; samajh na aaye to null.
 * Akela "5" nahi chalta: subah ya shaam, ye andaza yahan nahi lagaya jata.
 */
function parseClock(text) {
  const m = /^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/.exec(text);
  if (!m || (!m[2] && !m[3])) return null;
  let h = Number(m[1]);
  const min = m[2] ? Number(m[2]) : 0;
  if (m[3]) {
    if (h < 1 || h > 12) return null;
    h = (h % 12) + (m[3] === 'pm' ? 12 : 0);
  }
  return h > 23 || min > 59 ? null : { h, m: min };
}

/**
 * `follow_up` ka "kab" -> waqt (ms). Ye shaklein chalti hain:
 *   "+10m" · "+2h" · "+3d"                      abhi se itni der baad
 *   "17:00" · "5pm"                             aaj; guzar chuka ho to kal
 *   "today 17:00" · "tomorrow" · "tomorrow 9am"
 *   "fri" · "friday 15:00"                      agla aisa din (aaj bhi, agar waqt baqi ho)
 *   "2026-10-12" · "2026-10-12 10:00"
 *
 * Samajh na aaye, ya waqt guzar chuka ho, to `null` — bulane wala saaf inkaar
 * karta hai. Andaze se koi waqt nahi lagaya jata: ghalat waqt ka waada "nahi
 * likha" se bura hai.
 */
function parseWhen(value, now = Date.now()) {
  const text = String(value || '').trim().toLowerCase().replace(/\s+/g, ' ');
  if (!text) return null;
  const inRange = (ts) => (Number.isFinite(ts) && ts > now && ts <= now + MAX_AHEAD ? ts : null);

  const rel = /^(?:\+|in )\s?(\d+(?:\.\d+)?) ?(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$/.exec(text);
  if (rel) {
    const unit = rel[2][0] === 'm' ? MINUTE : (rel[2][0] === 'h' ? HOUR : DAY);
    return inRange(now + Math.round(Number(rel[1]) * unit));
  }

  const today = new Date(now);
  /** Aaj se `days` din baad, us ghari par. Date khud mahine ki hadd aur DST sambhalta hai. */
  const at = (days, clock) => new Date(today.getFullYear(), today.getMonth(), today.getDate() + days, clock.h, clock.m, 0).getTime();

  const bare = parseClock(text);
  if (bare) return inRange(at(0, bare) > now ? at(0, bare) : at(1, bare));

  const named = /^(today|tomorrow|sun|mon|tue|wed|thu|fri|sat)[a-z]*(?: (?:at )?(.+))?$/.exec(text);
  if (named) {
    const clock = named[2] ? parseClock(named[2]) : { h: DEFAULT_HOUR, m: 0 };
    if (!clock) return null;
    if (named[1] === 'today') {
      // "aaj" bina waqt ke: subah guzar chuki ho to ek ghante baad.
      return inRange(named[2] ? at(0, clock) : Math.max(at(0, clock), now + HOUR));
    }
    if (named[1] === 'tomorrow') return inRange(at(1, clock));
    const ahead = (WEEKDAYS.indexOf(named[1]) - today.getDay() + 7) % 7;
    return inRange(at(ahead, clock) > now ? at(ahead, clock) : at(ahead + 7, clock));
  }

  const dated = /^(\d{4})-(\d{2})-(\d{2})(?:[t ](?:at )?(.+))?$/.exec(text);
  if (dated) {
    const clock = dated[4] ? parseClock(dated[4]) : { h: DEFAULT_HOUR, m: 0 };
    if (!clock) return null;
    return inRange(new Date(Number(dated[1]), Number(dated[2]) - 1, Number(dated[3]), clock.h, clock.m, 0).getTime());
  }
  return null;
}

/** "5:00 PM" */
function clockText(ts) {
  const d = new Date(ts);
  return `${d.getHours() % 12 || 12}:${String(d.getMinutes()).padStart(2, '0')} ${d.getHours() < 12 ? 'AM' : 'PM'}`;
}

/**
 * Aane wala waqt, model ke bolne laiq:
 * "in 10 minutes (3:45 PM)" | "today at 5:00 PM" | "tomorrow at 9:00 AM" | "on Fri 9 Oct 2026 at 5:00 PM"
 */
function dueText(ts, now = Date.now()) {
  const clock = clockText(ts);
  const diff = ts - now;
  if (diff >= 0 && diff < 90 * MINUTE) {
    const mins = Math.max(1, Math.round(diff / MINUTE));
    return `in ${mins} ${mins === 1 ? 'minute' : 'minutes'} (${clock})`;
  }
  const days = Math.round((startOfDay(ts) - startOfDay(now)) / DAY);
  if (days === 0) return `today at ${clock}`;
  if (days === 1) return `tomorrow at ${clock}`;
  return `on ${WEEKDAY_NAMES[new Date(ts).getDay()]} ${absoluteDate(ts)} at ${clock}`;
}

module.exports = { relativeTime, absoluteDate, whenText, startOfDay, parseWhen, dueText, clockText, WEEKDAY_NAMES, DAY };
