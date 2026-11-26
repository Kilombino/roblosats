import { ZAPSTORE_APP_URL, showGetAndroidApp } from '..';
import en from '../../../../static/locales/en.json';
import es from '../../../../static/locales/es.json';
import ca from '../../../../static/locales/ca.json';

// 0.9.16 (Kilombino TG 15678): "Get the Android app" only on the web
describe('Get the Android app', () => {
  it('is offered on the web clients only', () => {
    expect(showGetAndroidApp('web')).toBe(true);
    expect(showGetAndroidApp('selfhosted')).toBe(true);
    expect(showGetAndroidApp('mobile')).toBe(false);
    expect(showGetAndroidApp('desktop')).toBe(false);
  });
  it('links to the Zapstore page of the app, with texts in en, es and ca', () => {
    expect(ZAPSTORE_APP_URL).toBe('https://zapstore.dev/apps/com.roblosats');
    for (const k of ['Get the Android app on Zapstore', 'Also on', 'and'])
      for (const d of [en, es, ca] as Array<Record<string, string>>) expect(d[k]).toBeTruthy();
  });
});
