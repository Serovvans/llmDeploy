import { IconTextBulletChecklistRegular16 } from '@skbkontur/icons/IconTextBulletChecklistRegular16';
import { Button, Hint, SingleToast } from '@skbkontur/react-ui';
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api, isApiError, NetworkError } from '../api/client';
import type { Dialog, DialogKind, DialogSummary, QuestionParams } from '../api/types';
import { useChat } from '../chat/ChatProvider';
import type { DialogView } from '../chat/state';
import { ConfirmModal } from '../components/ConfirmModal';
import { CompressModal } from '../components/memory/CompressModal';
import { dialogPath } from '../components/memory/MemoryBlocks';
import { MemoryMeter } from '../components/memory/MemoryMeter';
import { MemoryPanel, type PanelFocus } from '../components/memory/MemoryPanel';
import { TransferModal, type TransferSource } from '../components/memory/TransferModal';
import { Notice } from '../components/Notice';
import { useSession } from '../session/SessionContext';
import { errorText, texts } from '../texts';
import type { DraftExit } from './draft';
import styles from './MemoryToggle.module.css';
import { readPanelOpen, storePanelOpen } from './panelPreference';
import { defaultMemoryQuery, memoryQuery, useDialogMemory } from './useDialogMemory';

const t = texts.memory;
/** Сколько держится отметка пары в ленте после перехода к ней из описи (концепция §4.9). */
const FLASH_MS = 2000;

/** Что лента знает о памяти диалога: пометки пар, отметка из описи, переход к паре. */
export interface FeedMemory {
  /** Пометки «Модель не учитывает» — кнопки в панель; при выключенной функции — текст (концепция §4.9, Д-36). */
  onOpenPair: ((questionId: string) => void) | null;
  /** Вопрос, пара которого отмечена полосой на полях. */
  marked: string | null;
  /** Прокрутить ленту к вопросу; `n` — номер запроса. */
  reveal: { questionId: string; n: number } | null;
  /** Лента подгружает ранние сообщения до нужной пары. */
  revealing: boolean;
  /** «Продолжить отсюда» под ответом (0.8); `null` — функция выключена. */
  onContinue: ((answerId: string) => void) | null;
}

interface Options {
  kind: DialogKind;
  dialogId: string | null;
  /** Название открытого диалога для окна переноса; `null` — названия нет. */
  title: string | null;
  view: DialogView | undefined;
  /** На экране есть лента с панелью запроса (у разбора — экран результата); иначе панели и кнопки нет. */
  available: boolean;
  params: QuestionParams;
  /** В диалоге формируется ответ — здесь или в другой вкладке. */
  busy: boolean;
  /** Просмотр источника временно занимает место панели (концепция §3.3). */
  viewer?: { isOpen: boolean; close: () => void };
  /** «Новый чат» в заметке о длинном диалоге; у разбора документа действия нет. */
  onNewDialog?: () => void;
  onSchemaMissing?: () => void;
}

export interface MemoryUi {
  /** Функция включена: без неё экран — как до «Памяти диалога» (ПД §7). */
  active: boolean;
  toggle: React.ReactNode;
  meter: React.ReactNode;
  panel: React.ReactNode;
  /** Заметка о длинном диалоге по уровню памяти; `null` — заметки нет. Только при включённой функции. */
  longDialog: React.ReactNode;
  feed: FeedMemory;
  /** Окна сжатия, отмены сжатия и переноса (0.8). */
  dialogs: React.ReactNode;
  /**
   * «Перенести в новый диалог…» из `Kebab` строки заголовка или истории; `opener` — куда вернуть фокус
   * после закрытия окна (пункт меню к этому времени исчезнет). `null` — функция выключена.
   */
  transfer: ((dialog: Dialog, opener: string) => void) | null;
}

/** Открытое окно памяти: сжатие и правка пересказа, отмена сжатия, перенос. */
type MemoryModal =
  | { type: 'compress' | 'edit' }
  | { type: 'cancel'; pending: boolean; error: string | null }
  | { type: 'transfer'; source: TransferSource; until: string | null };

/**
 * «Память диалога» на экране диалога (концепция §3.2, §4.9): кнопка в строке заголовка, индикатор у
 * поля ввода, панель справа и связь описи с лентой. Положение панели запоминается в браузере.
 */
export function useMemoryUi({
  kind,
  dialogId,
  title,
  view,
  available,
  params,
  busy,
  viewer,
  onNewDialog,
  onSchemaMissing,
}: Options): MemoryUi {
  const { config } = useSession();
  const { loadOlderUntil, showSummary, addDialog, forgetDialog } = useChat();
  const navigate = useNavigate();
  const panelId = useId();
  const ready = dialogId === null || view?.status === 'ready';
  const memory = useDialogMemory({
    kind,
    dialogId,
    ready,
    enabled: config?.memory.enabled === true,
    params,
    busy,
    onSchemaMissing,
  });
  const [open, setOpen] = useState(readPanelOpen);
  const [focus, setFocus] = useState<PanelFocus | null>(null);
  const opener = useRef<Element | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [reveal, setReveal] = useState<FeedMemory['reveal']>(null);
  // Переход к паре относится к своему диалогу: при возврате в диалог лента снова следует за ответом.
  const [revealFor, setRevealFor] = useState(dialogId);
  if (revealFor !== dialogId) {
    setRevealFor(dialogId);
    setReveal(null);
  }
  const [revealing, setRevealing] = useState(false);
  const requests = useRef(0);
  const viewerOpen = viewer?.isOpen ?? false;
  const closeViewer = viewer?.close;
  const active = memory.active;
  const shown = active && available && open && !viewerOpen;

  const remember = (next: boolean) => {
    setOpen(next);
    storePanelOpen(next);
  };

  /** Открыть панель (или только перевести в неё фокус); просмотр источника сначала закрывается. */
  const show = useCallback(
    (target: PanelFocus['target']) => {
      opener.current = document.activeElement;
      closeViewer?.();
      setOpen(true);
      storePanelOpen(true);
      requests.current += 1;
      setFocus({ target, n: requests.current });
    },
    [closeViewer],
  );

  const hide = () => {
    remember(false);
    // Фокус — туда, откуда панель открыли; открытая сама при загрузке — на кнопку в строке заголовка.
    const back = opener.current;
    const target = back instanceof HTMLElement && back.isConnected ? back : null;
    (target ?? document.querySelector<HTMLElement>('[data-memory-toggle] button'))?.focus();
  };

  // Фокус в панель — после закрытия просмотра источника, который возвращает фокус на свою карточку.
  const pairsCount = memory.memory?.pairs.length ?? 0;
  const done = useRef<number | null>(null);
  useEffect(() => {
    if (!shown || !focus || done.current === focus.n) {
      return;
    }
    const panel = document.getElementById(panelId);
    const heading = panel?.querySelector<HTMLElement>('h2');
    if (focus.target === 'heading') {
      done.current = focus.n;
      heading?.focus();
      return;
    }
    const select = panel?.querySelector<HTMLElement>(`[data-memory-select="${focus.target.questionId}"] button`);
    if (select) {
      done.current = focus.n;
      select.scrollIntoView({ block: 'nearest' });
      select.focus();
    } else if (memory.status !== 'loading') {
      done.current = focus.n;
      heading?.focus();
    }
  }, [shown, focus, panelId, memory.status, pairsCount]);

  // Событие `context_truncated` сразу поднимает уровень до «переполнена».
  const last = view?.messages.at(-1);
  const lastAnswer = last?.role === 'assistant' ? last : undefined;
  const dropped = (lastAnswer?.dropped_messages ?? 0) > 0;
  const truncatedNow = view?.generation != null && dropped;
  const { markTruncated } = memory;
  useEffect(() => {
    if (truncatedNow) {
      markTruncated();
    }
  }, [truncatedNow, markTruncated]);

  // Отметка пары держится 2 секунды после перехода к ней.
  useEffect(() => {
    if (!flash) {
      return;
    }
    const timer = window.setTimeout(() => setFlash(null), FLASH_MS);
    return () => window.clearTimeout(timer);
  }, [flash]);

  const revealPair = (questionId: string) => {
    if (!dialogId) {
      return;
    }
    setFlash(questionId);
    const go = () => {
      requests.current += 1;
      setReveal({ questionId, n: requests.current });
    };
    if (view?.messages.some((message) => message.id === questionId)) {
      go();
      return;
    }
    // Пара старше показанных сообщений: лента сначала подгружает их до неё.
    setRevealing(true);
    void loadOlderUntil(dialogId, questionId).then((found) => {
      setRevealing(false);
      if (found) {
        go();
      }
    });
  };

  // Окна сжатия и переноса (0.8): фокус после закрытия — туда, откуда окно открыли, иначе в поле ввода (§9).
  const [modal, setModal] = useState<MemoryModal | null>(null);
  const modalOpener = useRef<Element | string | null>(null);
  const openModal = (next: MemoryModal, from: Element | string | null = document.activeElement) => {
    modalOpener.current = from;
    setModal(next);
  };
  const closeModal = (focusBack = true) => {
    setModal(null);
    const from = modalOpener.current;
    modalOpener.current = null;
    if (!focusBack) {
      return;
    }
    // Окно уже закрыто, когда фокус переходит: до этого его удерживает само окно.
    window.setTimeout(() => {
      const element = typeof from === 'string' ? document.querySelector(from) : from;
      const target = element instanceof HTMLElement && element.isConnected ? element : null;
      (target ?? document.querySelector<HTMLElement>('[data-composer] textarea'))?.focus();
    });
  };
  const keepRecent = config?.memory.keep_recent_pairs ?? 0;
  const maxChars = config?.memory.max_chars ?? 0;
  const { refresh, disable } = memory;

  /** Окно ушло по отказу: диалог удалён (здесь — или в другой вкладке), функция выключена, сессия кончилась. */
  const exitModal = (exit: DraftExit, source: { id: string; kind: DialogKind }) => {
    closeModal(exit !== 'session');
    if (exit === 'deleted') {
      forgetDialog(source.id);
      SingleToast.push(texts.dialogs[source.kind].removedElsewhere);
    } else if (exit === 'disabled') {
      disable();
    }
  };

  const openTransfer = (source: TransferSource, until: string | null, from?: string) =>
    openModal({ type: 'transfer', source, until }, from);

  const summary = view?.summary ?? null;
  const cancelSummary = () => {
    if (!dialogId || modal?.type !== 'cancel') {
      return;
    }
    setModal({ type: 'cancel', pending: true, error: null });
    api.deleteSummary(dialogId).then(
      () => {
        showSummary(dialogId, null);
        SingleToast.push(t.summary.cancelled);
        closeModal();
        refresh('summary');
      },
      (error: unknown) => {
        if (isApiError(error, 'not_found')) {
          exitModal('deleted', { id: dialogId, kind });
        } else if (isApiError(error, 'feature_disabled')) {
          exitModal('disabled', { id: dialogId, kind });
        } else if (isApiError(error, 'unauthenticated')) {
          exitModal('session', { id: dialogId, kind });
        } else {
          const text = isApiError(error, 'generation_in_progress')
            ? t.changeBusy
            : error instanceof NetworkError
              ? texts.common.noConnection
              : errorText(error);
          setModal({ type: 'cancel', pending: false, error: text });
        }
      },
    );
  };

  let dialogs: React.ReactNode = null;
  if (active && modal && config) {
    const here = dialogId ? { id: dialogId, kind } : null;
    if ((modal.type === 'compress' || modal.type === 'edit') && here) {
      dialogs = (
        <CompressModal
          dialogId={here.id}
          kind={kind}
          mode={modal.type}
          summary={summary}
          compressPairs={memory.memory?.compress_pairs ?? null}
          keepRecent={keepRecent}
          maxChars={maxChars}
          onSaved={(saved: DialogSummary) => {
            showSummary(here.id, saved);
            SingleToast.push(modal.type === 'edit' ? t.summary.saved : t.summary.applied);
            closeModal();
            refresh('summary');
          }}
          onStale={() => refresh('stale')}
          onExit={(exit) => exitModal(exit, here)}
          onClose={() => closeModal()}
        />
      );
    } else if (modal.type === 'cancel') {
      dialogs = (
        <ConfirmModal
          title={t.summary.cancelTitle}
          body={t.summary.cancelBody}
          action={t.summary.cancel}
          cancel={t.summary.keep}
          pending={modal.pending}
          error={modal.error}
          onConfirm={cancelSummary}
          onCancel={() => closeModal()}
        />
      );
    } else if (modal.type === 'transfer') {
      const { source } = modal;
      dialogs = (
        <TransferModal
          source={source}
          until={modal.until}
          query={source.id === dialogId ? memoryQuery(params) : defaultMemoryQuery(source.kind, config)}
          loaded={source.id === dialogId ? (view?.messages ?? []) : []}
          keepRecent={keepRecent}
          maxChars={maxChars}
          onDone={(created) => {
            // Новый диалог сразу в истории, под названием по виду, пока модель не предложит своё (Д-39).
            addDialog(created);
            closeModal(false);
            navigate(dialogPath(created.kind, created.id));
          }}
          onExit={(exit) => exitModal(exit, source)}
          onClose={() => closeModal()}
        />
      );
    }
  }
  const transfer = active
    ? (target: Dialog, from: string) => openTransfer({ id: target.id, kind: target.kind, title: target.title }, null, from)
    : null;

  if (!active || !available) {
    return {
      active,
      toggle: null,
      meter: null,
      panel: null,
      longDialog: null,
      feed: { onOpenPair: null, marked: null, reveal: null, revealing: false, onContinue: null },
      dialogs,
      transfer,
    };
  }

  const toggle = (
    <span data-memory-toggle="" className={styles.toggle}>
      <Hint text={shown ? t.hidePanel : t.showPanel}>
        <Button
          size="small"
          icon={<IconTextBulletChecklistRegular16 />}
          active={shown}
          aria-expanded={shown}
          aria-controls={panelId}
          // Закрытие кнопкой оставляет фокус на ней (концепция §9).
          onClick={() => (shown ? remember(false) : show('heading'))}
        >
          {t.title}
        </Button>
      </Hint>
    </span>
  );

  const group = kind === 'docparse' ? 'docparse' : 'dialog';
  const level = memory.level;
  let longText: string | null = null;
  if (level === 'almost_full') {
    longText = t.longDialog[group].almost_full;
  } else if (level === 'overflow') {
    longText = dropped ? t.longDialog[group].dropped : t.longDialog[group].overflow;
  } else if (level === null && dropped && memory.status !== 'loading') {
    // Оценки нет (сбой): последний ответ уже отбросил ранние сообщения — это известно и без неё.
    longText = t.longDialog[group].dropped;
  }
  const openAction = { label: t.openPanel, onClick: () => show('heading') };
  // Есть что сжать (0.8) — заметка предлагает сжатие вместо панели (§4.9).
  const compressAction = { label: t.summary.compress, disabled: busy, onClick: () => openModal({ type: 'compress' }) };
  const firstAction = memory.memory?.compress_pairs ? compressAction : openAction;
  const current: TransferSource | null = dialogId ? { id: dialogId, kind, title } : null;

  return {
    active,
    toggle,
    meter: <MemoryMeter memory={memory} panelId={panelId} panelOpen={shown} onOpen={() => show('heading')} />,
    panel: shown ? (
      <MemoryPanel
        id={panelId}
        kind={kind}
        memory={memory}
        busy={busy}
        onClose={hide}
        onHover={setHovered}
        onReveal={revealPair}
        summary={summary}
        carried={view?.carried ?? null}
        keepRecent={keepRecent}
        onCompress={() => openModal({ type: 'compress' })}
        onEditSummary={() => openModal({ type: 'edit' })}
        onCancelSummary={() => openModal({ type: 'cancel', pending: false, error: null })}
        onTransfer={current ? () => openTransfer(current, null) : null}
      />
    ) : null,
    longDialog: longText ? (
      <Notice
        kind="info"
        action={onNewDialog ? [firstAction, { label: texts.dialogs[kind].newLabel, onClick: onNewDialog }] : firstAction}
      >
        {longText}
      </Notice>
    ) : null,
    feed: {
      onOpenPair: (questionId) => show({ questionId }),
      marked: hovered ?? flash,
      reveal,
      revealing,
      onContinue: current ? (answerId) => openTransfer(current, answerId) : null,
    },
    dialogs,
    transfer,
  };
}
