/* =========================================================
   Gihigugma — text statistics (word counter)

   Runs entirely in the browser, so counts update as you type.
   The same rules are used everywhere a count is shown.

   Counting rules
     Words       A run of letters or digits. An apostrophe or hyphen inside
                 a word keeps it one word (don't, well-known), and so does a
                 decimal point or thousands separator inside a number (3.5,
                 12,400) and the dots of an abbreviation (e.g., U.S.).
                 Punctuation and symbols on their own are not words.
     Characters  Every character except line breaks. "Without spaces" also
                 leaves out spaces and tabs.
     Sentences   Text ending in . ! ? or … followed by a space or the end of
                 a line. Dots in common abbreviations (Dr., e.g., etc.),
                 initials (J. Cruz), acronyms (U.S.) and decimals do not end
                 a sentence. A last line without closing punctuation still
                 counts when it contains a word.
     Paragraphs  Lines that contain text. Blank lines are not counted.
   ========================================================= */
(function (root) {
  "use strict";

  // number with separators | dotted abbreviation (e.g, U.S) | ordinary word
  const WORD = /\p{N}+(?:[.,]\p{N}+)+|\p{L}(?:\.\p{L})+(?![\p{L}\p{N}])|[\p{L}\p{N}]+(?:['’\-][\p{L}\p{N}]+)*/gu;
  const HAS_WORD = /[\p{L}\p{N}]/u;
  const ABBREVIATION = /\b(Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|e\.g|i\.e|No|Fig|Eq|Vol|pp|approx|Inc|Ltd|Co|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\./gi;
  const ACRONYM = /\b(?:\p{L}\.){2,}/gu;
  const INITIAL = /(?<=^|\s)(\p{Lu})\.(?=\s+\p{Lu})/gu;      // a lone capital, as in "J. Cruz"
  const DECIMAL = /(\p{N})\.(?=\p{N})/gu;
  const SENTENCE_END = /[.!?…]+["'”’)\]]*(?:\s+|$)/u;
  const KEPT = "\u0001";      // stands in for a dot that does not end a sentence

  function countWords(text) {
    return (text.match(WORD) || []).length;
  }

  function countSentences(text) {
    let total = 0;
    for (const line of text.split(/\r?\n/)) {
      const safe = line
        .replace(DECIMAL, "$1" + KEPT)
        .replace(ACRONYM, (match, offset, whole) => {
          // "U.S." ends a sentence only when a new one clearly starts after it (or the line ends).
          const after = whole.slice(offset + match.length);
          const ends = /^\s*$/.test(after) || /^\s+\p{Lu}/u.test(after);
          const inner = match.slice(0, -1).replace(/\./g, KEPT);
          return inner + (ends ? "." : KEPT);
        })
        .replace(ABBREVIATION, (match) => match.slice(0, -1).replace(/\./g, KEPT) + KEPT)
        .replace(INITIAL, "$1" + KEPT);
      for (const piece of safe.split(SENTENCE_END)) {
        if (HAS_WORD.test(piece)) total++;
      }
    }
    return total;
  }

  function countParagraphs(text) {
    return text.split(/\r?\n/).filter((line) => line.trim()).length;
  }

  /** All counts for a piece of text. */
  function stats(text) {
    text = text || "";
    return {
      words: countWords(text),
      characters: Array.from(text.replace(/[\r\n]/g, "")).length,
      charactersNoSpaces: Array.from(text.replace(/\s/g, "")).length,
      sentences: countSentences(text),
      paragraphs: countParagraphs(text),
    };
  }

  const api = { stats, countWords, countSentences, countParagraphs };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.TextStats = api;
})(typeof window !== "undefined" ? window : globalThis);
