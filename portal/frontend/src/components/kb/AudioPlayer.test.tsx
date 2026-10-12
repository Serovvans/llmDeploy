import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { AudioPlayer } from './AudioPlayer';

function setup(): HTMLAudioElement {
  render(<AudioPlayer ref={null} src="/audio" label="Запись" startAt={760} failed={false} onFailedChange={() => {}} />);
  return screen.getByLabelText('Запись') as HTMLAudioElement;
}

describe('проигрыватель записи', () => {
  it('после загрузки метаданных встаёт на момент цитаты', () => {
    const audio = setup();
    fireEvent.loadedMetadata(audio);
    expect(audio.currentTime).toBe(760);
  });

  it('момент, выбранный до загрузки метаданных, не затирается моментом цитаты', () => {
    const audio = setup();
    audio.currentTime = 790;
    fireEvent.loadedMetadata(audio);
    expect(audio.currentTime).toBe(790);
  });
});
