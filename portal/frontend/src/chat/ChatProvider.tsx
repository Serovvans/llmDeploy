import { SingleToast } from '@skbkontur/react-ui';
import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, useState } from 'react';

import { api, isAbort, isApiError, NetworkError, StreamBrokenError } from '../api/client';
import type { StreamEvent } from '../api/stream';
import type { AnswerMode, Dialog, Knowledge, Message, PortalConfig } from '../api/types';
import { errorText, texts } from '../texts';
import { checkFile, sendErrorText, uploadErrorText } from './files';
import { chatReducer, EMPTY_DRAFT, INITIAL_STATE, NEW_CHAT, type ChatState, type Draft } from './state';

const MODE_STORAGE_KEY = 'portal.chat.mode';

function readMode(): AnswerMode {
  try {
    return window.localStorage.getItem(MODE_STORAGE_KEY) === 'thorough' ? 'thorough' : 'fast';
  } catch {
    // Хранилище недоступно — режим по умолчанию.
    return 'fast';
  }
}

const KNOWLEDGE_STORAGE_KEY = 'portal.chat.knowledge';

function readKnowledge(): Knowledge {
  try {
    const stored = window.localStorage.getItem(KNOWLEDGE_STORAGE_KEY);
    return stored === 'shared' || stored === 'shared_and_personal' ? stored : 'none';
  } catch {
    // Хранилище недоступно — база знаний по умолчанию не используется.
    return 'none';
  }
}

/** Вызывается, когда чат создан на сервере: экран переходит на его адрес. */
type OnCreated = (id: string) => void;

interface ChatApi {
  state: ChatState;
  mode: AnswerMode;
  setMode: (mode: AnswerMode) => void;
  knowledge: Knowledge;
  setKnowledge: (knowledge: Knowledge) => void;
  loadHistory: (more: boolean) => void;
  openDialog: (id: string) => void;
  refreshDialog: (id: string) => void;
  loadOlder: (id: string) => void;
  setText: (key: string, text: string) => void;
  setNotice: (key: string, notice: string | null) => void;
  addFiles: (key: string, files: File[], onCreated: OnCreated) => void;
  retryFile: (key: string, fileKey: string) => void;
  removeFile: (key: string, fileKey: string) => void;
  send: (key: string, onCreated: OnCreated) => void;
  regenerate: (id: string) => void;
  stop: (id: string) => void;
  rename: (id: string, title: string) => Promise<void>;
  remove: (id: string) => Promise<void>;
}

const ChatContext = createContext<ChatApi | null>(null);

export function useChat(): ChatApi {
  const chat = useContext(ChatContext);
  if (!chat) {
    throw new Error('useChat вызван вне ChatProvider');
  }
  return chat;
}

/**
 * Хранит чаты, пока открыто приложение: уход в другой раздел поток не закрывает,
 * ответ дописывается и виден при возвращении (концепция §5.5).
 */
export function ChatProvider({ config, children }: { config: PortalConfig | null; children: React.ReactNode }) {
  const [state, dispatch] = useReducer(chatReducer, INITIAL_STATE);
  const [mode, setModeState] = useState(readMode);
  const [knowledge, setKnowledgeState] = useState(readKnowledge);
  const stateRef = useRef(state);
  const streams = useRef(new Map<string, AbortController>());
  const fileCounter = useRef(0);
  // Чаты, созданные первым вложением: строка истории появляется после первого отправленного вопроса.
  const unlisted = useRef(new Map<string, Dialog>());

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  useEffect(() => {
    const open = streams.current;
    return () => open.forEach((controller) => controller.abort());
  }, []);

  const setMode = useCallback((next: AnswerMode) => {
    setModeState(next);
    try {
      window.localStorage.setItem(MODE_STORAGE_KEY, next);
    } catch {
      // Хранилище недоступно — выбор действует до закрытия вкладки.
    }
  }, []);

  const setKnowledge = useCallback((next: Knowledge) => {
    setKnowledgeState(next);
    try {
      window.localStorage.setItem(KNOWLEDGE_STORAGE_KEY, next);
    } catch {
      // Хранилище недоступно — выбор действует до закрытия вкладки.
    }
  }, []);

  const updateDraft = useCallback((key: string, update: (draft: Draft) => Draft) => {
    dispatch({ type: 'draftUpdated', key, update });
  }, []);

  const setNotice = useCallback(
    (key: string, notice: string | null) => updateDraft(key, (draft) => ({ ...draft, notice })),
    [updateDraft],
  );

  const setText = useCallback(
    (key: string, text: string) => updateDraft(key, (draft) => ({ ...draft, text, notice: null })),
    [updateDraft],
  );

  const loadHistory = useCallback((more: boolean) => {
    if (!more) {
      dispatch({ type: 'historyLoading' });
    }
    api.listDialogs('chat', more ? stateRef.current.history.nextCursor : null).then(
      (page) => dispatch({ type: 'historyLoaded', items: page.items, nextCursor: page.next_cursor, append: more }),
      () => dispatch({ type: 'historyFailed' }),
    );
  }, []);

  useEffect(() => {
    loadHistory(false);
  }, [loadHistory]);

  const refreshDialog = useCallback((id: string) => {
    api.listMessages(id, null).then(
      (page) => dispatch({ type: 'messagesLoaded', id, items: page.items, nextCursor: page.next_cursor, older: false }),
      (error: unknown) => dispatch({ type: 'messagesFailed', id, notFound: isApiError(error, 'not_found') }),
    );
  }, []);

  const openDialog = useCallback(
    (id: string) => {
      const view = stateRef.current.dialogs[id];
      if (view && view.status === 'ready') {
        return;
      }
      dispatch({ type: 'messagesLoading', id });
      api.listMessages(id, null).then(
        (page) => {
          dispatch({ type: 'messagesLoaded', id, items: page.items, nextCursor: page.next_cursor, older: false });
          // Чата может не быть в загруженной части истории: название для заголовка берётся отдельно.
          // Чат без сообщений в истории не показывается.
          if (page.items.length > 0 && !stateRef.current.history.items.some((item) => item.id === id)) {
            api.getDialog(id).then(
              (dialog) => dispatch({ type: 'dialogUpserted', dialog, toTop: false }),
              () => {
                // Название не получено: в заголовке остаётся «Новый чат».
              },
            );
          }
        },
        (error: unknown) => dispatch({ type: 'messagesFailed', id, notFound: isApiError(error, 'not_found') }),
      );
    },
    [],
  );

  const loadOlder = useCallback((id: string) => {
    const cursor = stateRef.current.dialogs[id]?.olderCursor;
    if (!cursor) {
      return;
    }
    api.listMessages(id, cursor).then(
      (page) => dispatch({ type: 'messagesLoaded', id, items: page.items, nextCursor: page.next_cursor, older: true }),
      (error: unknown) => SingleToast.push(errorText(error), { use: 'error' }),
    );
  }, []);

  /** Чат создаётся на сервере при первой отправке или первом вложении (контракт §5.2). */
  const ensureDialog = useCallback(async (key: string, onCreated: OnCreated): Promise<string> => {
    if (key !== NEW_CHAT) {
      return key;
    }
    const dialog = await api.createDialog('chat');
    unlisted.current.set(dialog.id, dialog);
    dispatch({ type: 'messagesLoaded', id: dialog.id, items: [], nextCursor: null, older: false });
    dispatch({ type: 'draftMoved', from: NEW_CHAT, to: dialog.id });
    onCreated(dialog.id);
    return dialog.id;
  }, []);

  const upload = useCallback(
    async (id: string, fileKey: string, file: File) => {
      const patch = (update: (draft: Draft) => Draft) => updateDraft(id, update);
      try {
        const attachment = await api.uploadAttachment(id, file);
        patch((draft) => ({
          ...draft,
          attachments: draft.attachments.map((item) =>
            item.key === fileKey ? { ...item, status: 'ready', attachment } : item,
          ),
        }));
      } catch (error) {
        if (error instanceof NetworkError) {
          // Загрузка оборвалась: вложение остаётся с «Повторить».
          patch((draft) => ({
            ...draft,
            attachments: draft.attachments.map((item) => (item.key === fileKey ? { ...item, status: 'failed' } : item)),
          }));
        } else if (config && !isApiError(error, 'unauthenticated')) {
          patch((draft) => ({
            ...draft,
            attachments: draft.attachments.filter((item) => item.key !== fileKey),
            notice: uploadErrorText(error, file.name, config.chat),
          }));
        }
      }
    },
    [config, updateDraft],
  );

  const addFiles = useCallback(
    (key: string, files: File[], onCreated: OnCreated) => {
      if (!config) {
        return;
      }
      const current = stateRef.current.drafts[key] ?? EMPTY_DRAFT;
      let notice: string | null = null;
      const accepted: { key: string; file: File }[] = [];
      for (const file of files) {
        const refusal = checkFile(file, config.chat);
        if (refusal) {
          notice = refusal;
        } else if (current.attachments.length + accepted.length >= config.chat.max_attachments) {
          notice = texts.chat.files.tooManyFiles(config.chat.max_attachments);
        } else {
          fileCounter.current += 1;
          accepted.push({ key: `file-${fileCounter.current}`, file });
        }
      }
      updateDraft(key, (draft) => ({
        ...draft,
        notice,
        attachments: [
          ...draft.attachments,
          ...accepted.map((item) => ({ ...item, status: 'uploading' as const, attachment: null })),
        ],
      }));
      if (accepted.length === 0) {
        return;
      }
      ensureDialog(key, onCreated).then(
        (id) => accepted.forEach((item) => void upload(id, item.key, item.file)),
        (error: unknown) =>
          updateDraft(key, (draft) => ({
            ...draft,
            notice: errorText(error),
            attachments: draft.attachments.filter((item) => !accepted.some((added) => added.key === item.key)),
          })),
      );
    },
    [config, ensureDialog, updateDraft, upload],
  );

  const retryFile = useCallback(
    (key: string, fileKey: string) => {
      const item = stateRef.current.drafts[key]?.attachments.find((candidate) => candidate.key === fileKey);
      if (!item) {
        return;
      }
      updateDraft(key, (draft) => ({
        ...draft,
        attachments: draft.attachments.map((candidate) =>
          candidate.key === fileKey ? { ...candidate, status: 'uploading' } : candidate,
        ),
      }));
      void upload(key, fileKey, item.file);
    },
    [updateDraft, upload],
  );

  const removeFile = useCallback(
    (key: string, fileKey: string) => {
      const item = stateRef.current.drafts[key]?.attachments.find((candidate) => candidate.key === fileKey);
      updateDraft(key, (draft) => ({
        ...draft,
        notice: null,
        attachments: draft.attachments.filter((candidate) => candidate.key !== fileKey),
      }));
      if (item?.attachment) {
        api.deleteAttachment(key, item.attachment.id).catch(() => {
          // Файл уже убран из сообщения; оставшийся на сервере удалится вместе с чатом.
        });
      }
    },
    [updateDraft],
  );

  /**
   * Читает поток до конца. Что делать при сбое — по правилу «Принят ли вопрос» (концепция §5.5):
   * отказ до статуса 200 возвращает экран к прежнему виду (`rollback`); обрыв после статуса 200 —
   * лента перечитывается; ответа нет вовсе — лента перечитывается, и прежний вид возвращается,
   * только если вопроса (`question`) в ней нет.
   */
  const runStream = useCallback(
    async (
      id: string,
      open: (signal: AbortSignal) => AsyncGenerator<StreamEvent>,
      question: string | null,
      rollback: () => void,
    ) => {
      const controller = new AbortController();
      streams.current.set(id, controller);
      const known = new Set((stateRef.current.dialogs[id]?.messages ?? []).map((message) => message.id));
      const reject = (notice: string) => {
        rollback();
        setNotice(id, notice);
      };
      const showSaved = (items: Message[], nextCursor: string | null) => {
        dispatch({ type: 'generationStopped', id });
        dispatch({ type: 'messagesLoaded', id, items, nextCursor, older: false });
        SingleToast.push(texts.common.noConnection, { use: 'error' });
      };
      try {
        for await (const event of open(controller.signal)) {
          if (event.type === 'error' && event.code === 'dialog_deleted') {
            // Чат удалён в другой вкладке: лента закрывается, набранный текст переезжает в новый чат.
            dispatch({ type: 'draftMoved', from: id, to: NEW_CHAT });
            dispatch({ type: 'dialogRemoved', id });
            SingleToast.push(texts.chat.remove.elsewhere);
            return;
          }
          dispatch({ type: 'streamEvent', id, event, now: Date.now() });
        }
      } catch (error) {
        if (isAbort(error)) {
          dispatch({ type: 'generationStopped', id });
        } else if (error instanceof StreamBrokenError) {
          // Вопрос сохранён: показываем его и ответ в сохранённом виде.
          dispatch({ type: 'generationStopped', id });
          SingleToast.push(texts.common.noConnection, { use: 'error' });
          refreshDialog(id);
        } else if (error instanceof NetworkError) {
          // Ответа нет вовсе: принят ли вопрос, скажет только лента.
          try {
            const page = await api.listMessages(id, null);
            const saved = page.items.some(
              (message) => message.role === 'user' && !known.has(message.id) && message.content === question,
            );
            // У повторной генерации вопроса для сверки нет: показываем ленту как на сервере.
            if (saved || question === null) {
              showSaved(page.items, page.next_cursor);
            } else {
              reject(texts.common.noConnection);
            }
          } catch {
            reject(texts.common.noConnection);
          }
        } else if (isApiError(error, 'unauthenticated')) {
          rollback();
        } else {
          // Без конфигурации (она не загрузилась) текст отказа — общий.
          reject(config ? sendErrorText(error, config) : errorText(error));
        }
      } finally {
        streams.current.delete(id);
        // Ответ закончился: просьба дождаться его больше не нужна.
        updateDraft(id, (draft) => (draft.notice === texts.chat.composer.waitAnswer ? { ...draft, notice: null } : draft));
      }
    },
    [config, refreshDialog, setNotice, updateDraft],
  );

  const send = useCallback(
    (key: string, onCreated: OnCreated) => {
      if (!config) {
        return;
      }
      const draft = stateRef.current.drafts[key] ?? EMPTY_DRAFT;
      const composer = texts.chat.composer;
      const ready = draft.attachments.flatMap((item) => (item.attachment ? [item.attachment] : []));
      const images = ready.reduce((sum, attachment) => sum + attachment.image_count, 0);
      let refusal: string | null = null;
      if (stateRef.current.dialogs[key]?.messages.at(-1)?.status === 'streaming') {
        refusal = composer.waitAnswer;
      } else if (draft.attachments.some((item) => item.status === 'uploading')) {
        refusal = composer.uploading;
      } else if (!draft.text.trim() && ready.length === 0) {
        refusal = composer.enterQuestion;
      } else if (draft.text.length > config.dialogs.message_max_chars) {
        refusal = composer.messageTooLong(config.dialogs.message_max_chars);
      } else if (images > config.chat.max_images) {
        refusal = texts.chat.files.tooManyImages(config.chat.max_images);
      }
      if (refusal) {
        setNotice(key, refusal);
        return;
      }

      const content = draft.text;
      ensureDialog(key, onCreated).then(
        (id) => {
          const before = stateRef.current.dialogs[id]?.messages ?? [];
          const now = Date.now();
          const created = unlisted.current.get(id);
          if (created) {
            unlisted.current.delete(id);
            dispatch({ type: 'dialogUpserted', dialog: created, toTop: true });
          } else if (!stateRef.current.history.items.some((item) => item.id === id)) {
            // Чат без сообщений, открытый по адресу (например, после обновления страницы): в истории его ещё нет.
            api.getDialog(id).then(
              (dialog) => dispatch({ type: 'dialogUpserted', dialog, toTop: true }),
              () => {
                // Строка появится при следующей загрузке истории.
              },
            );
          }
          dispatch({ type: 'generationStarted', id, question: { content, attachments: ready }, now });
          dispatch({ type: 'dialogTouched', id, now: new Date(now).toISOString() });
          updateDraft(id, () => EMPTY_DRAFT);
          const body = { content, attachment_ids: ready.map((a) => a.id), mode, knowledge };
          void runStream(
            id,
            (signal) => api.sendMessage(id, body, signal),
            content,
            () => {
              // Вопрос и вложения возвращаются в панель запроса.
              dispatch({ type: 'generationRejected', id, messages: before });
              // Набранное за время ожидания не теряется: возвращаемый текст встаёт перед ним.
              updateDraft(id, (current) => ({
                ...draft,
                text: [draft.text, current.text].filter((text) => text.trim()).join('\n\n'),
                attachments: [...draft.attachments, ...current.attachments],
              }));
              // Вопрос не принят: чат без сообщений в истории не показывается.
              if (created) {
                unlisted.current.set(id, created);
                dispatch({ type: 'dialogUnlisted', id });
              }
            },
          );
        },
        (error: unknown) => setNotice(key, errorText(error)),
      );
    },
    [config, ensureDialog, knowledge, mode, runStream, setNotice, updateDraft],
  );

  const regenerate = useCallback(
    (id: string) => {
      const before = stateRef.current.dialogs[id]?.messages ?? [];
      dispatch({ type: 'generationStarted', id, question: null, now: Date.now() });
      void runStream(
        id,
        (signal) => api.regenerate(id, signal),
        null,
        () => dispatch({ type: 'generationRejected', id, messages: before }),
      );
    },
    [runStream],
  );

  const stop = useCallback((id: string) => streams.current.get(id)?.abort(), []);

  const rename = useCallback(async (id: string, title: string) => {
    dispatch({ type: 'dialogUpserted', dialog: await api.renameDialog(id, title), toTop: false });
  }, []);

  const remove = useCallback(async (id: string) => {
    streams.current.get(id)?.abort();
    await api.deleteDialog(id);
    dispatch({ type: 'dialogRemoved', id });
  }, []);

  const value = useMemo<ChatApi>(
    () => ({
      state,
      mode,
      setMode,
      knowledge,
      setKnowledge,
      loadHistory,
      openDialog,
      refreshDialog,
      loadOlder,
      setText,
      setNotice,
      addFiles,
      retryFile,
      removeFile,
      send,
      regenerate,
      stop,
      rename,
      remove,
    }),
    [
      state,
      mode,
      setMode,
      knowledge,
      setKnowledge,
      loadHistory,
      openDialog,
      refreshDialog,
      loadOlder,
      setText,
      setNotice,
      addFiles,
      retryFile,
      removeFile,
      send,
      regenerate,
      stop,
      rename,
      remove,
    ],
  );

  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>;
}
