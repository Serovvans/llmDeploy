import { Button, MiniModal } from '@skbkontur/react-ui';

import { texts } from '../texts';
import styles from './ConfirmModal.module.css';
import { Notice } from './Notice';

interface ConfirmModalProps {
  title: string;
  /** Над кем или чем действие — первая строка тела начертанием 600 (концепция §5.10). */
  who?: string;
  body: string;
  action: string;
  pending?: boolean;
  /** Подпись кнопки отказа от действия; по умолчанию — «Отмена». */
  cancel?: string;
  /** Отказ сервера — заметкой в теле окна, под последствиями (концепция §4.9). */
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Подтверждение необратимого действия (концепция §4.5): вопрос — в заголовке, последствия — в теле;
 * опасная кнопка с глаголом, фокус — на «Отмена».
 */
export function ConfirmModal(props: ConfirmModalProps) {
  const { title, who, body, action, pending = false, onConfirm, onCancel } = props;
  return (
    <MiniModal onClose={onCancel}>
      <MiniModal.Header>{title}</MiniModal.Header>
      <MiniModal.Body>
        {who && <p className={styles.who}>{who}</p>}
        <p>{body}</p>
        {props.error && (
          <div className={styles.error}>
            <Notice kind="error">{props.error}</Notice>
          </div>
        )}
      </MiniModal.Body>
      <MiniModal.Footer>
        <Button use="danger" loading={pending} onClick={onConfirm}>
          {action}
        </Button>
        <Button autoFocus disabled={pending} onClick={onCancel}>
          {props.cancel ?? texts.common.cancel}
        </Button>
      </MiniModal.Footer>
    </MiniModal>
  );
}
