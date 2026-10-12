import { texts } from '../../texts';
import { Notice } from '../Notice';
import styles from './AudioPlayer.module.css';

interface AudioPlayerProps {
  ref: React.Ref<HTMLAudioElement>;
  src: string;
  /** Имя проигрывателя для экранного чтения: «Запись ‹название›». */
  label: string;
  /** Момент, на который проигрыватель встаёт при открытии, в секундах. */
  startAt: number;
  failed: boolean;
  onFailedChange: (failed: boolean) => void;
}

/**
 * Проигрыватель записи (концепция §4.4) — стандартный элемент браузера: пуск, перемотка, громкость и
 * клавиатура — его собственные. При открытии встаёт на момент цитаты и ждёт нажатия: звук сам не включается.
 * Звук не загрузился — заметка с «Повторить» на месте проигрывателя.
 */
export function AudioPlayer({ ref, src, label, startAt, failed, onFailedChange }: AudioPlayerProps) {
  if (failed) {
    return (
      <Notice kind="error" action={{ label: texts.common.retry, onClick: () => onFailedChange(false) }}>
        {texts.viewer.audioFailed}
      </Notice>
    );
  }
  return (
    <audio
      ref={ref}
      className={styles.audio}
      controls
      preload="metadata"
      src={src}
      aria-label={label}
      onLoadedMetadata={(event) => {
        // Момент, выбранный до загрузки метаданных (кнопка в расшифровке), не затирается.
        if (event.currentTarget.currentTime === 0) {
          event.currentTarget.currentTime = startAt;
        }
      }}
      onError={() => onFailedChange(true)}
    />
  );
}
