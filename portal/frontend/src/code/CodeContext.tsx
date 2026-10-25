import { SingleToast } from '@skbkontur/react-ui';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api, isApiError } from '../api/client';
import type { CodeProject, CodeProjectItem, CodeSdk } from '../api/types';
import { useSession } from '../session/SessionContext';
import { texts } from '../texts';

const w = texts.workshop;

export interface ProjectList {
  items: CodeProjectItem[];
  nextCursor: string | null;
  status: 'loading' | 'ready' | 'failed';
}

interface CodeSection {
  /** Справочник и набор пакетов — одним запросом при открытии раздела (контракт §7.5.14); `null` — ещё нет. */
  sdk: CodeSdk | null;
  projects: ProjectList;
  loadProjects: (more: boolean) => void;
  /** Проект изменился или создан — строка списка встаёт наверх (порядок — по времени изменения). */
  touchProject: (project: CodeProject) => void;
  forgetProject: (id: string) => void;
  /**
   * Отказ `403 feature_disabled` по маршрутам раздела: раздел выключили, пока экран открыт (§5.13.14) —
   * настройки перечитываются, переход на `/cogis`. `true` — отказ обработан.
   */
  sectionOff: (error: unknown) => boolean;
}

const CodeContext = createContext<CodeSection | null>(null);

export function useCode(): CodeSection {
  const value = useContext(CodeContext);
  if (!value) {
    throw new Error('useCode вызван вне CodeProvider');
  }
  return value;
}

function itemOf(project: CodeProject): CodeProjectItem {
  const { build } = project;
  return {
    id: project.id,
    name: project.name,
    updated_at: project.updated_at,
    build: build ? { status: build.status, outdated: build.outdated, findings: build.findings?.length ?? null } : null,
  };
}

/** Общее для экранов раздела «Код», пока он открыт: список проектов, справочник CoGIS, выключение флагом. */
export function CodeProvider({ children }: { children: React.ReactNode }) {
  const navigate = useNavigate();
  const { reloadConfig } = useSession();
  const [sdk, setSdk] = useState<CodeSdk | null>(null);
  const [projects, setProjects] = useState<ProjectList>({ items: [], nextCursor: null, status: 'loading' });
  const cursor = useRef<string | null>(null);
  const toastShown = useRef(false);

  const sectionOff = useCallback(
    (error: unknown) => {
      if (!isApiError(error, 'feature_disabled')) {
        return false;
      }
      reloadConfig();
      if (!toastShown.current) {
        toastShown.current = true;
        SingleToast.push(w.build.sectionOff);
      }
      navigate('/cogis', { replace: true });
      return true;
    },
    [navigate, reloadConfig],
  );

  const loadProjects = useCallback(
    (more: boolean) => {
      const from = more ? cursor.current : null;
      api.listCodeProjects(from).then(
        (page) => {
          cursor.current = page.next_cursor;
          setProjects((state) => ({
            items: more ? [...state.items, ...page.items.filter((item) => !state.items.some((known) => known.id === item.id))] : page.items,
            nextCursor: page.next_cursor,
            status: 'ready',
          }));
        },
        (error: unknown) => {
          if (!sectionOff(error)) {
            setProjects((state) => ({ ...state, status: 'failed' }));
          }
        },
      );
    },
    [sectionOff],
  );

  useEffect(() => {
    loadProjects(false);
    let cancelled = false;
    api.getCodeSdk().then(
      (value) => {
        if (!cancelled) {
          setSdk(value);
        }
      },
      (error: unknown) => {
        // Без справочника редактор работает без подсказок; заметки о библиотеках тоже нет.
        sectionOff(error);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [loadProjects, sectionOff]);

  const touchProject = useCallback((project: CodeProject) => {
    setProjects((state) => ({ ...state, items: [itemOf(project), ...state.items.filter((item) => item.id !== project.id)] }));
  }, []);

  const forgetProject = useCallback((id: string) => {
    setProjects((state) => ({ ...state, items: state.items.filter((item) => item.id !== id) }));
  }, []);

  const value = useMemo(
    () => ({ sdk, projects, loadProjects, touchProject, forgetProject, sectionOff }),
    [sdk, projects, loadProjects, touchProject, forgetProject, sectionOff],
  );
  return <CodeContext.Provider value={value}>{children}</CodeContext.Provider>;
}
