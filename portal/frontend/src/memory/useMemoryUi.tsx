import { IconTextBulletChecklistRegular16 } from '@skbkontur/icons/IconTextBulletChecklistRegular16';
import { Button, Hint } from '@skbkontur/react-ui';
import { useCallback, useEffect, useId, useRef, useState } from 'react';

import type { DialogKind, QuestionParams } from '../api/types';
import { useChat } from '../chat/ChatProvider';
import type { DialogView } from '../chat/state';
import { MemoryMeter } from '../components/memory/MemoryMeter';
import { MemoryPanel, type PanelFocus } from '../components/memory/MemoryPanel';
import { Notice } from '../components/Notice';
import { useSession } from '../session/SessionContext';
import { texts } from '../texts';
import styles from './MemoryToggle.module.css';
import { readPanelOpen, storePanelOpen } from './panelPreference';
import { useDialogMemory } from './useDialogMemory';

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
}

interface Options {
  kind: DialogKind;
  dialogId: string | null;
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
}

/**
 * «Память диалога» на экране диалога (концепция §3.2, §4.9): кнопка в строке заголовка, индикатор у
 * поля ввода, панель справа и связь описи с лентой. Положение панели запоминается в браузере.
 */
export function useMemoryUi({
  kind,
  dialogId,
  view,
  available,
  params,
  busy,
  viewer,
  onNewDialog,
  onSchemaMissing,
}: Options): MemoryUi {
  const { config } = useSession();
  const { loadOlderUntil } = useChat();
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

  if (!active || !available) {
    return {
      active,
      toggle: null,
      meter: null,
      panel: null,
      longDialog: null,
      feed: { onOpenPair: null, marked: null, reveal: null, revealing: false },
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
  } else if (level === null && dropped) {
    // Оценки нет (сбой): последний ответ уже отбросил ранние сообщения — это известно и без неё.
    longText = t.longDialog[group].dropped;
  }
  const openAction = { label: t.openPanel, onClick: () => show('heading') };

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
      />
    ) : null,
    longDialog: longText ? (
      <Notice kind="info" action={onNewDialog ? [openAction, { label: texts.dialogs[kind].newLabel, onClick: onNewDialog }] : openAction}>
        {longText}
      </Notice>
    ) : null,
    feed: {
      onOpenPair: (questionId) => show({ questionId }),
      marked: hovered ?? flash,
      reveal,
      revealing,
    },
  };
}
