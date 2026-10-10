// Word-counter accuracy. Run from the eros folder:   node tests/test_textstats.js
const assert = require("node:assert/strict");
const { stats } = require("../frontend/textstats.js");

const cases = [
  ["empty text", "", { words: 0, characters: 0, charactersNoSpaces: 0, sentences: 0, paragraphs: 0 }],
  ["only spaces and blank lines", "   \n\n \t ", { words: 0, sentences: 0, paragraphs: 0, charactersNoSpaces: 0 }],
  ["simple sentence", "The cat sat on the mat.", { words: 6, characters: 23, charactersNoSpaces: 18, sentences: 1, paragraphs: 1 }],
  ["contractions are one word", "Don't stop; it's John's turn.", { words: 5, sentences: 1 }],
  ["curly apostrophes too", "It’s the user’s choice.", { words: 4 }],
  ["hyphenated words are one word", "A well-known, state-of-the-art method.", { words: 4, sentences: 1 }],
  ["a dash between words is not a word", "Flooding - a serious problem - is rising.", { words: 6 }],
  ["numbers with decimals and separators", "It cost 3.5 million, or 12,400 pesos each.", { words: 8, sentences: 1 }],
  ["punctuation alone is not a word", "Wait... what?! — yes.", { words: 3, sentences: 3 }],
  ["extra whitespace is ignored for words", "  one   two\tthree  ", { words: 3, characters: 19, charactersNoSpaces: 11 }],
  ["question and exclamation", "Is it true? Yes! It is.", { words: 6, sentences: 3 }],
  ["abbreviations do not end sentences", "Dr. Cruz met Prof. Reyes, e.g. on Monday. They agreed.", { words: 10, sentences: 2 }],
  ["initials do not end sentences", "J. P. Rizal wrote it. It is famous.", { words: 8, sentences: 2 }],
  ["acronym inside a sentence", "The U.S. economy grew. It slowed later.", { sentences: 2 }],
  ["acronym at the end of a sentence", "She moved to the U.S. Then she studied.", { sentences: 2 }],
  ["decimals do not end sentences", "The value is 3.14 exactly. Good.", { sentences: 2 }],
  ["quote after the full stop", "He said \"stop.\" Then he left.", { sentences: 2 }],
  ["last line without a full stop still counts", "First point.\nSecond point without a stop", { sentences: 2, paragraphs: 2 }],
  ["ellipsis character", "Well… maybe. Sure.", { sentences: 3 }],
  ["paragraphs skip blank lines", "Para one.\n\n\nPara two.\n   \nPara three.", { paragraphs: 3, sentences: 3 }],
  ["line breaks are not characters", "ab\ncd", { characters: 4, charactersNoSpaces: 4, paragraphs: 2 }],
  ["windows line endings", "ab\r\ncd\r\n", { characters: 4, paragraphs: 2, words: 2 }],
  ["emoji count as one character", "ok 👍", { characters: 4, charactersNoSpaces: 3, words: 1 }],
  ["other scripts", "Mahal kita. 你好 世界", { words: 4, sentences: 2 }],
  ["citations and brackets", "Rainfall rose by 12% (Cruz, 2020) [3].", { words: 7, sentences: 1 }],
];

let failed = 0;
for (const [name, text, expected] of cases) {
  const got = stats(text);
  try {
    for (const [key, value] of Object.entries(expected)) assert.equal(got[key], value, `${key}: expected ${value}, got ${got[key]}`);
    console.log(`ok    ${name}`);
  } catch (err) {
    failed++;
    console.log(`FAIL  ${name}  ->  ${err.message}   (text: ${JSON.stringify(text)})`);
  }
}
console.log(`\n${cases.length - failed} of ${cases.length} word-counter cases passed`);
process.exit(failed ? 1 : 0);
