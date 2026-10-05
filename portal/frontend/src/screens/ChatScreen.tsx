import { useChat } from '../chat/ChatProvider';
import { DialogScreen } from '../components/chat/DialogScreen';
import { texts } from '../texts';

/** Чат (концепция §5.5): общий рабочий экран с вложениями, режимом ответа и базой знаний. */
export function ChatScreen() {
  const { mode, knowledge } = useChat();
  return (
    <DialogScreen
      kind="chat"
      basePath="/chat"
      sectionTitle={texts.dialogs.chat.newLabel}
      empty={texts.chat.empty}
      params={{ mode, knowledge }}
    />
  );
}
