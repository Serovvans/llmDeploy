/**
 * Воркер TypeScript для Monaco с русскими сообщениями проверки синтаксиса (концепция §5.13.19, Д-67):
 * сообщения той же версии TypeScript, что встроена в Monaco; кода без перевода показывается как
 * «Ошибка синтаксиса».
 */
import { typescript } from 'monaco-editor/languages/features/typescript/lib/typescriptServices.js';
import 'monaco-editor/languages/features/typescript/ts.worker.js';
import messages from 'virtual:ts-syntax-messages-ru';

// The worker module only installs `onmessage`; the service is created on the first message, after this line.
typescript.setLocalizedDiagnosticMessages(messages);
