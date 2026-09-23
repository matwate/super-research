// Browser-side state: the user's keys and saved starting templates.

const KEYS = "sr.keys";
const TEMPLATES = "sr.templates";
const DRAFT = "sr.draft";

function read(storage, key, fallback) {
  try {
    const raw = storage.getItem(key);
    return raw == null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}
function write(storage, key, value) {
  try {
    storage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage blocked or full */
  }
}
function remove(storage, key) {
  try {
    storage.removeItem(key);
  } catch {
    /* storage blocked */
  }
}

// Keys: localStorage when "remember" is on, otherwise this tab only.
let memoryKeys = null;
export function getKeys() {
  return memoryKeys || read(sessionStorage, KEYS, null) || read(localStorage, KEYS, null) || {};
}
export function keysRemembered() {
  return !!read(localStorage, KEYS, null);
}
export function setKeys(keys, remember) {
  const clean = Object.fromEntries(Object.entries(keys).map(([k, v]) => [k, (v || "").trim()]).filter(([, v]) => v));
  memoryKeys = clean;
  remove(localStorage, KEYS);
  remove(sessionStorage, KEYS);
  if (Object.keys(clean).length) write(remember ? localStorage : sessionStorage, KEYS, clean);
}

export function listSavedTemplates() {
  return read(localStorage, TEMPLATES, []);
}
export function saveTemplate(entry) {
  const all = listSavedTemplates().filter((t) => t.id !== entry.id);
  all.push(entry);
  write(localStorage, TEMPLATES, all);
}
export function deleteTemplate(id) {
  write(localStorage, TEMPLATES, listSavedTemplates().filter((t) => t.id !== id));
}

// The unsent new-run form, so a reload or a trip to the Keys page keeps it.
export const draft = {
  get: () => read(sessionStorage, DRAFT, null),
  set: (v) => write(sessionStorage, DRAFT, v),
};
