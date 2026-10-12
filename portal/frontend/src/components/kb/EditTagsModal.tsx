import { Button, Gapped, Modal, SingleToast } from '@skbkontur/react-ui';
import { useState } from 'react';

import { api, isApiError } from '../../api/client';
import type { KbDocument, PortalConfig } from '../../api/types';
import { tagsErrorText } from '../../kb/documents';
import { texts } from '../../texts';
import styles from './EditTagsModal.module.css';
import { TagsField, type TagsProblem } from './TagsField';

const t = texts.kb.tags;

interface EditTagsModalProps {
  document: KbDocument;
  /** Пределы тегов; `null` — настройки не загрузились. */
  limits: PortalConfig['kb']['tags'] | null;
  /** Теги сохранены: строка списка обновляется по ответу сервера. */
  onSaved: (document: KbDocument) => void;
  /** Права на документ уже нет: строку пора перезапросить. */
  onForbidden: () => void;
  /** Документ удалён в другой вкладке: строка убирается. */
  onDeleted: () => void;
  onClose: () => void;
}

/** Окно «Теги документа» (концепция §5.6): список заменяется целиком, повторной обработки нет. */
export function EditTagsModal({ document, limits, onSaved, onForbidden, onDeleted, onClose }: EditTagsModalProps) {
  const [tags, setTags] = useState(document.tags);
  const [problem, setProblem] = useState<TagsProblem | null>(null);
  const [saving, setSaving] = useState(false);

  const save = async () => {
    setSaving(true);
    try {
      onSaved(await api.setKbDocumentTags(document.id, tags));
      SingleToast.push(t.saved);
      onClose();
    } catch (failure) {
      setSaving(false);
      if (isApiError(failure, 'forbidden')) {
        SingleToast.push(document.scope === 'ministries' ? t.forbiddenMinistries : t.forbidden, { use: 'error' });
        onForbidden();
        onClose();
      } else if (isApiError(failure, 'not_found')) {
        SingleToast.push(texts.viewer.deleted, { use: 'error' });
        onDeleted();
        onClose();
      } else if (!isApiError(failure, 'unauthenticated')) {
        setProblem({ text: tagsErrorText(failure, limits) ?? t.failed, blocks: false });
      }
    }
  };

  return (
    <Modal width={480} ignoreBackgroundClick onClose={onClose} disableClose={saving}>
      <Modal.Header>{t.title}</Modal.Header>
      <Modal.Body>
        <p className={styles.document}>{document.title}</p>
        <TagsField
          tags={tags}
          limits={limits}
          problem={problem}
          hint={limits ? t.hint(limits.max_per_document, limits.max_length) : ''}
          disabled={saving}
          autoFocus
          onChange={(next, nextProblem) => {
            setTags(next);
            setProblem(nextProblem);
          }}
        />
      </Modal.Body>
      <Modal.Footer>
        <Gapped gap={8}>
          <Button use="primary" loading={saving} disabled={problem?.blocks} onClick={() => void save()}>
            {t.save}
          </Button>
          <Button disabled={saving} onClick={onClose}>
            {texts.common.cancel}
          </Button>
        </Gapped>
      </Modal.Footer>
    </Modal>
  );
}
