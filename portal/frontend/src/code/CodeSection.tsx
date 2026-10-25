import { Route, Routes } from 'react-router-dom';

import { CodeProvider } from './CodeContext';
import { CodeHome } from './CodeHome';
import { OldDialogScreen } from './OldDialogScreen';
import { ProjectRoute } from './ProjectScreen';
import { Wizard } from './Wizard';

/**
 * Раздел «Код» (концепция §5.13): грузится отдельным файлом только при входе в раздел — вместе с
 * ним и Monaco, поэтому основной файл интерфейса не растёт (CLAUDE.md, пайплайн «Код», правило 13).
 */
export default function CodeSection() {
  return (
    <CodeProvider>
      <Routes>
        <Route index element={<CodeHome />} />
        <Route path="new" element={<Wizard />} />
        <Route path="dialogs/:id" element={<OldDialogScreen />} />
        <Route path=":id" element={<ProjectRoute />} />
      </Routes>
    </CodeProvider>
  );
}
