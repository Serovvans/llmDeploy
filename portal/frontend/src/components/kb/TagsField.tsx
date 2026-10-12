import { TokenInput, TokenInputType } from '@skbkontur/react-ui';
import { LocaleContext } from '@skbkontur/react-ui/lib/locale';

import { api } from '../../api/client';
import type { PortalConfig } from '../../api/types';
import { acceptTags, tagKey } from '../../kb/documents';
import { texts } from '../../texts';
import { Field } from '../Field';

const t = texts.kb.tags;

/** Сообщение под полем тегов; `blocks` — в поле остался неисправленный ввод, отправлять форму рано. */
export interface TagsProblem {
  text: string;
  blocks: boolean;
}

interface TagsFieldProps {
  tags: string[];
  /** Пределы из настроек; `null` — настройки не загрузились, теги проверяет только сервер. */
  limits: PortalConfig['kb']['tags'] | null;
  /** Сообщение под полем: проверка в поле или отказ сервера. */
  problem: TagsProblem | null;
  /** Пояснение под полем: в окне правки — пределы, в окне добавления — зачем теги. */
  hint: string;
  disabled?: boolean;
  /** Поле — главное в своём окне: фокус встаёт в него при открытии. */
  autoFocus?: boolean;
  /** Новый список и итог последней правки: отклонённый тег в список не попадает. */
  onChange: (tags: string[], problem: TagsProblem | null) => void;
}

/** Подсказки существующих тегов; сбой подсказок вводу не мешает — тег можно набрать целиком. */
async function suggestTags(query: string): Promise<string[]> {
  try {
    return (await api.listKbTags([], query.trim())).items;
  } catch {
    return [];
  }
}

/**
 * Теги документа (концепция §5.6): свободный ввод с подсказками существующих тегов. Enter и запятая
 * добавляют тег; пробелы по краям отбрасываются, повтор в другом регистре не добавляется.
 */
export function TagsField({ tags, limits, problem, hint, disabled, autoFocus, onChange }: TagsFieldProps) {
  const tooLong = (value: string) => limits !== null && value.trim().length > limits.max_length;
  // Список подсказок закрывает текст под полем, поэтому причина отказа названа и в самом списке —
  // вместо общего «Неверный формат» библиотеки.
  const locale = limits ? { TokenInput: { addButtonInvalidComment: t.tooLong(limits.max_length) } } : {};
  return (
    <Field label={t.label} error={problem?.text} hint={hint}>
      {(control) => (
        <LocaleContext.Provider value={{ locale }}>
          <TokenInput<string>
            {...control}
            type={TokenInputType.Combined}
            width="100%"
            placeholder={t.placeholder}
            disabled={disabled}
            autoFocus={autoFocus}
            selectedItems={tags}
            getItems={suggestTags}
            // Подсказки — по первым буквам («начните вводить…»): в пустом поле список не закрывает кнопки окна.
            hideMenuIfEmptyInputValue
            valueToItem={(value) => value.trim()}
            itemToId={tagKey}
            // Слишком длинный тег остаётся в поле: его сокращают, а не набирают заново.
            isTokenValid={(value) => !tooLong(value)}
            // Набранный, но не подтверждённый тег добавляется при уходе из поля, а не теряется молча.
            onUnexpectedInput={(value) => value.trim() || undefined}
            onInputValueChange={(value) => {
              if (limits && tooLong(value)) {
                onChange(tags, { text: t.tooLong(limits.max_length), blocks: true });
              } else if (value !== '' || problem?.blocks) {
                // Поле, опустевшее после добавления тега, сообщение о нём же не стирает.
                onChange(tags, null);
              }
            }}
            onValueChange={(next) => {
              const accepted = acceptTags(next, limits);
              onChange(accepted.tags, accepted.error === null ? null : { text: accepted.error, blocks: false });
            }}
          />
        </LocaleContext.Provider>
      )}
    </Field>
  );
}
