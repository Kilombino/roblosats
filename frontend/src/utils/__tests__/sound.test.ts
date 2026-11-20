import { playSound } from '../sound';

// 0.9.10: a refused play() was an unhandled rejection, reported as the app's last crash.
describe('playSound', () => {
  it('swallows a refused play()', async () => {
    const audio = {
      play: jest.fn(() =>
        Promise.reject(new Error('play() can only be initiated by a user gesture')),
      ),
    };
    const unhandled = jest.fn();
    process.on('unhandledRejection', unhandled);
    playSound(audio as unknown as HTMLAudioElement);
    await new Promise((r) => setTimeout(r, 10));
    process.off('unhandledRejection', unhandled);
    expect(audio.play).toHaveBeenCalled();
    expect(unhandled).not.toHaveBeenCalled();
  });

  it('survives play() throwing and a missing element', () => {
    expect(() =>
      playSound({
        play: () => {
          throw new Error('x');
        },
      } as unknown as HTMLAudioElement),
    ).not.toThrow();
    expect(() => playSound(null)).not.toThrow();
  });
});
