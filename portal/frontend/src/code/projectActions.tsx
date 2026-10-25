import { SingleToast } from '@skbkontur/react-ui';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api } from '../api/client';
import { ConfirmModal } from '../components/ConfirmModal';
import { useFocusReturn } from '../hooks/useFocusReturn';
import { texts } from '../texts';
import { useCode } from './CodeContext';
import { saveBlob } from './download';

const w = texts.workshop;
const d = texts.dialogs.code;

/** «Скачать для Visual Studio» и «Удалить проект» — из строки списка и из строки заголовка (§5.13.5, §5.13.13). */
export function useProjectActions() {
  const navigate = useNavigate();
  const { forgetProject, sectionOff } = useCode();
  const [removing, setRemoving] = useState<{ id: string; name: string } | null>(null);
  const [pending, setPending] = useState(false);
  const rememberOpener = useFocusReturn(removing !== null);

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

  const modal = removing && (
    <ConfirmModal
      title={d.removeTitle(removing.name)}
      body={d.removeBody}
      action={w.files.removeAction}
      pending={pending}
      onConfirm={() => void confirm()}
      onCancel={() => setRemoving(null)}
    />
  );

  /** `opener` — селектор элемента, на который вернётся фокус после «Отмена». */
  const askRemove = (project: { id: string; name: string }, opener: string) => {
    rememberOpener(opener);
    setRemoving(project);
  };

  return { exportProject, askRemove, modal };
}
