/**
 * 0.9.10: plays a short sound and never lets a refusal escape. Browsers (and WebViews until the
 * app allowed it) refuse audio that does not come from a tap: an unhandled play() rejection
 * ("play() can only be initiated by a user gesture") was reported as the app's last crash.
 * A refused sound is primed on the next tap (played muted and stopped), so the following ones
 * are allowed.
 */
const waiting = new Set<HTMLAudioElement>();
let listening = false;

const unlockOnTap = (): void => {
  if (listening || typeof document === 'undefined') return;
  listening = true;
  const unlock = (): void => {
    document.removeEventListener('pointerdown', unlock, true);
    document.removeEventListener('keydown', unlock, true);
    listening = false;
    waiting.forEach((audio) => {
      const muted = audio.muted;
      audio.muted = true;
      try {
        void Promise.resolve(audio.play())
          .then(() => {
            audio.pause();
            audio.currentTime = 0;
          })
          .catch(() => {})
          .finally(() => {
            audio.muted = muted;
          });
      } catch {
        audio.muted = muted;
      }
    });
    waiting.clear();
  };
  document.addEventListener('pointerdown', unlock, true);
  document.addEventListener('keydown', unlock, true);
};

export const playSound = (audio: HTMLAudioElement | null | undefined): void => {
  if (!audio) return;
  try {
    const result = audio.play();
    if (result && typeof result.catch === 'function') {
      result.catch(() => {
        waiting.add(audio);
        unlockOnTap();
      });
    }
  } catch {
    waiting.add(audio);
    unlockOnTap();
  }
};

export default playSound;
