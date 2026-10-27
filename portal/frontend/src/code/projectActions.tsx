import { SingleToast } from '@skbkontur/react-ui';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api } from '../api/client';
import type { CodeKind } from '../api/types';
import { ConfirmModal } from '../components/ConfirmModal';
import { useFocusReturn } from '../hooks/useFocusReturn';
import { texts } from '../texts';
import { useCode } from './CodeContext';
import { saveBlob } from './download';
import { isWebKind } from './kinds';
import { WebExportModal } from './web/WebExportModal';

const w = texts.workshop;
const d = texts.dialogs.code;

interface Removing {
  id: string;
  name: string;
  kind?: CodeKind;
}

/**
 * «Скачать для Visual Studio» (GPTool), «Скачать для CoGIS» (JS и HTML, §5.13.22) и «Удалить проект» — из строки
 * списка и из строки заголовка (§5.13.5, §5.13.13).
 */
export function useProjectActions() {
  const navigate = useNavigate();
  const { forgetProject, sectionOff } = useCode();
  const [removing, setRemoving] = useState<Removing | null>(null);
  const [exporting, setExporting] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const rememberOpener = useFocusReturn(removing !== null);
  const rememberExportOpener = useFocusReturn(exporting !== null);

  const exportProject = (id: string, name: string) => {
    SingleToast.push(w.project.exporting);
    api.exportCodeProject(id).then(({ blob, fileName }) => saveBlob(blob, fileName ?? `${name}.zip`), (error: unknown) => {
      if (!sectionOff(error)) {
        SingleToast.push(w.project.exportFailed, { use: 'error' });
      }
    });
  };

  const confirm = async () => {
    if (!removing) {
      return;
    }
    setPending(true);
    try {
      await api.deleteCodeProject(removing.id);
      forgetProject(removing.id);
      SingleToast.push(d.removed);
      navigate('/code');
    } catch (error) {
      if (!sectionOff(error)) {
        SingleToast.push(d.removeFailed, { use: 'error' });
      }
    } finally {
      setPending(false);
      setRemoving(null);
    }
  };

  const modal = (
    <>
      {removing && (
        <ConfirmModal
          title={d.removeTitle(removing.name)}
          body={removing.kind && isWebKind(removing.kind) ? w.web.removeBody : d.removeBody}
          action={w.files.removeAction}
          pending={pending}
          onConfirm={() => void confirm()}
          onCancel={() => setRemoving(null)}
        />
      )}
      {exporting && <WebExportModal project={{ id: exporting }} onClose={() => setExporting(null)} sectionOff={sectionOff} />}
    </>
  );

  /** `opener` — селектор элемента, на который вернётся фокус после «Отмена». */
  const askRemove = (project: Removing, opener: string) => {
    rememberOpener(opener);
    setRemoving(project);
  };

  /** Окно «Скачать для CoGIS» из списка проектов: файлы и ошибки синтаксиса окно получает само. */
  const exportWeb = (id: string, opener: string) => {
    rememberExportOpener(opener);
    setExporting(id);
  };

  return { exportProject, exportWeb, askRemove, modal };
}
