import styles from './QrCode.module.css';

interface QrCodeProps {
  size: number;
  path: string;
  label: string;
}

/**
 * QR-код из данных сервера (`qr.size`, `qr.path`): без `<img>`, адресов `data:` и вставки разметки.
 * Всегда чёрный на белом — иначе в тёмной теме камера его не распознаёт (концепция §5.4).
 */
export function QrCode({ size, path, label }: QrCodeProps) {
  return (
    <div className={styles.frame}>
      <svg
        className={styles.code}
        viewBox={`0 0 ${size} ${size}`}
        shapeRendering="crispEdges"
        role="img"
        aria-label={label}
      >
        <path d={path} />
      </svg>
    </div>
  );
}
