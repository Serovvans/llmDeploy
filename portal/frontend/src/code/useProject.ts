import { SingleToast } from '@skbkontur/react-ui';
import { useCallback, useEffect, useReducer, useRef, useState } from 'react';

import { api, ApiError, isApiError, NetworkError } from '../api/client';
import type { CodeBuild, CodeConfig, CodeFile, CodeProject } from '../api/types';
import { formatSize, texts } from '../texts';
import { filePathServerError, splitPath } from './rules';

const w = texts.workshop;
/** Ручные правки сохраняются через секунду после последнего нажатия (ПД §5.4, концепция §5.13.6). */
export const SAVE_DELAY_MS = 1000;
/** Без связи сохранение повторяется само. */
const OFFLINE_RETRY_MS = 5000;
/** Идёт сборка, начатая не этой кнопкой: проект перечитывается, пока признак не снимется (§5.13.8). */
export const BUILD_POLL_MS = 3000;

interface Buffer {
  /** Текст в редакторе. */
  text: string;
  /** Текст, который сервер уже принял, и его ревизия. */
  saved: string;
  revision: number;
}

export type SaveState =
  | { kind: 'saved' }
  | { kind: 'saving' }
  | { kind: 'offline' }
  | { kind: 'error'; text: string };

export interface Conflict {
  fileId: string;
  revision: number;
}

export type ProjectStatus = 'loading' | 'ready' | 'not_found' | 'failed';

export interface ProjectApi {
  status: ProjectStatus;
  project: CodeProject | null;
  /** Текст файла в редакторе: с несохранёнными правками. */
  text: (fileId: string) => string;
  setText: (fileId: string, text: string) => void;
  /** Сохранить всё несохранённое сейчас; `false` — что-то не сохранилось. */
  flush: () => Promise<boolean>;
  saveState: SaveState;
  conflict: Conflict | null;
  resolveConflict: (choice: 'reload' | 'overwrite') => void;
  /** Ошибка пути — текст для поля в дереве; файл — создан. */
  createFile: (path: string) => Promise<CodeFile | string>;
  renameFile: (fileId: string, path: string) => Promise<CodeFile | string>;
  deleteFile: (fileId: string) => Promise<boolean>;
  /** Принятая правка, возврат версии, сохранённые настройки: проект от сервера целиком. */
  replace: (project: CodeProject) => void;
  reload: () => Promise<CodeProject | null>;
  /** Меняется после каждого сохранённого изменения файлов или пакетов: оценка памяти повторяется. */
  revision: string;
  /** Файлы изменились здесь после последней сборки — станция «Код изменён» до ответа сервера. */
  changedSinceBuild: boolean;
  setBuild: (build: CodeBuild) => void;
  /**
   * Что делает экран, когда файл удалён в другой вкладке (закрывает вкладку) и когда удалён
   * весь проект (уходит на `/code`).
   */
  listen: (handlers: { fileGone: (fileId: string) => void; projectGone: () => void }) => void;
}

/** Состояние открытого проекта: файлы с автосохранением, сборка, ревизии (концепция §5.13.5, §5.13.6). */
export function useProject(id: string, config: CodeConfig | null, sectionOff: (error: unknown) => boolean): ProjectApi {
  const [status, setStatus] = useState<ProjectStatus>('loading');
  const [project, setProject] = useState<CodeProject | null>(null);
  const [, rerender] = useReducer((n: number) => n + 1, 0);
  const buffers = useRef(new Map<string, Buffer>());
  const timers = useRef(new Map<string, number>());
  const saving = useRef(new Map<string, Promise<boolean>>());
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'saved' });
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [revision, bumpRevision] = useReducer((n: number) => n + 1, 0);
  const [changedSinceBuild, setChangedSinceBuild] = useState(false);
  const projectRef = useRef<CodeProject | null>(null);
  const onFileGone = useRef<(fileId: string) => void>(() => undefined);
  const onProjectGone = useRef<() => void>(() => undefined);

  const adopt = useCallback((next: CodeProject, force: boolean) => {
    projectRef.current = next;
    const known = buffers.current;
    for (const file of next.files) {
      const buffer = known.get(file.id);
      // Несохранённый текст не затирается обычным перечитыванием; принятая правка и возврат — затирают.
      if (!buffer || force || buffer.text === buffer.saved) {
        known.set(file.id, { text: file.content, saved: file.content, revision: file.revision });
      } else {
        buffer.saved = file.content;
        buffer.revision = file.revision;
      }
    }
    for (const fileId of [...known.keys()]) {
      if (!next.files.some((file) => file.id === fileId)) {
        known.delete(fileId);
      }
    }
    setProject(next);
  }, []);

  const changed = useCallback(() => {
    bumpRevision();
    setChangedSinceBuild(true);
  }, []);

  const updateFile = useCallback((file: CodeFile) => {
    const current = projectRef.current;
    if (!current) {
      return;
    }
    const exists = current.files.some((item) => item.id === file.id);
    const files = exists ? current.files.map((item) => (item.id === file.id ? file : item)) : [...current.files, file];
    const next = { ...current, files, updated_at: file.updated_at };
    projectRef.current = next;
    setProject(next);
  }, []);

  const reload = useCallback(async () => {
    try {
      const next = await api.getCodeProject(id);
      adopt(next, false);
      // Итог сборки — от сервера: его `outdated` уже учитывает все сохранённые изменения.
      setChangedSinceBuild(false);
      setStatus('ready');
      return next;
    } catch (error) {
      if (isApiError(error, 'not_found')) {
        if (projectRef.current) {
          onProjectGone.current();
        } else {
          setStatus('not_found');
        }
      } else if (!sectionOff(error) && !projectRef.current) {
        setStatus('failed');
      }
      return null;
    }
  }, [adopt, id, sectionOff]);

  // Экран проекта создаётся заново для каждого `id` (ключ маршрута): здесь только первое чтение.
  useEffect(() => {
    let cancelled = false;
    api.getCodeProject(id).then(
      (next) => {
        if (!cancelled) {
          adopt(next, false);
          setStatus('ready');
        }
      },
      (error: unknown) => {
        if (!cancelled && !sectionOff(error)) {
          setStatus(isApiError(error, 'not_found') ? 'not_found' : 'failed');
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [adopt, id, sectionOff]);

  const fileGone = useCallback(
    async (fileId: string) => {
      // 404 у файла: удалён файл или весь проект — скажет перечитывание проекта.
      const name = splitPath(projectRef.current?.files.find((file) => file.id === fileId)?.path ?? '').name;
      const next = await reload();
      if (next && !next.files.some((file) => file.id === fileId)) {
        onFileGone.current(fileId);
        SingleToast.push(w.files.removedElsewhere(name));
      }
    },
    [reload],
  );

  const saveRef = useRef<(fileId: string) => Promise<boolean>>(() => Promise.resolve(true));
  const save = useCallback(
    (fileId: string): Promise<boolean> => {
      const running = saving.current.get(fileId);
      if (running) {
        // Сохранение уже идёт: следующее начнётся после него, с последним текстом.
        return running.then(() => saveRef.current(fileId));
      }
      const buffer = buffers.current.get(fileId);
      if (!buffer || buffer.text === buffer.saved) {
        return Promise.resolve(true);
      }
      const text = buffer.text;
      setSaveState({ kind: 'saving' });
      const job = api.updateCodeFile(id, fileId, { revision: buffer.revision, content: text }).then(
        (file) => {
          const current = buffers.current.get(fileId);
          if (current) {
            current.saved = text;
            current.revision = file.revision;
          }
          updateFile(file);
          changed();
          setSaveState({ kind: 'saved' });
          return true;
        },
        (error: unknown) => {
          if (error instanceof NetworkError) {
            setSaveState({ kind: 'offline' });
            window.clearTimeout(timers.current.get(fileId));
            timers.current.set(fileId, window.setTimeout(() => void saveRef.current(fileId), OFFLINE_RETRY_MS));
          } else if (isApiError(error, 'file_changed')) {
            // Файл изменили в другой вкладке: до выбора сотрудника он не сохраняется (§5.13.6).
            setConflict({ fileId, revision: Number(error.details.revision) });
            setSaveState({ kind: 'saved' });
          } else if (isApiError(error, 'not_found')) {
            setSaveState({ kind: 'saved' });
            void fileGone(fileId);
          } else if (!sectionOff(error)) {
            setSaveState({ kind: 'error', text: saveErrorText(error, config) });
          }
          return false;
        },
      );
      saving.current.set(fileId, job);
      return job.finally(() => saving.current.delete(fileId));
    },
    [changed, config, fileGone, id, sectionOff, updateFile],
  );
  useEffect(() => {
    saveRef.current = save;
  }, [save]);

  const setText = useCallback(
    (fileId: string, text: string) => {
      const buffer = buffers.current.get(fileId);
      if (!buffer || buffer.text === text) {
        return;
      }
      buffer.text = text;
      rerender();
      window.clearTimeout(timers.current.get(fileId));
      if (conflict?.fileId === fileId) {
        return;
      }
      timers.current.set(fileId, window.setTimeout(() => void save(fileId), SAVE_DELAY_MS));
    },
    [conflict, save],
  );

  const flush = useCallback(async () => {
    timers.current.forEach((timer) => window.clearTimeout(timer));
    timers.current.clear();
    const dirty = [...buffers.current.entries()].filter(
      ([fileId, buffer]) => buffer.text !== buffer.saved && conflict?.fileId !== fileId,
    );
    const results = await Promise.all(dirty.map(([fileId]) => save(fileId)));
    return results.every(Boolean) && conflict === null;
  }, [conflict, save]);

  // Уход со страницы с несохранённой правкой: браузер предупреждает (§6, таблица ухода).
  useEffect(() => {
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      if ([...buffers.current.values()].some((buffer) => buffer.text !== buffer.saved)) {
        event.preventDefault();
      }
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
  }, []);

  // Уход из раздела — сохранение несохранённого.
  const flushRef = useRef(flush);
  useEffect(() => {
    flushRef.current = flush;
  });
  useEffect(() => () => void flushRef.current(), [id]);

  const resolveConflict = useCallback(
    (choice: 'reload' | 'overwrite') => {
      const current = conflict;
      if (!current) {
        return;
      }
      setConflict(null);
      if (choice === 'overwrite') {
        const buffer = buffers.current.get(current.fileId);
        if (buffer) {
          buffer.revision = current.revision;
        }
        void save(current.fileId);
        return;
      }
      void api.getCodeProject(id).then(
        (next) => {
          const file = next.files.find((item) => item.id === current.fileId);
          if (file) {
            buffers.current.set(file.id, { text: file.content, saved: file.content, revision: file.revision });
          }
          adopt(next, false);
          setChangedSinceBuild(false);
        },
        (error: unknown) => {
          setConflict(current);
          if (isApiError(error, 'not_found')) {
            onProjectGone.current();
          } else if (!sectionOff(error)) {
            setSaveState({ kind: 'error', text: saveErrorText(error, config) });
          }
        },
      );
    },
    [adopt, config, conflict, id, save, sectionOff],
  );

  const fileFailure = useCallback(
    (error: unknown, path: string): string => {
      if (sectionOff(error)) {
        return '';
      }
      if (error instanceof ApiError) {
        return filePathServerError(error, path) ?? saveErrorText(error, config);
      }
      return saveErrorText(error, config);
    },
    [config, sectionOff],
  );

  const createFile = useCallback(
    async (path: string) => {
      try {
        const file = await api.createCodeFile(id, path);
        buffers.current.set(file.id, { text: file.content, saved: file.content, revision: file.revision });
        updateFile(file);
        changed();
        return file;
      } catch (error) {
        if (isApiError(error, 'not_found')) {
          onProjectGone.current();
          return '';
        }
        return fileFailure(error, path);
      }
    },
    [changed, fileFailure, id, updateFile],
  );

  const renameFile = useCallback(
    async (fileId: string, path: string) => {
      // Несохранённый текст — сначала: переименование меняет ревизию.
      await save(fileId);
      const buffer = buffers.current.get(fileId);
      if (!buffer) {
        return '';
      }
      try {
        const file = await api.updateCodeFile(id, fileId, { revision: buffer.revision, path });
        buffer.revision = file.revision;
        updateFile(file);
        changed();
        return file;
      } catch (error) {
        if (isApiError(error, 'not_found')) {
          void fileGone(fileId);
          return '';
        }
        if (isApiError(error, 'file_changed')) {
          setConflict({ fileId, revision: Number(error.details.revision) });
          return '';
        }
        return fileFailure(error, path);
      }
    },
    [changed, fileFailure, fileGone, id, save, updateFile],
  );

  const deleteFile = useCallback(
    async (fileId: string) => {
      window.clearTimeout(timers.current.get(fileId));
      try {
        await api.deleteCodeFile(id, fileId);
      } catch (error) {
        if (isApiError(error, 'not_found')) {
          void fileGone(fileId);
        } else if (!sectionOff(error)) {
          SingleToast.push(texts.common.actionFailed, { use: 'error' });
        }
        return false;
      }
      buffers.current.delete(fileId);
      const current = projectRef.current;
      if (current) {
        const next = { ...current, files: current.files.filter((file) => file.id !== fileId) };
        projectRef.current = next;
        setProject(next);
      }
      if (conflict?.fileId === fileId) {
        setConflict(null);
      }
      changed();
      return true;
    },
    [changed, conflict, fileGone, id, sectionOff],
  );

  const replace = useCallback(
    (next: CodeProject) => {
      adopt(next, true);
      changed();
    },
    [adopt, changed],
  );

  const setBuild = useCallback((build: CodeBuild) => {
    const current = projectRef.current;
    if (current) {
      const next = { ...current, build, build_running: false };
      projectRef.current = next;
      setProject(next);
    }
    setChangedSinceBuild(false);
  }, []);

  const text = useCallback((fileId: string) => buffers.current.get(fileId)?.text ?? '', []);
  const listen = useCallback((handlers: { fileGone: (fileId: string) => void; projectGone: () => void }) => {
    onFileGone.current = handlers.fileGone;
    onProjectGone.current = handlers.projectGone;
  }, []);

  return {
    status,
    project,
    text,
    setText,
    flush,
    saveState,
    conflict,
    resolveConflict,
    createFile,
    renameFile,
    deleteFile,
    replace,
    reload,
    revision: String(revision),
    changedSinceBuild,
    setBuild,
    listen,
  };
}

/** Отказ сохранения файла — строка состояния (таблица §5.13.15). */
export function saveErrorText(error: unknown, config: CodeConfig | null): string {
  if (error instanceof NetworkError) {
    return w.editor.offline;
  }
  if (isApiError(error, 'project_limit')) {
    const max = Number(error.details.max);
    return error.details.limit === 'files' ? w.editor.projectFiles(max) : w.editor.projectBytes(formatSize(max));
  }
  if (error instanceof ApiError && error.fields.some((field) => field.field === 'content' && field.code === 'too_long')) {
    return w.editor.tooLarge(formatSize(config?.file_max_bytes ?? 0));
  }
  return w.editor.saveFailed;
}
