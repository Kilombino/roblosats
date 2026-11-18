// rl31-beta20: a cleared numeric field holds '' / null (no stuck "0", no "01").
import { cleanNumberInput, numberFromInput, parseNumberInput } from '../numberInput';

const field = (value: string): HTMLInputElement => ({ value }) as HTMLInputElement;

describe('numeric inputs', () => {
  it('cleared means empty, not 0', () => {
    expect(numberFromInput(field(''))).toBeNull();
    expect(parseNumberInput('-')).toBeNull();
    expect(parseNumberInput('abc')).toBeNull();
    expect(parseNumberInput('0')).toBe(0);
  });

  it('strips leading zeros and writes them back', () => {
    const f = field('01');
    expect(cleanNumberInput(f)).toBe('1');
    expect(f.value).toBe('1');
    expect(cleanNumberInput(field('0.5'))).toBe('0.5');
    expect(cleanNumberInput(field('-05'))).toBe('-5');
    expect(numberFromInput(field('00025000'))).toBe(25000);
  });
});
