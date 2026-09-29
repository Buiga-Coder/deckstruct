'use strict';
// Browser multipart filenames are UTF-8 bytes; Busboy's default parameter charset
// is latin1. Decode only an exact, valid byte roundtrip. Preserve real Unicode.
module.exports = function filename(value) {
  if (typeof value !== 'string' || !/[\u00c2-\u00f4]/.test(value) || [...value].some(c => c.codePointAt(0) > 255)) return value;
  try {
    const bytes = Buffer.from(value, 'latin1');
    const decoded = new TextDecoder('utf-8', {fatal:true}).decode(bytes);
    return Buffer.from(decoded, 'utf8').equals(bytes) ? decoded : value;
  } catch { return value; }
};
