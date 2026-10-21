import { Button, Gapped, Modal, type Textarea } from '@skbkontur/react-ui';
import { useEffect, useRef, useState } from 'react';

import { api, ApiError, isApiError, NetworkError } from '../../api/client';
import type { DialogKind, DialogSummary } from '../../api/types';
import { draftNotice, type DraftExit, type DraftNotice } from '../../memory/draft';
import { useSummaryDraft } from '../../memory/useSummaryDraft';
import { errorText, texts } from '../../texts';
import { SummaryDraft } from './SummaryDraft';
import styles from './MemoryModals.module.css';

const t = texts.memory;

/** Отказ при сохранении (`PUT`, `POST` с `carried`): заметка над полем или ошибка под полем (концепция §4.9). */
export interface SaveFailure {
  notice: DraftNotice | null;
  field: string | null;
}

function fieldCode(error: ApiError, field: string): string | null {
  return error.fields.find((item) => item.field === field)?.code ?? null;
}

/**
 * Отказ сохранения пересказа или переноса; `null` — окно уходит само (диалог удалён, функция выключена,
 * сессия кончилась). `field` — имя поля текста в отказе, `texts` — тексты окна.
 */
export function saveFailure(
  error: unknown,
  field: string,
  words: { enter: string; tooLong: string; changed: DraftNotice },
): SaveFailure | null {
  if (error instanceof NetworkError) {
    return { notice: { text: texts.common.noConnection, action: null }, field: null };
  }
  if (!(error instanceof ApiError)) {
    return { notice: { text: texts.common.actionFailed, action: null }, field: null };
  }
  if (['not_found', 'feature_disabled', 'unauthenticated'].includes(error.code)) {
    return null;
  }
  if (error.code === 'generation_in_progress') {
    return { notice: { text: t.changeBusy, action: null }, field: null };
  }
  if (error.code === 'validation_error') {
    const code = fieldCode(error, field);
    if (code === 'required') {
      return { notice: null, field: words.enter };
    }
    if (code === 'too_long') {
      return { notice: null, field: words.tooLong };
    }
    if (fieldCode(error, 'until_message_id') === 'unknown_value') {
      return { notice: words.changed, field: null };
    }
  }
  return { notice: { text: errorText(error), action: null }, field: null };
}

/** Выход из окна по отказу: диалог удалён или функция выключена. */
export function exitOf(error: unknown): DraftExit | null {
  if (isApiError(error, 'not_found')) {
    return 'deleted';
  }
  if (isApiError(error, 'feature_disabled')) {
    return 'disabled';
  }
  return isApiError(error, 'unauthenticated') ? 'session' : null;
}

interface CompressModalProps {
  dialogId: string;
  kind: DialogKind;
  /** `compress` — «Сжать раннюю часть» с черновиком потоком; `edit` — «Изменить» применённый пересказ. */
  mode: 'compress' | 'edit';
  /** Применённый пересказ: при сжатии — повторное сжатие, при правке — текст и граница. */
  summary: DialogSummary | null;
  compressPairs: { from: number; to: number } | null;
  keepRecent: number;
  maxChars: number;
  onSaved: (summary: DialogSummary) => void;
  /** Сжимать нечего (`409 nothing_to_summarize`): оценка устарела, панель запрашивает её заново. */
  onStale: () => void;
  onExit: (exit: DraftExit) => void;
  onClose: () => void;
}

/** Окно «Сжать раннюю часть» и «Изменить пересказ» (концепция §4.9): черновик готовится сразу при открытии. */
export function CompressModal(props: CompressModalProps) {
  const { dialogId, mode, summary, compressPairs, keepRecent, maxChars, onExit } = props;
  const draft = useSummaryDraft(dialogId, onExit);
  const { start, load } = draft;
  const field = useRef<Textarea>(null);
  const [saving, setSaving] = useState(false);
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [saveNotice, setSaveNotice] = useState<DraftNotice | null>(null);
  const edit = mode === 'edit';
  const { state } = draft;

  // Сотрудник уже сказал, чего хочет: черновик начинается при открытии (Д-34); фокус — в поле.
  const initial = useRef({ edit, summary });
  useEffect(() => {
    const { edit: editing, summary: applied } = initial.current;
    if (editing && applied) {
      load(applied.text, applied.until_message_id);
    } else {
      start({ purpose: 'compress' });
    }
    field.current?.focus();
  }, [load, start]);

  const failure = state.failure;
  const stale = failure?.kind === 'refused' && failure.error.code === 'nothing_to_summarize';
  const { onStale } = props;
  useEffect(() => {
    if (stale) {
      onStale();
    }
  }, [stale, onStale]);

  const restart = () => {
    setSaveNotice(null);
    setFieldError(null);
    start({ purpose: 'compress' });
  };

  const apply = async () => {
    const text = state.text.trim();
    const problem =
      state.phase === 'streaming'
        ? t.draft.wait
        : !text || !state.until
          ? t.compress.enter
          : state.text.length > maxChars
            ? t.compress.tooLong(maxChars)
            : null;
    setFieldError(problem);
    if (problem || !state.until) {
      return;
    }
    setSaving(true);
    setSaveNotice(null);
    try {
      props.onSaved(await api.saveSummary(dialogId, { text: state.text, until_message_id: state.until }));
    } catch (error) {
      setSaving(false);
      const exit = exitOf(error);
      if (exit) {
        onExit(exit);
        return;
      }
      const result = saveFailure(error, 'text', {
        enter: t.compress.enter,
        tooLong: t.compress.tooLong(maxChars),
        changed: edit ? { text: t.compress.changedEdit, action: null } : { text: t.compress.changed, action: 'restart' },
      });
      setSaveNotice(result?.notice ?? null);
      setFieldError(result?.field ?? null);
    }
  };

  const notice =
    saveNotice ??
    (failure ? draftNotice(failure, state.text.length > 0, { purpose: 'compress', keepRecent, verbatim: false }) : null);
  const intro = [
    summary && compressPairs ? t.compress.repeat(compressPairs.from, compressPairs.to) : t.compress.first(keepRecent),
    t.compress.rest,
  ].join(' ');

  return (
    <Modal width={720} ignoreBackgroundClick disableClose={saving} onClose={props.onClose}>
      <Modal.Header>{edit ? t.compress.editTitle : t.compress.title}</Modal.Header>
      <Modal.Body>
        <div className={styles.body}>
          {!edit && (
            <p className={styles.intro}>
              {props.kind === 'docparse' ? `${intro}. ${t.compress.docparse}` : intro}
            </p>
          )}
          <SummaryDraft
            state={state}
            label={t.summary.title}
            maxChars={maxChars}
            notice={notice}
            fieldError={fieldError === t.draft.wait && state.phase !== 'streaming' ? null : fieldError}
            disclaimer={t.compress.disclaimer}
            textareaRef={field}
            onEdit={(text) => {
              draft.edit(text);
              setFieldError(null);
            }}
            onStop={draft.stop}
            onRestart={restart}
          />
        </div>
      </Modal.Body>
      <Modal.Footer>
        <Gapped gap={8}>
          <Button use="primary" loading={saving} onClick={() => void apply()}>
            {edit ? t.compress.save : t.compress.apply}
          </Button>
          <Button disabled={saving} onClick={props.onClose}>
            {texts.common.cancel}
          </Button>
        </Gapped>
      </Modal.Footer>
    </Modal>
  );
}
