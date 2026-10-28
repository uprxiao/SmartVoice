import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CorrectionObserver, parseNativeObservation, type ObservationIdentity, type ObservationSample, type CorrectionSubmission } from './correction-observer';

const identity = (): ObservationIdentity => ({ origin: 'http://127.0.0.1:17863', userId: 'user-a', authEpoch: 1,
  sessionId: 'session-a', resultId: 'result-a', insertionId: 'insertion-a', targetId: 'target-a' });
function setup() {
  const id = identity();
  let sample: ObservationSample = { valid: true, text: '加一个按纽', composition: 'committed' };
  const poll = vi.fn(async () => sample);
  const submit = vi.fn(async (_body: CorrectionSubmission, _identity: ObservationIdentity, _signal: AbortSignal) => ({ event_id: 'event-a' }));
  const release = vi.fn();
  const onSubmitted = vi.fn();
  const onEnd = vi.fn();
  const isCurrent = vi.fn(() => true);
  const shouldRetry = vi.fn(() => false);
  const observer = new CorrectionObserver({ identity: id, insertedText: '加一个按钮', poll, submit,
    release, onSubmitted, onEnd, isCurrent, shouldRetry });
  return { id, observer, poll, submit, release, onSubmitted, onEnd, isCurrent, shouldRetry,
    setSample: (next: ObservationSample) => { sample = next; } };
}

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(0); });
afterEach(() => { vi.useRealTimers(); });
describe('CorrectionObserver', () => {
  it('keeps observing a long-lived real composition and waits for committed stability', async () => {
    const f = setup(); f.observer.confirmReceipt(f.id);
    f.setSample({ valid: true, text: '加一个an jian', composition: 'composing' });
    f.observer.start();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(f.submit).not.toHaveBeenCalled();
    expect(f.onEnd).not.toHaveBeenCalled();
    f.setSample({ valid: true, text: '加一个按键', composition: 'committed' });
    await vi.advanceTimersByTimeAsync(15_299);
    expect(f.submit).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(f.submit).toHaveBeenCalledTimes(1);
    expect(f.submit.mock.calls[0]?.[0]).toMatchObject({ before: '按钮', after: '按键' });
  });

  it('waits a full 15 seconds after the final changed sample and sends one event', async () => {
    const f = setup();
    f.observer.confirmReceipt(f.id); f.observer.start();
    await vi.advanceTimersByTimeAsync(15_299);
    expect(f.submit).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(f.submit).toHaveBeenCalledTimes(1);
    expect(f.submit.mock.calls[0]?.[0]).toMatchObject({ session_id: 'session-a', insertion_id: 'insertion-a',
      offset: 3, before: '按钮', after: '按纽', left: '加一个' });
    expect(f.onSubmitted).toHaveBeenCalledTimes(1);
    expect(f.onEnd).toHaveBeenCalledWith('submitted', f.id);
    expect(f.release).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(f.submit).toHaveBeenCalledTimes(1);
  });

  it('does not add an initial poll interval to the unchanged 15 second window', async () => {
    const f = setup(); f.setSample({ valid: true, text: '加一个按钮', composition: 'committed' });
    f.observer.start();
    await vi.advanceTimersByTimeAsync(14_999);
    expect(f.onEnd).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(f.onEnd).toHaveBeenCalledWith('unchanged', f.id);
  });

  it('does not send without an exact confirmed receipt; ends the stable window', async () => {
    const f = setup(); f.observer.start();
    expect(f.observer.confirmReceipt({ ...f.id, insertionId: 'other' })).toBe(false);
    await vi.advanceTimersByTimeAsync(15_300);
    expect(f.submit).not.toHaveBeenCalled();
    expect(f.onEnd).toHaveBeenCalledWith('receipt_unconfirmed', f.id);
    expect(f.observer.confirmReceipt(f.id)).toBe(false);
  });

  it('accepts a late receipt before the stable window ends', async () => {
    const f = setup(); f.observer.start();
    await vi.advanceTimersByTimeAsync(14_900);
    expect(f.observer.confirmReceipt(f.id)).toBe(true);
    await vi.advanceTimersByTimeAsync(400);
    expect(f.submit).toHaveBeenCalledTimes(1);
  });

  it.each(['unknown', 'composing'] as const)('never submits %s IME state', async composition => {
    const f = setup(); f.setSample({ valid: true, text: '加一个按纽', composition });
    f.observer.confirmReceipt(f.id); f.observer.start();
    await vi.advanceTimersByTimeAsync(15_300);
    expect(f.submit).not.toHaveBeenCalled();
    if (composition === 'unknown') expect(f.onEnd).toHaveBeenCalledWith('ime_unknown', f.id);
    else {
      expect(f.onEnd).not.toHaveBeenCalled();
      await vi.advanceTimersByTimeAsync(44_700);
      expect(f.onEnd).toHaveBeenCalledWith('deadline', f.id);
      expect(f.submit).not.toHaveBeenCalled();
      expect(f.release).toHaveBeenCalledTimes(1);
    }
  });

  it('starts a fresh stable period when the IME commit is proven', async () => {
    const f = setup(); f.setSample({ valid: true, text: '加一个按纽', composition: 'composing' });
    f.observer.confirmReceipt(f.id); f.observer.start();
    await vi.advanceTimersByTimeAsync(3_000);
    f.setSample({ valid: true, text: '加一个按纽', composition: 'committed' });
    await vi.advanceTimersByTimeAsync(15_299);
    expect(f.submit).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    expect(f.submit).toHaveBeenCalledTimes(1);
  });

  it.each(['target_changed', 'outside_changed', 'not_observable'])('ends on %s and releases only the frozen target', async reason => {
    const f = setup(); f.setSample({ valid: false, reason, composition: 'unknown' });
    f.observer.start();
    Object.assign(f.id, { targetId: 'new-target' });
    await vi.advanceTimersByTimeAsync(300);
    expect(f.submit).not.toHaveBeenCalled();
    expect(f.release.mock.calls[0]?.[0].targetId).toBe('target-a');
    f.observer.stop();
    expect(f.release).toHaveBeenCalledTimes(1);
  });

  it('retries only an explicitly retryable failure with the same ID and payload', async () => {
    const f = setup();
    f.shouldRetry.mockReturnValue(true);
    f.submit.mockRejectedValueOnce(new Error('transport'));
    f.observer.confirmReceipt(f.id); f.observer.start();
    await vi.advanceTimersByTimeAsync(15_300);
    expect(f.submit).toHaveBeenCalledTimes(2);
    expect(f.submit.mock.calls[0]?.[0]).toBe(f.submit.mock.calls[1]?.[0]);
    expect(f.onSubmitted).toHaveBeenCalledTimes(1);
  });

  it('makes at most two attempts and releases on permanent submission failure', async () => {
    const f = setup(); f.shouldRetry.mockReturnValue(true); f.submit.mockRejectedValue(new Error('offline'));
    f.observer.confirmReceipt(f.id); f.observer.start();
    await vi.advanceTimersByTimeAsync(15_300);
    expect(f.submit).toHaveBeenCalledTimes(2);
    expect(f.onEnd).toHaveBeenCalledWith('submit_failed', f.id);
    expect(f.release).toHaveBeenCalledTimes(1);
  });

  it('cancels an in-flight submission and ignores its late response after account switch', async () => {
    const f = setup();
    let resolve!: (value: { event_id: string }) => void;
    f.submit.mockImplementation(() => new Promise(r => { resolve = r; }));
    f.observer.confirmReceipt(f.id); f.observer.start();
    await vi.advanceTimersByTimeAsync(15_300);
    const signal = f.submit.mock.calls[0]?.[2];
    f.isCurrent.mockReturnValue(false); f.observer.stop('identity_changed');
    expect(signal?.aborted).toBe(true);
    resolve({ event_id: 'late-event' });
    await vi.advanceTimersByTimeAsync(1);
    expect(f.onSubmitted).not.toHaveBeenCalled();
    expect(f.release).toHaveBeenCalledTimes(1);
  });

  it('has an independent 60 second deadline even when polling never returns', async () => {
    const f = setup(); f.poll.mockImplementation(() => new Promise(() => undefined));
    f.observer.start();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(f.onEnd).toHaveBeenCalledWith('receipt_unconfirmed', f.id);
    expect(f.release).toHaveBeenCalledTimes(1);
    expect(f.submit).not.toHaveBeenCalled();
  });

  it('does not send a protected or multi-point edit', async () => {
    const f = setup(); f.setSample({ valid: true, text: '改两个按纽', composition: 'committed' });
    f.observer.confirmReceipt(f.id); f.observer.start();
    await vi.advanceTimersByTimeAsync(15_300);
    expect(f.submit).not.toHaveBeenCalled();
    expect(f.onEnd).toHaveBeenCalledWith('invalid_replacement', f.id);
  });
});

describe('parseNativeObservation', () => {
  it('does not invent committed evidence for legacy / malformed helper responses', () => {
    expect(parseNativeObservation({ valid: true, text: '字' }).composition).toBe('unknown');
    expect(parseNativeObservation({ valid: true, text: '字', composition: 'false' }).composition).toBe('unknown');
    expect(parseNativeObservation({ valid: true, text: 10 }).valid).toBe(false);
    expect(parseNativeObservation({ valid: true, text: '字', composition: 'committed' }).composition).toBe('committed');
  });
});
