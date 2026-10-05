/** Состояние чата: история, сообщения диалогов, идущие ответы и черновики панели запроса. Чистые функции. */
import type { StreamEvent } from '../api/stream';
import type { Attachment, Dialog, DialogKind, Message, Source } from '../api/types';
import { footnoteNumbers } from './footnotes';

/** Сообщение на экране: `error_message` — текст сервера для кода, которого нет в словаре (только из потока). */
export interface ChatMessage extends Message {
  error_message?: string;
}

/** Ответ, который формируется в этой вкладке. */
export interface Generation {
  /** `preparing` — поиск закончен, текста ещё нет: к «Отправляю…» строка состояния не возвращается. */
  phase: 'sending' | 'searching' | 'preparing' | 'thinking' | 'answering';
  startedAt: number;
  reasoningStartedAt: number | null;
  /** Всё найденное в базе знаний (событие `sources`); под ответом остаются только упомянутые в тексте. */
  found: Source[] | null;
}

export interface DialogView {
  /** `deleted` — чат удалён (здесь или в другой вкладке): экран уходит в новый чат. */
  status: 'loading' | 'ready' | 'not_found' | 'failed' | 'deleted';
  /** По возрастанию времени. */
  messages: ChatMessage[];
  olderCursor: string | null;
  generation: Generation | null;
}

export interface DraftAttachment {
  key: string;
  file: File;
  status: 'uploading' | 'ready' | 'failed';
  attachment: Attachment | null;
}

/** Черновик панели запроса; живёт, пока открыто приложение. */
export interface Draft {
  text: string;
  attachments: DraftAttachment[];
  /** Сообщение под панелью запроса. */
  notice: string | null;
}

/** История диалогов одного вида; `idle` — ещё не запрашивалась. */
export interface History {
  status: 'idle' | 'loading' | 'ready' | 'failed';
  items: Dialog[];
  nextCursor: string | null;
}

/** Диалоги всех видов: чат, SQL-помощник, помощник CoGIS, вопросы по разобранному документу. */
export interface ChatState {
  history: Record<DialogKind, History>;
  dialogs: Record<string, DialogView>;
  drafts: Record<string, Draft>;
}

/** Ключ черновика диалога, которого ещё нет на сервере. */
export function newDialogKey(kind: DialogKind): string {
  return `new:${kind}`;
}

const EMPTY_HISTORY: History = { status: 'idle', items: [], nextCursor: null };

export const EMPTY_DRAFT: Draft = { text: '', attachments: [], notice: null };

export const INITIAL_STATE: ChatState = {
  history: { chat: EMPTY_HISTORY, sql: EMPTY_HISTORY, cogis: EMPTY_HISTORY, docparse: EMPTY_HISTORY },
  dialogs: {},
  drafts: {},
};

const EMPTY_VIEW: DialogView = { status: 'ready', messages: [], olderCursor: null, generation: null };

export type ChatAction =
  | { type: 'historyLoading'; kind: DialogKind }
  | { type: 'historyLoaded'; kind: DialogKind; items: Dialog[]; nextCursor: string | null; append: boolean }
  | { type: 'historyFailed'; kind: DialogKind }
  | { type: 'dialogUpserted'; dialog: Dialog; toTop: boolean }
  | { type: 'dialogTouched'; id: string; now: string }
  | { type: 'dialogRemoved'; id: string }
  | { type: 'dialogUnlisted'; id: string }
  | { type: 'messagesLoading'; id: string }
  | { type: 'messagesLoaded'; id: string; items: Message[]; nextCursor: string | null; older: boolean }
  | { type: 'messagesFailed'; id: string; notFound: boolean }
  | { type: 'generationStarted'; id: string; question: { content: string; attachments: Attachment[] } | null; now: number }
  | { type: 'streamEvent'; id: string; event: StreamEvent; now: number }
  | { type: 'generationStopped'; id: string }
  | { type: 'generationRejected'; id: string; messages: ChatMessage[] }
  | { type: 'draftUpdated'; key: string; update: (draft: Draft) => Draft }
  | { type: 'draftMoved'; from: string; to: string };

function without<T>(record: Record<string, T>, key: string): Record<string, T> {
  return Object.fromEntries(Object.entries(record).filter(([name]) => name !== key));
}

function withView(state: ChatState, id: string, update: (view: DialogView) => DialogView): ChatState {
  return { ...state, dialogs: { ...state.dialogs, [id]: update(state.dialogs[id] ?? EMPTY_VIEW) } };
}

function withLastAnswer(view: DialogView, update: (answer: ChatMessage) => ChatMessage): DialogView {
  const index = view.messages.length - 1;
  const last = view.messages[index];
  if (!last || last.role !== 'assistant') {
    return view;
  }
  return { ...view, messages: [...view.messages.slice(0, index), update(last)] };
}

/** Сообщение, которому сервер ещё не назвал идентификатор (до события `start`). */
const LOCAL_ID = 'local-';

function localMessage(role: 'user' | 'assistant', now: number, patch: Partial<ChatMessage>): ChatMessage {
  return {
    id: `${LOCAL_ID}${role}-${now}`,
    role,
    content: '',
    status: role === 'user' ? 'complete' : 'streaming',
    error_code: null,
    reasoning: null,
    reasoning_seconds: null,
    attachments: [],
    sources: null,
    sources_found: null,
    sql_check: null,
    sql_dangers: null,
    dropped_messages: 0,
    created_at: new Date(now).toISOString(),
    ...patch,
  };
}

/**
 * Источники завершённого ответа — только те из найденных, на которые в тексте есть сноска;
 * так же их сохраняет сервер, в том числе у остановленного и оборванного ответа (концепция §4.3).
 */
function withCitedSources(answer: ChatMessage, generation: Generation): ChatMessage {
  if (generation.found === null) {
    return answer;
  }
  const cited = footnoteNumbers(answer.content);
  return { ...answer, sources: generation.found.filter((source) => cited.has(source.n)) };
}

/** Применяет событие потока к диалогу (контракт §6.2). Размышления и текст принимаются в любом чередовании. */
export function applyStreamEvent(view: DialogView, event: StreamEvent, now: number): DialogView {
  const generation = view.generation;
  if (!generation) {
    return view;
  }
  switch (event.type) {
    case 'start': {
      const messages = view.messages.map((message, index) => {
        if (index === view.messages.length - 1) {
          return { ...message, id: event.assistant_message_id };
        }
        return index === view.messages.length - 2 ? { ...message, id: event.user_message_id } : message;
      });
      return { ...view, messages };
    }
    case 'sql_question_check': {
      // Предупреждение об опасной операции во вставленном запросе — у вопроса, до начала ответа.
      const index = view.messages.length - 2;
      const messages = view.messages.map((message, i) =>
        i === index && message.role === 'user' ? { ...message, sql_dangers: event.dangers } : message,
      );
      return { ...view, messages };
    }
    case 'sql_check':
      return withLastAnswer(view, (answer) => ({ ...answer, sql_check: { blocks: event.blocks } }));
    case 'progress':
    case 'extraction_started':
    case 'extraction':
      // События потока разбора документа: у диалогов их не бывает.
      return view;
    case 'search_started':
      return { ...view, generation: { ...generation, phase: 'searching' } };
    case 'sources':
      return {
        ...withLastAnswer(view, (answer) => ({ ...answer, sources_found: event.sources.length })),
        generation: { ...generation, phase: 'preparing', found: event.sources },
      };
    case 'context_truncated':
      return withLastAnswer(view, (answer) => ({ ...answer, dropped_messages: event.dropped_messages }));
    case 'reasoning_delta':
      return {
        ...withLastAnswer(view, (answer) => ({ ...answer, reasoning: (answer.reasoning ?? '') + event.text })),
        generation: {
          ...generation,
          phase: generation.phase === 'answering' ? 'answering' : 'thinking',
          reasoningStartedAt: generation.reasoningStartedAt ?? now,
        },
      };
    case 'delta':
      return {
        ...withLastAnswer(view, (answer) => ({
          ...answer,
          content: answer.content + event.text,
          // Секунды размышлений — до первого текста ответа; после перезагрузки их отдаёт сервер.
          reasoning_seconds:
            answer.reasoning_seconds ??
            (generation.reasoningStartedAt === null
              ? null
              : Math.round((now - generation.reasoningStartedAt) / 1000)),
        })),
        generation: { ...generation, phase: 'answering' },
      };
    case 'done':
      return {
        ...withLastAnswer(view, (answer) => ({ ...withCitedSources(answer, generation), status: event.status })),
        generation: null,
      };
    case 'error':
      return {
        ...withLastAnswer(view, (answer) => ({
          ...withCitedSources(answer, generation),
          status: 'error',
          error_code: event.code,
          error_message: event.message,
        })),
        generation: null,
      };
    case 'title':
      return view;
  }
}

function upsertDialog(items: Dialog[], dialog: Dialog, toTop: boolean): Dialog[] {
  const exists = items.some((item) => item.id === dialog.id);
  if (toTop || !exists) {
    return [dialog, ...items.filter((item) => item.id !== dialog.id)];
  }
  return items.map((item) => (item.id === dialog.id ? dialog : item));
}

function withHistory(state: ChatState, kind: DialogKind, update: (history: History) => History): ChatState {
  return { ...state, history: { ...state.history, [kind]: update(state.history[kind]) } };
}

/** Применяет правку к истории того вида, в которой есть диалог `id`. */
function withHistoryOf(state: ChatState, id: string, update: (items: Dialog[]) => Dialog[]): ChatState {
  const kind = (Object.keys(state.history) as DialogKind[]).find((candidate) =>
    state.history[candidate].items.some((item) => item.id === id),
  );
  return kind ? withHistory(state, kind, (history) => ({ ...history, items: update(history.items) })) : state;
}

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case 'historyLoading':
      return withHistory(state, action.kind, (history) => ({ ...history, status: 'loading' }));
    case 'historyLoaded':
      return withHistory(state, action.kind, (history) => {
        const known = new Set(action.items.map((item) => item.id));
        const items = action.append
          ? [...history.items.filter((item) => !known.has(item.id)), ...action.items]
          : action.items;
        return { status: 'ready', items, nextCursor: action.nextCursor };
      });
    case 'historyFailed':
      return withHistory(state, action.kind, (history) => ({ ...history, status: 'failed' }));
    case 'dialogUpserted':
      return withHistory(state, action.dialog.kind, (history) => ({
        ...history,
        items: upsertDialog(history.items, action.dialog, action.toTop),
      }));
    case 'dialogTouched':
      return withHistoryOf(state, action.id, (items) => {
        const dialog = items.find((item) => item.id === action.id) as Dialog;
        return upsertDialog(items, { ...dialog, updated_at: action.now }, true);
      });
    case 'dialogUnlisted':
      return withHistoryOf(state, action.id, (items) => items.filter((item) => item.id !== action.id));
    case 'dialogRemoved':
      return {
        ...withHistoryOf(state, action.id, (items) => items.filter((item) => item.id !== action.id)),
        dialogs: { ...state.dialogs, [action.id]: { ...EMPTY_VIEW, status: 'deleted' } },
        drafts: without(state.drafts, action.id),
      };
    case 'messagesLoading':
      return withView(state, action.id, (view) => ({ ...view, status: view.messages.length ? view.status : 'loading' }));
    case 'messagesLoaded':
      return withView(state, action.id, (view) => {
        const page = [...action.items].reverse();
        if (action.older) {
          return { ...view, status: 'ready', messages: [...page, ...view.messages], olderCursor: action.nextCursor };
        }
        // Обновление последней страницы: ранее подгруженные старые сообщения остаются.
        const fresh = new Set(page.map((message) => message.id));
        const firstFresh = page[0];
        const older = firstFresh
          ? view.messages.filter(
              (message) =>
                !fresh.has(message.id) && !message.id.startsWith(LOCAL_ID) && message.created_at < firstFresh.created_at,
            )
          : [];
        const olderCursor = older.length ? view.olderCursor : action.nextCursor;
        return { ...view, status: 'ready', messages: [...older, ...page], olderCursor };
      });
    case 'messagesFailed':
      return withView(state, action.id, (view) => ({ ...view, status: action.notFound ? 'not_found' : 'failed' }));
    case 'generationStarted':
      return withView(state, action.id, (view) => {
        const kept = action.question
          ? view.messages
          : view.messages.filter((message, index) => index < view.messages.length - 1 || message.role === 'user');
        const question = action.question ? [localMessage('user', action.now, action.question)] : [];
        return {
          ...view,
          status: 'ready',
          messages: [...kept, ...question, localMessage('assistant', action.now, {})],
          generation: { phase: 'sending', startedAt: action.now, reasoningStartedAt: null, found: null },
        };
      });
    case 'streamEvent': {
      const event = action.event;
      if (event.type === 'title') {
        return withHistoryOf(state, action.id, (items) =>
          items.map((item) => (item.id === action.id ? { ...item, title: event.title } : item)),
        );
      }
      return withView(state, action.id, (view) => applyStreamEvent(view, event, action.now));
    }
    case 'generationStopped':
      return withView(state, action.id, (view) =>
        view.generation
          ? {
              ...withLastAnswer(view, (answer) => ({
                ...withCitedSources(answer, view.generation as Generation),
                status: 'stopped',
              })),
              generation: null,
            }
          : view,
      );
    case 'generationRejected':
      return withView(state, action.id, (view) => ({ ...view, messages: action.messages, generation: null }));
    case 'draftUpdated':
      return {
        ...state,
        drafts: { ...state.drafts, [action.key]: action.update(state.drafts[action.key] ?? EMPTY_DRAFT) },
      };
    case 'draftMoved': {
      const draft = state.drafts[action.from];
      const drafts = without(state.drafts, action.from);
      return { ...state, drafts: draft ? { ...drafts, [action.to]: draft } : drafts };
    }
  }
}

export type HistoryGroup = 'today' | 'yesterday' | 'week' | 'earlier';

/** Группа истории по дате последнего сообщения: «Сегодня», «Вчера», «На этой неделе» (с понедельника), «Раньше». */
export function historyGroup(updatedAt: string, now: Date): HistoryGroup {
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const day = 24 * 60 * 60 * 1000;
  const time = new Date(updatedAt).getTime();
  if (time >= today) {
    return 'today';
  }
  if (time >= today - day) {
    return 'yesterday';
  }
  const daysSinceMonday = (now.getDay() + 6) % 7;
  return time >= today - daysSinceMonday * day ? 'week' : 'earlier';
}
