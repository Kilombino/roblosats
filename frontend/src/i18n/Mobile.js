import i18n from 'i18next';
import LanguageDetector from 'i18next-browser-languagedetector';
import { initReactI18next } from 'react-i18next';

import translationEN from '../../static/locales/en.json';

// 0.9.6: only English (the fallback) is inside the main bundle; the phone's language is loaded
// from its own chunk before the app renders (this module awaits it), and any other language
// when the user picks it. Up to 0.9.5 the 17 languages (≈1.9 MB of JSON) were parsed at every
// start.
export const LANGUAGES = [
  'en',
  'es',
  'ru',
  'de',
  'ja',
  'pl',
  'fr',
  'ca',
  'it',
  'pt',
  'eu',
  'sw',
  'cs',
  'th',
  'sv',
  'zh-SI',
  'zh-TR',
];

const loadLocale = async (lng) => {
  if (!LANGUAGES.includes(lng) || i18n.hasResourceBundle(lng, 'translations')) return;
  const data = await import(
    /* webpackChunkName: "locale-[request]" */ `../../static/locales/${lng}.json`
  );
  i18n.addResourceBundle(lng, 'translations', data.default ?? data, true, true);
};

const config = {
  resources: {
    en: { translations: translationEN },
  },
  fallbackLng: 'en',
  debug: false,
  // have a common namespace used around the full app
  ns: ['translations'],
  defaultNS: 'translations',
  keySeparator: false, // we use content as keys
  interpolation: {
    escapeValue: false,
    formatSeparator: ',',
  },
  react: {
    useSuspense: false,
  },
};

await i18n.use(LanguageDetector).use(initReactI18next).init(config);

const resolve = (lng) => {
  if (!lng) return 'en';
  if (LANGUAGES.includes(lng)) return lng;
  const short = lng.substring(0, 2);
  return LANGUAGES.includes(short) ? short : 'en';
};

const startLng = resolve(i18n.language);
if (startLng !== 'en') {
  try {
    await loadLocale(startLng);
    await i18n.changeLanguage(startLng);
  } catch (e) {
    console.warn('could not load language', startLng, e);
  }
}

// Settings → language: load it, then switch (i18next re-renders when the bundle arrives)
const changeLanguage = i18n.changeLanguage.bind(i18n);
i18n.changeLanguage = async (lng, ...rest) => {
  const target = resolve(lng);
  try {
    await loadLocale(target);
  } catch (e) {
    console.warn('could not load language', target, e);
  }
  return await changeLanguage(target, ...rest);
};

export default i18n;
