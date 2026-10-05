import { Button, MiniModal } from '@skbkontur/react-ui';

import { texts } from '../texts';
import styles from './ConfirmModal.module.css';

interface ConfirmModalProps {
  title: string;
  /** Над кем или чем действие — первая строка тела начертанием 600 (концепция §5.10). */
  who?: string;
  body: string;
  action: string;
  pending?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * Подтверждение необратимого действия (концепция §4.5): вопрос — в заголовке, последствия — в теле;
 * опасная кнопка с глаголом, фокус — на «Отмена».
 */
export function ConfirmModal({ title, who, body, action, pending = false, onConfirm, onCancel }: ConfirmModalProps) {
  return (
    <MiniModal onClose={onCancel}>
      <MiniModal.Header>{title}</MiniModal.Header>
      <MiniModal.Body>
        {who && <p className={styles.who}>{who}</p>}
        <p>{body}</p>
      </MiniModal.Body>
      <MiniModal.Footer>
        <Button use="danger" loading={pending} onClick={onConfirm}>
          {action}
        </Button>
        <Button autoFocus disabled={pending} onClick={onCancel}>
          {texts.common.cancel}
        </Button>
      </MiniModal.Footer>
    </MiniModal>
  );
}
